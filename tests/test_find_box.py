"""find_box_id: node extra index first, then the explorer with retries; clean error otherwise."""
import asyncio

import pytest

import config
from ergo.chain import find_box_id

TOKEN = "ab" * 32


class Resp:
    def __init__(self, status, payload=None, exc=None):
        self.status, self._payload, self._exc = status, payload, exc

    async def json(self):
        return self._payload

    async def __aenter__(self):
        if self._exc:
            raise self._exc
        return self

    async def __aexit__(self, *a):
        return False


class Session:
    def __init__(self, responses):
        self.responses, self.urls = list(responses), []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.responses.pop(0)


def run(coro):
    return asyncio.run(coro)


def test_uses_node_index_when_available():
    node = Session([Resp(200, [{"boxId": "fromnode"}])])
    explorer = Session([])
    assert run(find_box_id(TOKEN, node, explorer, retry_delay=0)) == "fromnode"
    assert node.urls == [f"{config.ERGO_NODE_URL}/blockchain/box/unspent/byTokenId/{TOKEN}?offset=0&limit=1"]
    assert explorer.urls == []


def test_falls_back_to_explorer_without_node_index():
    node = Session([Resp(400, {"reason": "extra indexing is not enabled"})])
    explorer = Session([Resp(200, {"items": [{"boxId": "fromexplorer"}]})])
    assert run(find_box_id(TOKEN, node, explorer, retry_delay=0)) == "fromexplorer"


def test_retries_explorer_timeouts():
    explorer = Session([Resp(0, exc=asyncio.TimeoutError()), Resp(0, exc=asyncio.TimeoutError()),
                        Resp(200, {"items": [{"boxId": "third"}]})])
    assert run(find_box_id(TOKEN, None, explorer, retry_delay=0)) == "third"


def test_gives_up_with_runtime_error():
    explorer = Session([Resp(0, exc=asyncio.TimeoutError()) for _ in range(3)])
    with pytest.raises(RuntimeError, match="explorer"):
        run(find_box_id(TOKEN, None, explorer, retry_delay=0))
