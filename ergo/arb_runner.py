"""Run the two-leg pool buy -> bank redeem arbitrage (used by execute_arb.py and the --live scanner)."""
import asyncio
from dataclasses import dataclass
from typing import Callable, Optional

import aiohttp

import config
from arbitrage.sizing import Market, SizeChoice, best_size
from ergo.chain import wait_for_box, wallet_context
from ergo.chain_state import latest_box
from ergo.chain_arb import (
    build_pool_sell_leg,
    build_redeem_leg,
    plan_bank_mint_pool_sell,
    plan_pool_buy_redeem,
    tx_output_box,
)
from ergo.signing import DryRun, check_on_node, guarded_sign, wallet_trees
from ergo.tx_guard import TxGuardError, verify_unsigned_tx

UI_FEE_TREE = "0008cd02c5f61c83056a746a19a9e449e3c9596314cc417a2ef496b7567af558518f2bc7"  # SigmaUSD UI fee
TIMEOUT = aiohttp.ClientTimeout(total=30)


class LegNotReady(RuntimeError):
    """Leg 2 cannot be rebuilt yet (an oracle update is pending); retry without counting a rebuild."""


@dataclass
class ArbResult:
    status: str                # aborted | refused | not_profitable | dry_run | checked | executed | leg1_failed | leg2_failed
    message: str = ""
    erg_in: int = 0
    sigusd_cents: int = 0
    profit_nanoerg: int = 0
    profit_percent: float = 0.0
    tx1: Optional[str] = None
    tx2: Optional[str] = None
    path: str = "redeem"

    @property
    def recover_command(self) -> str:
        amount = f"{self.sigusd_cents / 100:.2f}"
        if self.path == "mint":
            return f"python arb.py swap --sell sigusd --amount {amount} --execute"
        return f"python arb.py redeem --sigusd {amount} --execute"


async def watch_leg2(tx_id: str, get_status, rebuild, *, timeout: float, interval: float,
                     max_rebuilds: int, log: Callable[[str], None]) -> tuple[str, str]:
    """Follow leg 2 until confirmed; rebuild it when it drops out of the mempool.

    get_status(tx_id) -> "confirmed" | "pending" | "dropped"
    rebuild() -> new tx id (built on fresh bank/oracle boxes); may raise.
    Returns (status, tx_id): confirmed | gave_up | timeout.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    rebuilds = 0
    while True:
        status = await get_status(tx_id)
        if status == "confirmed":
            return status, tx_id
        if status == "dropped":
            if rebuilds >= max_rebuilds:
                return "gave_up", tx_id
            rebuilds += 1
            log(f"  Leg 2 {tx_id[:12]} dropped from the mempool (bank/oracle box changed?): "
                f"rebuilding on fresh boxes ({rebuilds}/{max_rebuilds})")
            try:
                tx_id = await rebuild()
                log(f"  Leg 2 resubmitted: https://explorer.ergoplatform.com/en/transactions/{tx_id}")
            except LegNotReady as e:
                rebuilds -= 1
                log(f"  Leg 2 rebuild waiting: {e}")
            except (RuntimeError, TimeoutError, TxGuardError, ValueError) as e:
                log(f"  Leg 2 rebuild failed ({e}); retrying next round")
        if loop.time() >= deadline:
            return "timeout", tx_id
        await asyncio.sleep(interval)


async def _leg2_status(ns, tx_id: str, leg1_box_id: str, explorer=None) -> str:
    """confirmed | pending | dropped, for leg 2 spending leg 1's output `leg1_box_id`.

    /utxo/withPool hides boxes spent by mempool transactions, so leg 1's output
    being visible again means leg 2 left the mempool (dropped); hidden means it
    is still pending (or confirmed, which the wallet or explorer then shows).
    """
    node = config.ERGO_NODE_URL
    async with ns.get(f"{node}/wallet/transactionById?id={tx_id}", timeout=TIMEOUT) as r:
        if r.status == 200 and ((await r.json()).get("numConfirmations") or 0) >= 1:
            return "confirmed"
    if explorer is not None:
        try:
            async with explorer.get(f"{config.ERGO_EXPLORER_API_URL}/transactions/{tx_id}", timeout=TIMEOUT) as r:
                if r.status == 200:
                    return "confirmed"
        except (asyncio.TimeoutError, aiohttp.ClientError):
            pass
    async with ns.get(f"{node}/utxo/withPool/byId/{leg1_box_id}", timeout=TIMEOUT) as r:
        return "dropped" if r.status == 200 else "pending"


async def _fetch_latest(ns, nfts) -> list[tuple[dict, bool]]:
    """(box, pending) per NFT from ergo/chain_state.py: pending pool/bank boxes included,
    the oracle always confirmed with pending=True while an update is in the mempool."""
    return list(await asyncio.gather(*(latest_box(ns, nft) for nft in nfts)))


async def _fetch_boxes(ns, nfts) -> list[dict]:
    return [b for b, _ in await _fetch_latest(ns, nfts)]


ORACLE_PENDING = "oracle update pending (a bank TX on the old oracle box would be dropped); retry after the next block"


async def _leg2_boxes(ns, path: str) -> list[dict]:
    """Fresh contract boxes for rebuilding leg 2: [bank, oracle] (redeem) or [pool] (mint)."""
    if path == "mint":
        return await _fetch_boxes(ns, (config.SPECTRUM_SIGUSD_POOL_NFT,))
    (bank, _), (oracle, oracle_pending) = await _fetch_latest(ns, (config.SIGMAUSD_BANK_NFT, config.SIGMAUSD_ORACLE_NFT))
    if oracle_pending:
        raise LegNotReady(ORACLE_PENDING)
    return [bank, oracle]


async def _submit(ns, signed: dict) -> str:
    async with ns.post(f"{config.ERGO_NODE_URL}/transactions", json=signed, timeout=TIMEOUT) as r:
        body = await r.text()
        if r.status != 200:
            raise RuntimeError(f"submit failed: HTTP {r.status} {body[:400]}")
    return signed.get("id", "?")


def _describe_redeem(plan: dict, erg_in: int, log) -> None:
    i1, i2, cents = plan["leg1_info"], plan["leg2_info"], plan["sigusd_cents"]
    log(f"  Leg 1  pool swap:   {erg_in / 1e9:.4f} ERG -> {cents / 100:.2f} SigUSD "
        f"(impact {i1['price_impact_percent']:.2f}%, miner {i1['miner_fee'] / 1e9:.4f})")
    log(f"  Leg 2  bank redeem: {cents / 100:.2f} SigUSD -> {i2['user_receives'] / 1e9:.6f} ERG "
        f"(2% + UI fee {i2['ui_fee'] / 1e9:.4f}, miner {i2['miner_fee'] / 1e9:.4f})")


def _describe_mint(plan: dict, erg_in: int, log) -> None:
    i1, i2, cents = plan["leg1_info"], plan["leg2_info"], plan["sigusd_cents"]
    log(f"  Leg 1  bank mint:   {i1['cost_nanoerg'] / 1e9:.6f} ERG -> {cents / 100:.2f} SigUSD "
        f"(2% + UI fee {i1['ui_fee'] / 1e9:.4f}, miner {i1['miner_fee'] / 1e9:.4f}; RR after >= 400%)")
    log(f"  Leg 2  pool sell:   {cents / 100:.2f} SigUSD -> {i2['amount_out'] / 1e9:.6f} ERG "
        f"(impact {i2['price_impact_percent']:.2f}%, miner {i2['miner_fee'] / 1e9:.4f})")


PATHS = {
    "redeem": ("pool buy -> bank redeem", plan_pool_buy_redeem, _describe_redeem),
    "mint": ("bank mint -> pool sell", plan_bank_mint_pool_sell, _describe_mint),
}


def choose_size(path: str, pool_box: dict, bank_box: dict, oracle_box: dict, wallet_boxes: list[dict],
                max_erg_in: Optional[int] = None) -> SizeChoice:
    """Best size on exactly these boxes, capped by MAX_TRADE_SIZE_ERG, the wallet minus
    LIVE_ERG_RESERVE, and `max_erg_in` (nanoERG) if given."""
    caps = [config.MAX_TRADE_SIZE_ERG,
            sum(int(b["value"]) for b in wallet_boxes) / 1e9 - config.LIVE_ERG_RESERVE]
    if max_erg_in is not None:
        caps.append(max_erg_in / 1e9)
    return best_size(path, Market.from_boxes(pool_box, bank_box, oracle_box), max(min(caps), 0.0))


async def run_arb(ns, path: str, erg_in: Optional[int], *, check: bool = False, execute: bool = False,
                  force: bool = False, wait_leg2: bool = True, max_erg_in: Optional[int] = None,
                  log: Callable[[str], None] = print) -> ArbResult:
    """Plan, verify and (optionally) execute one two-leg arbitrage.

    path "redeem": leg 1 pool buy (ERG -> SigUSD), leg 2 bank redeem.
    path "mint":   leg 1 bank mint (ERG -> SigUSD), leg 2 pool sell. `erg_in` is the mint budget.
    erg_in None:   pick the best size (arbitrage/sizing.py) on the boxes this trade spends,
                   capped by `max_erg_in`, the wallet and MAX_TRADE_SIZE_ERG.
    """
    _, planner, describe = PATHS[path]
    node = config.ERGO_NODE_URL
    try:
        (pool_box, _), (bank_box, _), (oracle_box, oracle_pending) = await _fetch_latest(
            ns, (config.SPECTRUM_SIGUSD_POOL_NFT, config.SIGMAUSD_BANK_NFT, config.SIGMAUSD_ORACLE_NFT))
        if oracle_pending:
            if not force:
                raise RuntimeError(ORACLE_PENDING)
            log(f"  --force: ignoring {ORACLE_PENDING.split(' (')[0]}")
        wallet_boxes, height, our_tree = await wallet_context(ns)
        trees = await wallet_trees(ns, node)
        if erg_in is None:
            choice = choose_size(path, pool_box, bank_box, oracle_box, wallet_boxes, max_erg_in)
            log(f"  Best size ({path}, cap {choice.cap_erg:.2f} ERG): {choice.summary()}")
            if not choice.ok:
                if not force:
                    log("  Nothing worth trading right now.")
                    return ArbResult("not_profitable", choice.reason, path=path)
                erg_in = int(config.MIN_TRADE_SIZE_ERG * 1e9)
                log(f"  --force: using MIN_TRADE_SIZE_ERG ({config.MIN_TRADE_SIZE_ERG:g} ERG)")
            else:
                erg_in = choice.size_nanoerg
        plan = planner(pool_box, bank_box, oracle_box, wallet_boxes, erg_in,
                       height=height, our_tree=our_tree, ui_fee_tree=UI_FEE_TREE)
    except (RuntimeError, ValueError, TxGuardError) as e:
        log(f"ABORTED: {e}")
        return ArbResult("aborted", str(e), erg_in or 0, path=path)

    erg_in = plan.get("erg_in", erg_in)
    cents = plan["sigusd_cents"]
    result = ArbResult("planned", erg_in=erg_in, sigusd_cents=cents, path=path,
                       profit_nanoerg=plan["profit_nanoerg"], profit_percent=plan["profit_percent"])
    describe(plan, erg_in, log)
    profitable = plan["profit_percent"] >= config.MIN_PROFIT_PERCENT
    log(f"  Net:   {plan['profit_nanoerg'] / 1e9:+.6f} ERG ({plan['profit_percent']:+.2f}%)  -> "
        f"{'PROFITABLE' if profitable else 'NOT profitable'} (MIN_PROFIT_PERCENT={config.MIN_PROFIT_PERCENT}%)")
    log("")

    leg2_contract_box = bank_box if path == "redeem" else pool_box
    try:
        verify_unsigned_tx(plan["leg2_tx"], [leg2_contract_box, plan["leg1_output_box"]], trees,
                           plan["leg2_policy"])
        log("  Leg 2 TX guard OK (against leg 1's planned output)")
    except TxGuardError as e:
        log(f"  REFUSED: leg 2 fails the TX guard: {e}")
        result.status, result.message = "refused", str(e)
        return result

    if execute and not profitable and not force:
        log("  Not executing: below MIN_PROFIT_PERCENT (use --check to validate for free).")
        result.status = "not_profitable"
        return result

    try:
        signed1 = await guarded_sign(ns, node, plan["leg1_tx"], plan["leg1_policy"], execute=execute or check)
    except DryRun:
        log("  Leg 1 verified, not signed (dry run). Use --check for a node validation, --execute to run.")
        result.status = "dry_run"
        return result
    except TxGuardError as e:
        log(f"  REFUSED by TX guard (leg 1): {e}")
        result.status, result.message = "refused", str(e)
        return result

    if not execute:
        ok, detail = await check_on_node(ns, node, signed1)
        log(f"  Leg 1 node check: {'VALID' if ok else 'REJECTED'}")
        if not ok:
            log(f"  {detail}")
        log("  Leg 2 is validated by the node only once leg 1 is in the mempool (execute mode).")
        log("  Not broadcast (check mode).")
        result.status, result.message = ("checked", "") if ok else ("refused", detail)
        return result

    try:
        result.tx1 = await _submit(ns, signed1)
    except RuntimeError as e:
        log(f"  Leg 1 {e}. Nothing was spent; re-run.")
        result.status, result.message = "leg1_failed", str(e)
        return result
    log(f"  Leg 1 submitted: https://explorer.ergoplatform.com/en/transactions/{result.tx1}")

    leg1_out = tx_output_box(signed1, plan["leg1_output_index"])
    last = {}

    async def build_and_submit_leg2() -> str:
        if path == "redeem":
            bank, oracle = await _leg2_boxes(ns, path)
            tx2, info2, policy2 = build_redeem_leg(bank, oracle, leg1_out, cents, height, our_tree, UI_FEE_TREE)
            erg_back = info2["user_receives"]
        else:
            (pool,) = await _leg2_boxes(ns, path)
            tx2, info2, policy2 = build_pool_sell_leg(pool, leg1_out, cents, height, our_tree)
            erg_back = info2["amount_out"]
        signed2 = await guarded_sign(ns, node, tx2, policy2, execute=True)
        tx_id = await _submit(ns, signed2)
        last.update(erg_back=erg_back, miner_fee=info2["miner_fee"])
        return tx_id

    def fail(message: str) -> ArbResult:
        log(f"  Leg 2 FAILED: {message}")
        log(f"  You now hold {cents / 100:.2f} SigUSD from leg 1. Finish with:")
        log(f"    {result.recover_command}")
        result.status, result.message = "leg2_failed", message
        return result

    try:
        await wait_for_box(ns, leg1_out["boxId"], timeout=60)
        result.tx2 = await build_and_submit_leg2()
    except (RuntimeError, TimeoutError, TxGuardError, ValueError) as e:
        return fail(str(e))
    log(f"  Leg 2 submitted: https://explorer.ergoplatform.com/en/transactions/{result.tx2}")

    if not wait_leg2:
        result.status = "executed"
    else:
        log(f"  Watching leg 2 until confirmed (up to {config.LEG2_WATCH_TIMEOUT_SECONDS // 60} min)...")
        async with aiohttp.ClientSession() as explorer:
            status, result.tx2 = await watch_leg2(
                result.tx2, lambda t: _leg2_status(ns, t, leg1_out["boxId"], explorer), build_and_submit_leg2,
                timeout=config.LEG2_WATCH_TIMEOUT_SECONDS, interval=config.LEG2_WATCH_INTERVAL_SECONDS,
                max_rebuilds=config.LEG2_MAX_REBUILDS, log=log)
        if status != "confirmed":
            return fail(f"leg 2 not confirmed ({status})")
        log(f"  Leg 2 CONFIRMED: https://explorer.ergoplatform.com/en/transactions/{result.tx2}")
        result.status = "executed"

    leg1_miner = plan["leg1_info"]["miner_fee"] if path == "redeem" else 0  # mint cost already includes it
    result.profit_nanoerg = last["erg_back"] - last["miner_fee"] - erg_in - leg1_miner
    result.profit_percent = result.profit_nanoerg / erg_in * 100
    log(f"  Expected net: {result.profit_nanoerg / 1e9:+.6f} ERG")
    return result


async def run_pool_buy_redeem(ns, erg_in: int, **kw) -> ArbResult:
    return await run_arb(ns, "redeem", erg_in, **kw)
