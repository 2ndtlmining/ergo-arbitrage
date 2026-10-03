"""Rust node (arkadianet/ergo) compatibility: it has no GET /transactions/unconfirmed/outputs/byTokenId,
only POST /transactions/unconfirmed/byTokenId (body: the token id as a JSON string, answer: pool txs)."""
import asyncio
import json

import pytest

import ergo.chain_state as cs

NFT = "ab" * 32
POOL_OUT = {"boxId": "pending-pool", "value": 1, "assets": [{"tokenId": NFT, "amount": 1}]}
OTHER_OUT = {"boxId": "change", "value": 1, "assets": []}


class Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Node:
    """Scala-style (GET works) or Rust-style (GET 404, POST answers) mempool lookups."""

    def __init__(self, rust: bool):
        self.rust, self.gets, self.posts = rust, [], []

    def get(self, url, timeout=None):
        self.gets.append(url)
        if "/transactions/unconfirmed/outputs/byTokenId/" in url:
            return Resp(404, None) if self.rust else Resp(200, [POOL_OUT])
        if "/utxo/withPool/byId/pending-pool" in url:
            return Resp(200, POOL_OUT)
        return Resp(404, None)

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, data))
        assert url.endswith("/transactions/unconfirmed/byTokenId") and json.loads(data) == NFT
        return Resp(200, [{"id": "tx1", "outputs": [OTHER_OUT, POOL_OUT]}])


@pytest.fixture(autouse=True)
def fresh_detection():
    cs._MEMPOOL_BY_TOKEN_POST = None
    yield
    cs._MEMPOOL_BY_TOKEN_POST = None


def run(coro):
    return asyncio.run(coro)


def test_scala_node_uses_the_get_endpoint():
    node = Node(rust=False)
    assert [b["boxId"] for b in run(cs._pending_outputs(node, NFT))] == ["pending-pool"]
    assert node.posts == []


def test_rust_node_falls_back_to_the_post_endpoint():
    node = Node(rust=True)
    assert [b["boxId"] for b in run(cs._pending_outputs(node, NFT))] == ["pending-pool"]


def test_the_fallback_is_remembered_so_each_poll_makes_one_request():
    node = Node(rust=True)
    run(cs._pending_outputs(node, NFT))
    gets_before = sum("outputs/byTokenId" in u for u in node.gets)
    run(cs._pending_outputs(node, NFT))
    assert sum("outputs/byTokenId" in u for u in node.gets) == gets_before and len(node.posts) == 2


def test_a_node_with_neither_form_goes_back_to_trying_get():
    class Neither(Node):
        def post(self, url, data=None, headers=None, timeout=None):
            self.posts.append((url, data))
            return Resp(404, None)

    node = Neither(rust=True)
    assert run(cs._pending_outputs(node, NFT)) == []
    assert cs._MEMPOOL_BY_TOKEN_POST is None
