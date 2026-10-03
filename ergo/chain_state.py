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
import json
import logging
import time
from dataclasses import dataclass

import aiohttp

import config
from ergo.chain import find_box_id, node_box
from ergo.sigmausd_tx import register_int
from exchanges.sigmausd import BankState, can_mint_sigusd
from exchanges.spectrum import parse_n2t_pool_box

CHAIN_TIMEOUT = aiohttp.ClientTimeout(total=5)
logger = logging.getLogger("ergo_arb.chain_state")

# Mempool outputs by token: the Scala node answers GET /transactions/unconfirmed/outputs/byTokenId/{id};
# the Rust node (arkadianet/ergo) only has POST /transactions/unconfirmed/byTokenId (body: the token id as a
# JSON string, answer: pool transactions). None = not known yet; True once the GET form answered 404.
_MEMPOOL_BY_TOKEN_POST = None
PENDING_OK = frozenset({config.SPECTRUM_SIGUSD_POOL_NFT, config.SIGMAUSD_BANK_NFT})  # spent as inputs
_BOX_IDS: dict[str, str] = {}  # NFT -> last confirmed box id (saves an index/explorer lookup per poll)
CONTRACTS = (("pool", config.SPECTRUM_SIGUSD_POOL_NFT), ("bank", config.SIGMAUSD_BANK_NFT),
             ("oracle", config.SIGMAUSD_ORACLE_NFT))


async def _get(ns, path: str):
    """(status, json or None) for a node GET."""
    async with ns.get(f"{config.ERGO_NODE_URL}{path}", timeout=CHAIN_TIMEOUT) as r:
        return r.status, (await r.json() if r.status == 200 else None)


async def _mempool_outputs_by_token(ns, nft: str) -> list[dict]:
    """Every mempool output holding `nft`, from whichever form of the lookup this node supports."""
    global _MEMPOOL_BY_TOKEN_POST
    if not _MEMPOOL_BY_TOKEN_POST:
        status, outputs = await _get(ns, f"/transactions/unconfirmed/outputs/byTokenId/{nft}")
        if status != 404:
            _MEMPOOL_BY_TOKEN_POST = False
            return outputs or [] if status == 200 else []
        if _MEMPOOL_BY_TOKEN_POST is None:
            logger.info("Node has no GET /transactions/unconfirmed/outputs/byTokenId; "
                        "using POST /transactions/unconfirmed/byTokenId (Rust node API)")
        _MEMPOOL_BY_TOKEN_POST = True
    async with ns.post(f"{config.ERGO_NODE_URL}/transactions/unconfirmed/byTokenId", data=json.dumps(nft),
                       headers={"Content-Type": "application/json"}, timeout=CHAIN_TIMEOUT) as r:
        if r.status == 404:            # neither form here: try the GET form again next time
            _MEMPOOL_BY_TOKEN_POST = None
            return []
        txs = await r.json() if r.status == 200 else []
    return [o for tx in txs or [] for o in tx.get("outputs", [])
            if any(a.get("tokenId") == nft for a in o.get("assets", []))]


async def _pending_outputs(ns, nft: str) -> list[dict]:
    """Mempool outputs holding `nft` that no other mempool TX has spent yet."""
    live = []
    for out in await _mempool_outputs_by_token(ns, nft):
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
        try:
            return await _confirmed(ns, nft, explorer, node_box), False
        except RuntimeError:
            # Spent by a TX that reached the mempool after the pending check: chain onto it.
            pending = await _pending_outputs(ns, nft)
            if pending:
                return pending[-1], True
            raise
    # An update arriving between the two calls is reported as not pending; the runner
    # re-reads (and re-checks) right before it signs anything.
    return await _confirmed(ns, nft, explorer, _confirmed_box), bool(pending)


async def _confirmed(ns, nft: str, explorer, read) -> dict:
    """Confirmed box for `nft` read with `read(ns, box_id)`, via the cached id when it is still unspent."""
    cached = _BOX_IDS.get(nft)
    if cached:
        try:
            return await read(ns, cached)
        except RuntimeError:
            _BOX_IDS.pop(nft, None)  # spent: look the new one up
    box_id = await find_box_id(nft, ns, explorer)
    b = await read(ns, box_id)
    _BOX_IDS[nft] = box_id
    return b


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


def prices_from_snapshot(snap: ChainSnapshot) -> dict:
    """The scanner's on-chain price entries, from exactly the boxes in `snap`."""
    pool = parse_n2t_pool_box(snap.pool, config.SIGUSD_TOKEN_ID, config.SIGUSD_DECIMALS,
                              fee_num=register_int(snap.pool, "R4"), exchange="ErgoDEX pool (node)")
    state = BankState(int(snap.bank["value"]), register_int(snap.bank, "R4"), register_int(snap.oracle, "R4"))
    return {
        "spectrum_pool": pool,
        "spectrum_erg_sigusd": pool.price_x_in_y,
        "bank": {
            "oracle_erg_usd": state.oracle_usd_per_erg,
            "bank_erg_reserve": state.bank_erg_nano / 1e9,
            "sigusd_circulating": state.sigusd_circ_cents / 100,
            "reserve_ratio": state.reserve_ratio,
            "can_mint_sigusd": can_mint_sigusd(state, 1),
            "can_redeem_sigusd": True,
            "state": state,
        },
    }
