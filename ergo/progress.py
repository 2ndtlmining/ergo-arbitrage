"""Readable progress while a transaction waits for a block."""
import asyncio
from typing import Callable

import aiohttp

import config

TIMEOUT = aiohttp.ClientTimeout(total=15)


def fmt_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m{seconds % 60:02d}s"


async def _height(ns) -> int:
    try:
        async with ns.get(f"{config.ERGO_NODE_URL}/info", timeout=TIMEOUT) as r:
            return (await r.json()).get("fullHeight", 0) if r.status == 200 else 0
    except (asyncio.TimeoutError, aiohttp.ClientError):
        return 0


async def _confirmations(ns, tx_id: str):
    """Confirmations per the node wallet, or None if the wallet doesn't know the TX yet."""
    try:
        async with ns.get(f"{config.ERGO_NODE_URL}/wallet/transactionById?id={tx_id}", timeout=TIMEOUT) as r:
            if r.status != 200:
                return None
            return (await r.json()).get("numConfirmations") or 0
    except (asyncio.TimeoutError, aiohttp.ClientError):
        return None


async def wait_confirmed(ns, tx_id: str, *, log: Callable[[str], None], timeout: float = 900,
                         interval: float = 15) -> bool:
    """Poll until `tx_id` has a confirmation, logging height and progress each check."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    log("  Waiting for confirmation. Ergo blocks come every ~2 min on average, "
        "so this usually takes 1-4 min.")
    start_height = last_height = None
    while True:
        confs = await _confirmations(ns, tx_id)
        height = await _height(ns)
        elapsed = fmt_elapsed(loop.time() - start)
        if confs and confs >= 1:
            log(f"  [{elapsed}] CONFIRMED in block {height - confs + 1} "
                f"({confs} confirmation{'s' if confs > 1 else ''}).")
            return True
        if start_height is None:
            start_height = height
        state = "seen by your node, waiting for a miner" if confs == 0 else "submitted, waiting for a miner"
        new = " - new block, not included yet" if last_height is not None and height > last_height else ""
        log(f"  [{elapsed}] height {height}{new}: {state} ({height - start_height} block(s) since submit)")
        last_height = height
        if loop.time() - start >= timeout:
            log(f"  Not confirmed after {fmt_elapsed(timeout)}. It may still confirm: check the explorer "
                f"https://explorer.ergoplatform.com/en/transactions/{tx_id}")
            return False
        await asyncio.sleep(interval)
