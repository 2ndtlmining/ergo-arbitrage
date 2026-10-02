"""Chain/node reads shared by the execute scripts."""
import aiohttp

import config
from ergo.signing import wallet_trees

TIMEOUT = aiohttp.ClientTimeout(total=30)


async def explorer_box_id(s, token_id: str) -> str:
    """Id of the unspent box holding `token_id` (e.g. a pool or bank NFT), per the explorer."""
    url = f"{config.ERGO_EXPLORER_API_URL}/boxes/unspent/byTokenId/{token_id}?limit=1"
    async with s.get(url, timeout=TIMEOUT) as r:
        data = await r.json()
    items = data.get("items", data) if isinstance(data, dict) else data
    if not items:
        raise RuntimeError(f"no unspent box found for token {token_id[:8]}")
    return items[0]["boxId"]


async def node_box(ns, box_id: str) -> dict:
    """Box as the node sees it (UTXO set + mempool). Fails if the explorer was behind."""
    async with ns.get(f"{config.ERGO_NODE_URL}/utxo/withPool/byId/{box_id}", timeout=TIMEOUT) as r:
        if r.status != 200:
            raise RuntimeError(
                f"box {box_id[:12]} is not unspent on the node (explorer lagging or already spent); retry next block"
            )
        return await r.json()


async def _visible_on_node(ns, box_id: str) -> bool:
    async with ns.get(f"{config.ERGO_NODE_URL}/utxo/withPool/byId/{box_id}", timeout=TIMEOUT) as r:
        return r.status == 200


async def wallet_context(ns, on_stale=None) -> tuple[list[dict], int, str]:
    """(spendable P2PK wallet boxes, current height, ErgoTree of the wallet's first address).

    The node wallet can keep listing outputs of unconfirmed transactions that became
    invalid (e.g. a leg 2 whose oracle box was replaced). Only boxes the node can
    actually spend from (UTXO set or a valid pending TX, via /utxo/withPool) are
    returned; the rest are passed to `on_stale` if given.
    """
    import asyncio
    node = config.ERGO_NODE_URL
    async with ns.get(f"{node}/wallet/boxes/unspent?minConfirmations=0&minInclusionHeight=0", timeout=TIMEOUT) as r:
        boxes = [wb["box"] for wb in await r.json()]
    async with ns.get(f"{node}/info", timeout=TIMEOUT) as r:
        height = (await r.json()).get("fullHeight", 0)
    async with ns.get(f"{node}/wallet/addresses", timeout=TIMEOUT) as r:
        first_address = (await r.json())[0]
    async with ns.get(f"{node}/utils/addressToRaw/{first_address}", timeout=TIMEOUT) as r:
        our_tree = "0008cd" + (await r.json())["raw"]
    trees = await wallet_trees(ns, node)
    mine = [b for b in boxes if b.get("ergoTree") in trees]
    visible = await asyncio.gather(*(_visible_on_node(ns, b["boxId"]) for b in mine))
    stale = [b for b, ok in zip(mine, visible) if not ok]
    if stale and on_stale:
        on_stale(stale)
    return [b for b, ok in zip(mine, visible) if ok], height, our_tree


async def wait_for_box(ns, box_id: str, timeout: float = 60, interval: float = 1.0) -> dict:
    """Poll the node until `box_id` is visible (UTXO set or mempool); TimeoutError otherwise."""
    import asyncio
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            return await node_box(ns, box_id)
        except RuntimeError:
            if loop.time() >= deadline:
                raise TimeoutError(f"box {box_id[:12]} not visible on the node after {timeout:.0f}s")
            await asyncio.sleep(interval)


EXPLORER_ATTEMPTS = 3
EXPLORER_ATTEMPT_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def find_box_id(token_id: str, ns=None, explorer=None, retry_delay: float = 2.0) -> str:
    """Id of the unspent box holding `token_id` (pool/bank/oracle NFT).

    Tries the node's extra index first (/blockchain/..., needs extraIndex = true),
    then the public explorer with retries. Raises RuntimeError if both fail.
    """
    import asyncio
    if ns is not None:
        url = f"{config.ERGO_NODE_URL}/blockchain/box/unspent/byTokenId/{token_id}?offset=0&limit=1"
        try:
            async with ns.get(url, timeout=EXPLORER_ATTEMPT_TIMEOUT) as r:
                if r.status == 200:
                    boxes = await r.json()
                    if boxes:
                        return boxes[0]["boxId"]
        except (asyncio.TimeoutError, aiohttp.ClientError):
            pass

    own_session = explorer is None
    if own_session:
        explorer = aiohttp.ClientSession()
    last_error = "no response"
    try:
        for attempt in range(EXPLORER_ATTEMPTS):
            try:
                url = f"{config.ERGO_EXPLORER_API_URL}/boxes/unspent/byTokenId/{token_id}?limit=1"
                async with explorer.get(url, timeout=EXPLORER_ATTEMPT_TIMEOUT) as r:
                    if r.status == 200:
                        data = await r.json()
                        items = data.get("items", data) if isinstance(data, dict) else data
                        if items:
                            return items[0]["boxId"]
                        last_error = "no unspent box"
                    else:
                        last_error = f"HTTP {r.status}"
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                last_error = type(e).__name__
            if attempt < EXPLORER_ATTEMPTS - 1:
                await asyncio.sleep(retry_delay * (attempt + 1))
    finally:
        if own_session:
            await explorer.close()
    raise RuntimeError(f"could not find the box for token {token_id[:8]} (node index unavailable, explorer: {last_error})")
