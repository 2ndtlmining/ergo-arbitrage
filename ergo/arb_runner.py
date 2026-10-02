"""Run the two-leg pool buy -> bank redeem arbitrage (used by execute_arb.py and the --live scanner)."""
import asyncio
from dataclasses import dataclass
from typing import Callable, Optional

import aiohttp

import config
from ergo.chain import find_box_id, node_box, wait_for_box, wallet_context
from ergo.chain_arb import build_redeem_leg, plan_pool_buy_redeem, tx_output_box
from ergo.signing import DryRun, check_on_node, guarded_sign, wallet_trees
from ergo.tx_guard import TxGuardError, verify_unsigned_tx

UI_FEE_TREE = "0008cd02c5f61c83056a746a19a9e449e3c9596314cc417a2ef496b7567af558518f2bc7"  # SigmaUSD UI fee
TIMEOUT = aiohttp.ClientTimeout(total=30)


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

    @property
    def recover_command(self) -> str:
        return f"python execute_bank_redeem.py --sigusd {self.sigusd_cents / 100:.2f} --execute"


async def watch_leg2(tx_id: str, get_status, rebuild, *, timeout: float, interval: float,
                     max_rebuilds: int, log: Callable[[str], None]) -> tuple[str, str]:
    """Follow leg 2 until confirmed; rebuild it when it drops out of the mempool.

    get_status(tx_id) -> "confirmed" | "pending" | "dropped" | "leg1_output_spent"
    rebuild() -> new tx id (built on fresh bank/oracle boxes); may raise.
    Returns (status, tx_id): confirmed | gave_up | timeout | leg1_output_spent.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    rebuilds = 0
    while True:
        status = await get_status(tx_id)
        if status in ("confirmed", "leg1_output_spent"):
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
            except (RuntimeError, TimeoutError, TxGuardError, ValueError) as e:
                log(f"  Leg 2 rebuild failed ({e}); retrying next round")
        if loop.time() >= deadline:
            return "timeout", tx_id
        await asyncio.sleep(interval)


async def _leg2_status(ns, tx_id: str, leg1_box_id: str) -> str:
    node = config.ERGO_NODE_URL
    async with ns.get(f"{node}/wallet/transactionById?id={tx_id}", timeout=TIMEOUT) as r:
        if r.status == 200 and ((await r.json()).get("numConfirmations") or 0) >= 1:
            return "confirmed"
    async with ns.get(f"{node}/transactions/unconfirmed/byTransactionId/{tx_id}", timeout=TIMEOUT) as r:
        if r.status == 200:
            return "pending"
    async with ns.get(f"{node}/utxo/withPool/byId/{leg1_box_id}", timeout=TIMEOUT) as r:
        if r.status != 200:
            return "leg1_output_spent"
    return "dropped"


async def _fetch_boxes(ns, nfts) -> list[dict]:
    return [await node_box(ns, await find_box_id(nft, ns)) for nft in nfts]


async def _submit(ns, signed: dict) -> str:
    async with ns.post(f"{config.ERGO_NODE_URL}/transactions", json=signed, timeout=TIMEOUT) as r:
        body = await r.text()
        if r.status != 200:
            raise RuntimeError(f"submit failed: HTTP {r.status} {body[:400]}")
    return signed.get("id", "?")


async def run_pool_buy_redeem(ns, erg_in: int, *, check: bool = False, execute: bool = False,
                              force: bool = False, wait_leg2: bool = True,
                              log: Callable[[str], None] = print) -> ArbResult:
    """Plan, verify and (optionally) execute leg 1 (pool buy) then leg 2 (bank redeem)."""
    node = config.ERGO_NODE_URL
    try:
        pool_box, bank_box, oracle_box = await _fetch_boxes(
            ns, (config.SPECTRUM_SIGUSD_POOL_NFT, config.SIGMAUSD_BANK_NFT, config.SIGMAUSD_ORACLE_NFT))
        wallet_boxes, height, our_tree = await wallet_context(ns)
        trees = await wallet_trees(ns, node)
        plan = plan_pool_buy_redeem(pool_box, bank_box, oracle_box, wallet_boxes, erg_in,
                                    height=height, our_tree=our_tree, ui_fee_tree=UI_FEE_TREE)
    except (RuntimeError, ValueError, TxGuardError) as e:
        log(f"ABORTED: {e}")
        return ArbResult("aborted", str(e), erg_in)

    i1, i2 = plan["leg1_info"], plan["leg2_info"]
    cents = plan["sigusd_cents"]
    result = ArbResult("planned", erg_in=erg_in, sigusd_cents=cents,
                       profit_nanoerg=plan["profit_nanoerg"], profit_percent=plan["profit_percent"])
    log(f"  Leg 1  pool swap:   {erg_in / 1e9:.4f} ERG -> {cents / 100:.2f} SigUSD "
        f"(impact {i1['price_impact_percent']:.2f}%, miner {i1['miner_fee'] / 1e9:.4f})")
    log(f"  Leg 2  bank redeem: {cents / 100:.2f} SigUSD -> {i2['user_receives'] / 1e9:.6f} ERG "
        f"(2% + UI fee {i2['ui_fee'] / 1e9:.4f}, miner {i2['miner_fee'] / 1e9:.4f})")
    profitable = plan["profit_percent"] >= config.MIN_PROFIT_PERCENT
    log(f"  Net:   {plan['profit_nanoerg'] / 1e9:+.6f} ERG ({plan['profit_percent']:+.2f}%)  -> "
        f"{'PROFITABLE' if profitable else 'NOT profitable'} (MIN_PROFIT_PERCENT={config.MIN_PROFIT_PERCENT}%)")
    log("")

    try:
        verify_unsigned_tx(plan["leg2_tx"], [bank_box, plan["leg1_output_box"]], trees, plan["leg2_policy"])
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

    leg1_out = tx_output_box(signed1, 1)
    last_info = {}

    async def build_and_submit_leg2() -> str:
        bank, oracle = await _fetch_boxes(ns, (config.SIGMAUSD_BANK_NFT, config.SIGMAUSD_ORACLE_NFT))
        tx2_unsigned, info2, policy2 = build_redeem_leg(bank, oracle, leg1_out, cents, height,
                                                        our_tree, UI_FEE_TREE)
        signed2 = await guarded_sign(ns, node, tx2_unsigned, policy2, execute=True)
        tx_id = await _submit(ns, signed2)
        last_info.update(info2)
        return tx_id

    def fail(message: str) -> ArbResult:
        log(f"  Leg 2 FAILED: {message}")
        log(f"  You now hold {cents / 100:.2f} SigUSD from leg 1. Finish with:\n    {result.recover_command}")
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
        status, result.tx2 = await watch_leg2(
            result.tx2, lambda t: _leg2_status(ns, t, leg1_out["boxId"]), build_and_submit_leg2,
            timeout=config.LEG2_WATCH_TIMEOUT_SECONDS, interval=config.LEG2_WATCH_INTERVAL_SECONDS,
            max_rebuilds=config.LEG2_MAX_REBUILDS, log=log)
        if status == "confirmed":
            log(f"  Leg 2 CONFIRMED: https://explorer.ergoplatform.com/en/transactions/{result.tx2}")
            result.status = "executed"
        elif status == "leg1_output_spent":
            # Usually leg 2 confirming between checks; the wallet may lag a block behind
            log("  Leg 1's output is spent; leg 2 most likely confirmed. Check the explorer link above.")
            result.status = "executed"
        else:
            return fail(f"leg 2 not confirmed ({status})")

    result.profit_nanoerg = last_info["user_receives"] - last_info["miner_fee"] - erg_in - i1["miner_fee"]
    result.profit_percent = result.profit_nanoerg / erg_in * 100
    log(f"  Expected net: {result.profit_nanoerg / 1e9:+.6f} ERG")
    return result
