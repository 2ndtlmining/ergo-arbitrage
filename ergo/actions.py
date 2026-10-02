"""Wallet actions behind arb.py: balance, quote, swap, redeem, send, arb.

Every action explains each step, verifies the transaction with the TX guard,
and only signs/submits when asked (mode "check" signs and validates on the node
without broadcasting; "execute" submits and then follows the TX until confirmed).
"""
from typing import Callable, Optional

import aiohttp

import config
from ergo.amounts import erg_spendable, token_total
from ergo.arb_runner import run_pool_buy_redeem
from ergo.chain import find_box_id, node_box, wallet_context
from ergo.chain_arb import redeem_policy
from ergo.pool_swap import MINER_FEE, build_pool_swap_tx
from ergo.progress import wait_confirmed
from ergo.sigmausd_tx import build_redeem_tx, register_int
from ergo.signing import DryRun, check_on_node, guarded_sign
from ergo.tx_guard import SignPolicy, TxGuardError
from exchanges.sigmausd import BankState, quote_redeem_sigusd

Log = Callable[[str], None]
NODE_TIMEOUT = aiohttp.ClientTimeout(total=30)
UI_FEE_TREE = "0008cd02c5f61c83056a746a19a9e449e3c9596314cc417a2ef496b7567af558518f2bc7"
SIGUSD = config.SIGUSD_TOKEN_ID


# ---------- pure helpers (unit tested) ----------

def payment_request(address: str, erg_nano: int, cents: int) -> dict:
    """Body for the node's /wallet/transaction/generateUnsigned."""
    assets = [{"tokenId": SIGUSD, "amount": cents}] if cents else []
    value = max(erg_nano, config.ERG_MIN_BOX_NANO if assets else erg_nano)
    return {"requests": [{"address": address, "value": value, "assets": assets}],
            "fee": MINER_FEE, "inputsRaw": [], "dataInputsRaw": []}


def send_policy(payee_tree: str, erg_nano: int, cents: int) -> SignPolicy:
    """Only `payee_tree` may be paid, and only these amounts; everything else returns to the wallet."""
    allowed = {"ERG": erg_nano}
    if cents:
        allowed[SIGUSD] = cents
    return SignPolicy(max_erg_spent=erg_nano + MINER_FEE, max_token_spent={SIGUSD: cents} if cents else {},
                      max_service_fee=0, payees={payee_tree: allowed})


def balance_lines(confirmed: dict, unconfirmed: dict, oracle_usd_per_erg: Optional[float],
                  pool_sigusd_per_erg: Optional[float], reserve_ratio: Optional[float],
                  stale: Optional[dict] = None) -> list[str]:
    erg, sig = confirmed.get("erg", 0.0), confirmed.get("sigusd", 0.0)
    lines = [f"  ERG:     {erg:,.4f} ERG", f"  SigUSD:  {sig:,.2f} SigUSD"]
    d_erg, d_sig = unconfirmed.get("erg", erg) - erg, unconfirmed.get("sigusd", sig) - sig
    if abs(d_erg) > 1e-9 or abs(d_sig) > 1e-9:
        lines.append(f"  Pending (unconfirmed): {d_erg:+,.4f} ERG, {d_sig:+,.2f} SigUSD")
    if stale and stale.get("count"):
        lines.append(f"  Ignored: {stale['erg']:,.4f} ERG, {stale['sigusd']:,.2f} SigUSD that your node wallet still "
                     f"lists from {stale['count']} box(es) of transactions that are no longer valid "
                     f"(they will never confirm and are not spendable)")
    if oracle_usd_per_erg:
        total_erg = erg + sig / oracle_usd_per_erg
        lines.append(f"  Value:   ~{total_erg:,.4f} ERG / ~${total_erg * oracle_usd_per_erg:,.2f} (oracle "
                     f"${oracle_usd_per_erg:.4f}/ERG)")
    if oracle_usd_per_erg and pool_sigusd_per_erg:
        lines.append(f"  SigUSD on the pool: ${oracle_usd_per_erg / pool_sigusd_per_erg:.2f} "
                     f"({(oracle_usd_per_erg / pool_sigusd_per_erg - 1) * 100:+.1f}% vs $1)")
    if reserve_ratio:
        lines.append(f"  Bank:    RR {reserve_ratio:.0f}% (redeem always allowed, mint "
                     f"{'allowed' if reserve_ratio >= 400 else 'blocked below 400%'})")
    return lines


# ---------- node I/O ----------

async def _boxes(ns, *nfts) -> list[dict]:
    return [await node_box(ns, await find_box_id(nft, ns)) for nft in nfts]


async def _get(ns, path: str):
    async with ns.get(f"{config.ERGO_NODE_URL}{path}", timeout=NODE_TIMEOUT) as r:
        return await r.json() if r.status == 200 else None


async def _submit(ns, signed: dict) -> str:
    async with ns.post(f"{config.ERGO_NODE_URL}/transactions", json=signed, timeout=NODE_TIMEOUT) as r:
        body = await r.text()
        if r.status != 200:
            raise RuntimeError(f"submit failed: HTTP {r.status} {body[:400]}")
    return signed.get("id", "?")


async def _finish(ns, tx: dict, policy: SignPolicy, mode: str, log: Log) -> Optional[str]:
    """Guard -> (sign) -> (node check | submit + follow until confirmed)."""
    log("Step 3: checking the transaction with the TX guard...")
    try:
        signed = await guarded_sign(ns, config.ERGO_NODE_URL, tx, policy, execute=mode != "dry")
    except DryRun:
        log("  Dry run: verified, nothing signed. Add --check to have your node validate it, "
            "or --execute to send it.")
        return None
    except TxGuardError as e:
        log(f"  REFUSED by the TX guard: {e}")
        return None
    if mode == "check":
        ok, detail = await check_on_node(ns, config.ERGO_NODE_URL, signed)
        log(f"  Node check: {'VALID - the network would accept this transaction' if ok else 'REJECTED'}")
        if not ok:
            log(f"  {detail}")
        log("  Not broadcast (check mode).")
        return None
    log("Step 4: submitting to your node...")
    try:
        tx_id = await _submit(ns, signed)
    except RuntimeError as e:
        log(f"  {e}. Nothing was spent; you can re-run.")
        return None
    log(f"  Submitted: https://explorer.ergoplatform.com/en/transactions/{tx_id}")
    await wait_confirmed(ns, tx_id, log=log)
    return tx_id


async def balance(ns, log: Log):
    log("Reading your wallet and the SigmaUSD/pool state from your node...")
    conf = await _get(ns, "/wallet/balances") or {}
    stale_boxes: list[dict] = []
    spendable, _, _ = await wallet_context(ns, on_stale=stale_boxes.extend)

    def summary(b):
        assets = b.get("assets", {}) or {}
        return {"erg": b.get("balance", 0) / 1e9, "sigusd": assets.get(SIGUSD, 0) / 100}

    def from_boxes(boxes):
        return {"erg": sum(int(x["value"]) for x in boxes) / 1e9, "sigusd": token_total(boxes, SIGUSD) / 100}

    stale = dict(from_boxes(stale_boxes), count=len(stale_boxes))

    oracle = pool_spot = rr = None
    try:
        pool, bank, oracle_box = await _boxes(ns, config.SPECTRUM_SIGUSD_POOL_NFT, config.SIGMAUSD_BANK_NFT,
                                              config.SIGMAUSD_ORACLE_NFT)
        state = BankState(int(bank["value"]), register_int(bank, "R4"), register_int(oracle_box, "R4"))
        oracle, rr = state.oracle_usd_per_erg, state.reserve_ratio
        y = next(int(a["amount"]) for a in pool["assets"] if a["tokenId"] == SIGUSD)
        pool_spot = (y / 100) / (int(pool["value"]) / 1e9)
    except (RuntimeError, StopIteration, KeyError) as e:
        log(f"  (market data unavailable: {e})")
    for line in balance_lines(summary(conf), from_boxes(spendable), oracle, pool_spot, rr, stale):
        log(line)


async def quote(ns, sell: str, amount: float, log: Log):
    log("Reading the pool and bank boxes from your node...")
    pool, bank, oracle_box = await _boxes(ns, config.SPECTRUM_SIGUSD_POOL_NFT, config.SIGMAUSD_BANK_NFT,
                                          config.SIGMAUSD_ORACLE_NFT)
    state = BankState(int(bank["value"]), register_int(bank, "R4"), register_int(oracle_box, "R4"))
    wallet = [{"boxId": "q", "value": 10**15, "ergoTree": "0008cd02" + "00" * 32,
               "assets": [{"tokenId": SIGUSD, "amount": 10**12}]}]
    if sell == "erg":
        amt = int(round(amount * 1e9))
        _, info = build_pool_swap_tx(pool, wallet, SIGUSD, sell_erg=True, amount_in=amt, height=0,
                                     our_tree=wallet[0]["ergoTree"])
        log(f"  Pool (direct, no service fee): {amount:g} ERG -> {info['amount_out'] / 100:.2f} SigUSD "
            f"(impact {info['price_impact_percent']:.2f}%)")
        log(f"  Bank mint: {'blocked (RR below 400%)' if state.reserve_ratio < 400 else 'allowed'}")
    else:
        cents = int(round(amount * 100))
        _, info = build_pool_swap_tx(pool, wallet, SIGUSD, sell_erg=False, amount_in=cents, height=0,
                                     our_tree=wallet[0]["ergoTree"])
        pool_erg = (info["amount_out"] - MINER_FEE) / 1e9
        bank_erg = (quote_redeem_sigusd(state, cents) - MINER_FEE) / 1e9
        best = "pool" if pool_erg > bank_erg else "bank"
        log(f"  Pool (direct, no service fee): {amount:g} SigUSD -> {pool_erg:.6f} ERG (after miner fee)")
        log(f"  Bank redeem:                   {amount:g} SigUSD -> {bank_erg:.6f} ERG (after 2% + UI + miner fee)")
        log(f"  Better: {best}. Use `arb.py {'swap --sell sigusd' if best == 'pool' else 'redeem --sigusd'} "
            f"{amount:g}`")


async def swap(ns, sell: str, amount, mode: str, log: Log):
    sell_erg = sell == "erg"
    log("Step 1: reading the pool box and your wallet from your node...")
    try:
        (pool,) = await _boxes(ns, config.SPECTRUM_SIGUSD_POOL_NFT)
    except RuntimeError as e:
        log(f"  ABORTED: {e}")
        return
    wallet_boxes, height, our_tree = await wallet_context(ns)
    if amount == "all":
        amount_in = erg_spendable(wallet_boxes, MINER_FEE) if sell_erg else token_total(wallet_boxes, SIGUSD)
        log(f"  'all' = {amount_in / (1e9 if sell_erg else 100):,.4f} {'ERG' if sell_erg else 'SigUSD'}")
    else:
        amount_in = int(round(amount * (1e9 if sell_erg else 100)))
    log("Step 2: building the swap against the pool box...")
    try:
        tx, info = build_pool_swap_tx(pool, wallet_boxes, SIGUSD, sell_erg=sell_erg, amount_in=amount_in,
                                      height=height, our_tree=our_tree)
    except ValueError as e:
        log(f"  ABORTED: {e}")
        return
    out = info["amount_out"]
    if sell_erg:
        log(f"  You send {amount_in / 1e9:,.4f} ERG (+{MINER_FEE / 1e9} miner fee), receive {out / 100:,.2f} SigUSD")
        policy = SignPolicy(max_erg_spent=amount_in + MINER_FEE, min_received={SIGUSD: out}, max_service_fee=0)
    else:
        log(f"  You send {amount_in / 100:,.2f} SigUSD, receive {out / 1e9:,.6f} ERG (minus {MINER_FEE / 1e9} miner fee)")
        policy = SignPolicy(max_erg_spent=0, max_token_spent={SIGUSD: amount_in},
                            min_erg_received=out - MINER_FEE, max_service_fee=0)
    log(f"  Price impact {info['price_impact_percent']:.2f}% (includes the 0.5% pool fee), no service fee")
    await _finish(ns, tx, policy, mode, log)


async def redeem(ns, sigusd, mode: str, log: Log):
    log("Step 1: reading the bank and oracle boxes and your wallet from your node...")
    try:
        bank, oracle_box = await _boxes(ns, config.SIGMAUSD_BANK_NFT, config.SIGMAUSD_ORACLE_NFT)
    except RuntimeError as e:
        log(f"  ABORTED: {e}")
        return
    wallet_boxes, height, our_tree = await wallet_context(ns)
    cents = token_total(wallet_boxes, SIGUSD) if sigusd == "all" else int(round(sigusd * 100))
    if sigusd == "all":
        log(f"  'all' = {cents / 100:,.2f} SigUSD")
    log("Step 2: building the redeem (contract-exact bank math)...")
    try:
        tx, info = build_redeem_tx(bank, oracle_box, wallet_boxes, cents, height=height, our_tree=our_tree,
                                   ui_fee_tree=UI_FEE_TREE)
    except ValueError as e:
        log(f"  ABORTED: {e}")
        return
    state = info["state"]
    log(f"  Oracle 1 USD = {state.oracle_r4 / 1e9:.6f} ERG, RR {state.reserve_ratio:.0f}% (redeem always allowed)")
    log(f"  {cents / 100:,.2f} SigUSD -> {(info['user_receives'] - info['miner_fee']) / 1e9:,.6f} ERG "
        f"(after 2% bank fee, {info['ui_fee'] / 1e9:.4f} UI fee, {info['miner_fee'] / 1e9} miner fee)")
    await _finish(ns, tx, redeem_policy(info, cents, UI_FEE_TREE), mode, log)


async def send(ns, address: str, erg: float, sigusd: float, mode: str, log: Log):
    log(f"Step 1: checking the destination address {address}...")
    valid = await _get(ns, f"/utils/address/{address}")
    if not valid or not valid.get("isValid"):
        log("  ABORTED: not a valid Ergo address.")
        return
    tree = (await _get(ns, f"/script/addressToTree/{address}") or {}).get("tree")
    if not tree:
        log("  ABORTED: could not resolve the address.")
        return
    erg_nano, cents = int(round((erg or 0) * 1e9)), int(round((sigusd or 0) * 100))
    req = payment_request(address, erg_nano, cents)
    erg_nano = req["requests"][0]["value"]
    log(f"  SENDING to {address}: {erg_nano / 1e9:,.4f} ERG"
        f"{f' and {cents / 100:,.2f} SigUSD' if cents else ''} (+{MINER_FEE / 1e9} miner fee)")
    log("Step 2: asking your node to build the payment...")
    async with ns.post(f"{config.ERGO_NODE_URL}/wallet/transaction/generateUnsigned", json=req,
                       timeout=NODE_TIMEOUT) as r:
        body = await r.json() if r.status == 200 else None
        if body is None:
            log(f"  ABORTED: the node could not build it (HTTP {r.status}: {(await r.text())[:300]})")
            return
    await _finish(ns, body, send_policy(tree, erg_nano, cents), mode, log)


async def arb(ns, erg: float, mode: str, force: bool, log: Log):
    await run_pool_buy_redeem(ns, int(round(erg * 1e9)), check=mode == "check", execute=mode == "execute",
                              force=force, log=log)
