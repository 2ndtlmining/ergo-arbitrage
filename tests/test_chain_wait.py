"""wait_for_box: poll the node until a just-submitted output is visible (mempool included)."""
import asyncio

import pytest

import config
from ergo.chain import wait_for_box


class Resp:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Node:
    def __init__(self, misses):
        self.misses, self.calls = misses, 0

    def get(self, url, **kw):
        assert url == f"{config.ERGO_NODE_URL}/utxo/withPool/byId/b1"
        self.calls += 1
        if self.calls <= self.misses:
            return Resp(404, {})
        return Resp(200, {"boxId": "b1", "value": 5})


def test_returns_box_once_visible():
    node = Node(misses=2)
    box = asyncio.run(wait_for_box(node, "b1", timeout=5, interval=0.01))
    assert box["boxId"] == "b1" and node.calls == 3


def test_times_out():
    with pytest.raises(TimeoutError):
        asyncio.run(wait_for_box(Node(misses=10**6), "b1", timeout=0.05, interval=0.01))
