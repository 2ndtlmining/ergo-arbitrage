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


async def wallet_context(ns) -> tuple[list[dict], int, str]:
    """(P2PK wallet boxes, current height, ErgoTree of the wallet's first address)."""
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
    return [b for b in boxes if b.get("ergoTree") in trees], height, our_tree
