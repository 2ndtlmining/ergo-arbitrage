"""Newest unspent pool, bank and oracle boxes as the node sees them, mempool included.

The scanner and the runner both read contract boxes through latest_box(), so the
state the scanner prices is the state the runner's transactions spend. A pending
mempool output holding the NFT wins over the confirmed box: spending the confirmed
box would conflict with that pending transaction, chaining onto it does not.

That applies to boxes our transactions spend as inputs (pool, bank). The oracle box
is only a data input of the bank transaction, and whether the node accepts a pending
box there is unverified. So the oracle always comes from the confirmed UTXO set, and
a pending oracle update is only reported (pending=True): the live gate waits for it
to confirm, since a bank TX on the old oracle box would be dropped once it does.
"""
import asyncio
import time
from dataclasses import dataclass

import aiohttp

import config
from ergo.chain import find_box_id, node_box

CHAIN_TIMEOUT = aiohttp.ClientTimeout(total=5)
PENDING_OK = frozenset({config.SPECTRUM_SIGUSD_POOL_NFT, config.SIGMAUSD_BANK_NFT})  # spent as inputs
CONTRACTS = (("pool", config.SPECTRUM_SIGUSD_POOL_NFT), ("bank", config.SIGMAUSD_BANK_NFT),
             ("oracle", config.SIGMAUSD_ORACLE_NFT))


async def _get(ns, path: str):
    """(status, json or None) for a node GET."""
    async with ns.get(f"{config.ERGO_NODE_URL}{path}", timeout=CHAIN_TIMEOUT) as r:
        return r.status, (await r.json() if r.status == 200 else None)


async def _pending_outputs(ns, nft: str) -> list[dict]:
    """Mempool outputs holding `nft` that no other mempool TX has spent yet."""
    status, outputs = await _get(ns, f"/transactions/unconfirmed/outputs/byTokenId/{nft}")
    live = []
    for out in (outputs or []) if status == 200 else []:
        st, b = await _get(ns, f"/utxo/withPool/byId/{out['boxId']}")
        if st == 200 and b:
            live.append(b)
    return live


async def _confirmed_box(ns, box_id: str) -> dict:
    """Box from the confirmed UTXO set only (ignores mempool spends)."""
    status, b = await _get(ns, f"/utxo/byId/{box_id}")
    if status != 200 or not b:
        raise RuntimeError(f"box {box_id[:12]} is not in the confirmed UTXO set; retry next block")
    return b


async def latest_box(ns, nft: str, explorer=None) -> tuple[dict, bool]:
    """(box, pending) for `nft`'s contract box.

    Pool/bank (PENDING_OK): the newest mempool output still unspent in the pool, with
    pending=True; otherwise the confirmed box. Oracle: always the confirmed box;
    pending=True means an update is waiting in the mempool.
    The confirmed box id comes from the node index, then the explorer.
    Raises RuntimeError if the box cannot be found.
    """
    pending = await _pending_outputs(ns, nft)
    if nft in PENDING_OK:
        if pending:
            return pending[-1], True
        return await node_box(ns, await find_box_id(nft, ns, explorer)), False
    return await _confirmed_box(ns, await find_box_id(nft, ns, explorer)), bool(pending)


@dataclass(frozen=True)
class ChainSnapshot:
    height: int
    pool: dict
    bank: dict
    oracle: dict
    pending: frozenset
    read_ms: float

    @property
    def key(self) -> tuple[str, str, str]:
        return self.pool["boxId"], self.bank["boxId"], self.oracle["boxId"]


async def _height(ns) -> int:
    status, info = await _get(ns, "/info")
    if status != 200 or not info:
        raise RuntimeError(f"node /info returned HTTP {status}")
    return int(info.get("fullHeight") or 0)


async def read_snapshot(ns, explorer=None) -> ChainSnapshot:
    """Pool, bank and oracle boxes plus height, read in parallel."""
    start = time.perf_counter()
    *found, height = await asyncio.gather(*(latest_box(ns, nft, explorer) for _, nft in CONTRACTS), _height(ns))
    boxes = {name: b for (name, _), (b, _) in zip(CONTRACTS, found)}
    pending = frozenset(name for (name, _), (_, p) in zip(CONTRACTS, found) if p)
    return ChainSnapshot(height, boxes["pool"], boxes["bank"], boxes["oracle"], pending,
                         (time.perf_counter() - start) * 1000)
