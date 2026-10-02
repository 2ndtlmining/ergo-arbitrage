"""Progress while waiting for a block, and 'all' amounts."""
import asyncio

import pytest

import config
from ergo.amounts import erg_spendable, parse_amount, token_total
from ergo.progress import fmt_elapsed, wait_confirmed


class Resp:
    def __init__(self, status, payload=None):
        self.status, self._payload = status, payload or {}

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Node:
    def __init__(self, heights, confirmations):
        self.heights, self.confs = list(heights), list(confirmations)

    def get(self, url, **kw):
        if url.endswith("/info"):
            return Resp(200, {"fullHeight": self.heights.pop(0) if len(self.heights) > 1 else self.heights[0]})
        if "/wallet/transactionById" in url:
            c = self.confs.pop(0) if len(self.confs) > 1 else self.confs[0]
            return Resp(404) if c is None else Resp(200, {"numConfirmations": c})
        raise AssertionError(url)


def test_fmt_elapsed():
    assert fmt_elapsed(5) == "0m05s" and fmt_elapsed(185) == "3m05s"


def test_reports_every_check_and_confirmation():
    lines = []
    node = Node(heights=[100, 100, 101, 102], confirmations=[None, 0, 0, 1])
    ok = asyncio.run(wait_confirmed(node, "abc", log=lines.append, timeout=5, interval=0))
    assert ok
    text = "\n".join(lines)
    assert "~2 min" in text                       # sets expectations up front
    assert "height 100" in text and "new block" in text
    assert "CONFIRMED in block 102" in text


def test_timeout_explains_next_step():
    lines = []
    node = Node(heights=[100], confirmations=[0])
    ok = asyncio.run(wait_confirmed(node, "abc", log=lines.append, timeout=0.05, interval=0.01))
    assert not ok
    assert "explorer" in lines[-1].lower()


class TestAmounts:
    def test_parse(self):
        assert parse_amount("all") == "all" and parse_amount("ALL") == "all"
        assert parse_amount("1.5") == 1.5
        with pytest.raises(ValueError):
            parse_amount("-1")

    def test_erg_spendable_keeps_fee_and_token_box(self):
        boxes = [{"value": 3_000_000_000, "ergoTree": "0008cd02", "assets": []},
                 {"value": 1_000_000_000, "ergoTree": "0008cd02", "assets": [{"tokenId": "t", "amount": 5}]}]
        # tokens must stay in a change box with at least the minimum box value
        assert erg_spendable(boxes, fee=1_100_000) == 4_000_000_000 - 1_100_000 - config.ERG_MIN_BOX_NANO

    def test_erg_spendable_without_tokens(self):
        boxes = [{"value": 2_000_000_000, "ergoTree": "0008cd02", "assets": []}]
        assert erg_spendable(boxes, fee=1_100_000) == 2_000_000_000 - 1_100_000

    def test_token_total(self):
        boxes = [{"value": 1, "ergoTree": "0008cd02", "assets": [{"tokenId": "t", "amount": 5}]},
                 {"value": 1, "ergoTree": "0008cd02", "assets": [{"tokenId": "t", "amount": 7}]}]
        assert token_total(boxes, "t") == 12
