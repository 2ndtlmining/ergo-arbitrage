"""`python arb.py doctor`: is this node ready for the bot? (written for the move to the Rust node)

Checks, in order, everything the bot needs from the node and prints one line per check:
OK, WARN, FAIL (with a hint) or SKIP (an earlier check failed). The last check signs a 1 ERG
pool buy -> bank redeem and has the node validate it; that transaction is never broadcast.
Returns 1 when any check fails, 0 otherwise.
"""
import asyncio
from typing import Callable

import aiohttp

import config
from ergo import chain_state
from ergo.arb_runner import run_arb
from ergo.chain import wallet_context
from ergo.chain_state import prices_from_snapshot, read_snapshot
from exchanges.ergo_node import MAX_HEADER_LAG

TIMEOUT = aiohttp.ClientTimeout(total=10)
NETWORK_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, OSError)


async def _get(ns, path: str):
    async with ns.get(f"{config.ERGO_NODE_URL}{path}", timeout=TIMEOUT) as r:
        return r.status, (await r.json(content_type=None) if r.status in (200, 400, 401, 403) else None)


async def run_doctor(ns, sign: bool = True, log: Callable[[str], None] = print) -> int:
    failed = False

    def line(status: str, name: str, detail: str = "", hint: str = ""):
        nonlocal failed
        failed |= status == "FAIL"
        log(f"{status:<5} {name:<16} {detail}")
        if hint:
            log(f"{'':22}-> {hint}")

    def skip(*names):
        for n in names:
            line("SKIP", n, "an earlier check failed")

    log(f"Node {config.ERGO_NODE_URL}")
    later = ("API key", "wallet", "wallet boxes", "chain state", "mempool lookup", "sign check")

    # 1-2: reachable and synced
    try:
        status, info = await _get(ns, "/info")
    except NETWORK_ERRORS as e:
        line("FAIL", "node reachable", f"{e.__class__.__name__}: {e}"[:110],
             "is the node running? Is its REST API bound to this address and not only 127.0.0.1? "
             "Is ERGO_NODE_URL right?")
        skip("synced", *later)
        return 1
    if status != 200 or not info:
        line("FAIL", "node reachable", f"/info answered HTTP {status}", "check ERGO_NODE_URL points at the node's REST API")
        skip("synced", *later)
        return 1
    height, headers = info.get("fullHeight") or 0, info.get("headersHeight") or 0
    line("OK", "node reachable", f"{info.get('name', '?')} {info.get('appVersion', '')} height {height}".strip())
    behind = headers - height
    if height and behind < MAX_HEADER_LAG:
        line("OK", "synced", f"headers {headers}")
    else:
        line("WARN", "synced", f"{behind} blocks behind (height {height}, headers {headers})",
             "wait for the sync to finish; the bot reads stale data until then and live mode will not trade")

    # 3-4: API key and wallet
    try:
        status, wallet = await _get(ns, "/wallet/status")
    except NETWORK_ERRORS as e:
        line("FAIL", "API key", f"{e.__class__.__name__}: {e}"[:110])
        skip(*later[1:])
        return 1
    if status in (401, 403):
        line("FAIL", "API key", f"/wallet/status answered HTTP {status}",
             "set ERGO_NODE_API_KEY in .env to the secret whose hash is the node's apiKeyHash")
        skip(*later[1:])
        return 1
    if status != 200 or wallet is None:
        line("FAIL", "API key", f"/wallet/status answered HTTP {status}", "does this node have the wallet API enabled?")
        skip(*later[1:])
        return 1
    line("OK", "API key", "accepted")
    if not wallet.get("isInitialized"):
        line("FAIL", "wallet", "no wallet on this node", "restore your wallet on the node (mnemonic), then unlock it")
        skip(*later[2:])
        return 1
    if not wallet.get("isUnlocked"):
        line("FAIL", "wallet", "locked", 'unlock it: POST /wallet/unlock with {"pass": "<wallet password>"}')
        skip(*later[2:])
        return 1
    line("OK", "wallet", "initialised and unlocked")

    # 5: wallet boxes
    erg = 0.0
    try:
        boxes, _, _ = await wallet_context(ns)
        erg = sum(int(b["value"]) for b in boxes) / 1e9
        line("OK", "wallet boxes", f"{len(boxes)} boxes, {erg:.2f} ERG")
    except Exception as e:
        line("FAIL", "wallet boxes", f"{e.__class__.__name__}: {e}"[:110], "the wallet may still be scanning after a restore")

    # 6: chain state through the extra index
    try:
        snap = await read_snapshot(ns)
        bank = prices_from_snapshot(snap)["bank"]
        line("OK", "chain state", f"pool, bank, oracle read in {snap.read_ms:.0f} ms, RR {bank['reserve_ratio']:.0f}%")
    except Exception as e:
        line("FAIL", "chain state", f"{e.__class__.__name__}: {e}"[:110],
             "the bot finds the pool/bank/oracle boxes through the node's extra index "
             "(/blockchain/box/unspent/byTokenId): enable extraIndex and let it build")

    # 7: mempool lookup (GET on the Scala node, POST on the Rust node)
    try:
        await chain_state._mempool_outputs_by_token(ns, config.SPECTRUM_SIGUSD_POOL_NFT)
        form = ("POST /transactions/unconfirmed/byTokenId (Rust node API)" if chain_state._MEMPOOL_BY_TOKEN_POST
                else "GET /transactions/unconfirmed/outputs/byTokenId")
        line("OK", "mempool lookup", form)
    except Exception as e:
        line("FAIL", "mempool lookup", f"{e.__class__.__name__}: {e}"[:110],
             "without it the bot cannot see pending pool/bank/oracle changes")

    # 8: sign and validate, never broadcast
    if not sign:
        line("SKIP", "sign check", "--no-sign")
    elif failed:
        skip("sign check")
    elif erg < config.MIN_TRADE_SIZE_ERG + config.LIVE_ERG_RESERVE:
        line("SKIP", "sign check", f"wallet holds {erg:.2f} ERG; needs {config.MIN_TRADE_SIZE_ERG + config.LIVE_ERG_RESERVE:g}")
    else:
        try:
            result = await run_arb(ns, "redeem", int(config.MIN_TRADE_SIZE_ERG * 1e9), check=True, execute=False,
                                   log=lambda m: None)
        except Exception as e:
            line("FAIL", "sign check", f"{e.__class__.__name__}: {e}"[:110])
        else:
            if result.status == "checked":
                line("OK", "sign check", f"{config.MIN_TRADE_SIZE_ERG:g} ERG pool buy signed and validated by the node "
                                         "(checked, not broadcast)")
            elif result.status == "aborted":
                line("WARN", "sign check", result.message[:110], "retry in a minute (e.g. an oracle update was pending)")
            else:
                line("FAIL", "sign check", f"{result.status}: {result.message}"[:110],
                     "the node could not sign or validate a bot transaction")
    return 1 if failed else 0
