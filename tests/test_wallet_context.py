"""Wallet boxes from stale (invalid) unconfirmed transactions must be ignored."""
import asyncio

import config
from ergo.chain import wallet_context

TREE = "0008cd02" + "aa" * 32


class Resp:
    def __init__(self, status, payload=None):
        self.status, self._payload = status, payload if payload is not None else {}

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Node:
    """Wallet lists a confirmed box, a valid pending box and a phantom box from an invalid TX."""
    BOXES = [
        {"boxId": "confirmed", "value": 20_000_000_000, "ergoTree": TREE, "assets": []},
        {"boxId": "pending", "value": 1_000_000_000, "ergoTree": TREE, "assets": []},
        {"boxId": "phantom", "value": 4_862_457_000, "ergoTree": TREE, "assets": []},
    ]
    VISIBLE = {"confirmed", "pending"}  # /utxo/withPool knows these

    def get(self, url, **kw):
        n = config.ERGO_NODE_URL
        if url.startswith(f"{n}/wallet/boxes/unspent"):
            return Resp(200, [{"box": b} for b in self.BOXES])
        if url == f"{n}/info":
            return Resp(200, {"fullHeight": 100})
        if url == f"{n}/wallet/addresses":
            return Resp(200, ["9addr"])
        if url == f"{n}/utils/addressToRaw/9addr":
            return Resp(200, {"raw": TREE[6:]})
        if url.startswith(f"{n}/utxo/withPool/byId/"):
            box_id = url.rsplit("/", 1)[1]
            return Resp(200, {"boxId": box_id}) if box_id in self.VISIBLE else Resp(404)
        raise AssertionError(url)


def test_phantom_boxes_dropped():
    boxes, height, tree = asyncio.run(wallet_context(Node()))
    assert {b["boxId"] for b in boxes} == {"confirmed", "pending"}
    assert height == 100 and tree == TREE


def test_stale_listing_reported():
    stale = []
    asyncio.run(wallet_context(Node(), on_stale=stale.extend))
    assert [b["boxId"] for b in stale] == ["phantom"]
