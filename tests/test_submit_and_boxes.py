"""Deferred review items: a leg-2 submit with an unknown outcome is followed, not failed; plain ERG
spends avoid token-holding boxes, and a change box never gets more tokens than a box can safely hold."""
import asyncio

import aiohttp
import pytest

import ergo.arb_runner as runner
from ergo.wallet import MAX_CHANGE_TOKENS, select_boxes

TREE = "0008cd02" + "22" * 32


def run(coro):
    return asyncio.run(coro)


def box(i, erg, tokens=0):
    return {"boxId": f"b{i}", "value": int(erg * 1e9), "ergoTree": TREE,
            "assets": [{"tokenId": f"{i:02x}{t:02x}" * 16, "amount": 1} for t in range(tokens)]}


# --- leg 2 submit ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [asyncio.TimeoutError(), aiohttp.ServerDisconnectedError()])
def test_a_submit_with_an_unknown_outcome_is_followed(monkeypatch, exc):
    async def lost(ns, signed):
        raise exc

    monkeypatch.setattr(runner, "_submit", lost)
    logs = []
    assert run(runner.submit_leg2(None, {"id": "tx2"}, logs.append)) == "tx2"
    assert any("outcome unknown" in m for m in logs)


def test_a_rejected_submit_still_fails(monkeypatch):
    async def rejected(ns, signed):
        raise RuntimeError("submit failed: HTTP 400 double spend")

    monkeypatch.setattr(runner, "_submit", rejected)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        run(runner.submit_leg2(None, {"id": "tx2"}, lambda m: None))


# --- wallet box selection ---------------------------------------------------------------------------------

def test_plain_erg_spends_prefer_token_free_boxes():
    spam, clean = box(1, 50, tokens=3), box(2, 10)
    assert select_boxes([spam, clean], None, 0, 5 * 10**9) == [clean]


def test_token_boxes_are_still_used_when_the_erg_is_needed():
    spam, clean = box(1, 50, tokens=3), box(2, 10)
    assert select_boxes([spam, clean], None, 0, 30 * 10**9) == [clean, spam]


def test_too_many_tokens_for_one_change_box_is_refused_clearly():
    spammy = [box(i, 1, tokens=10) for i in range(10)]          # 100 distinct tokens, 10 ERG
    with pytest.raises(ValueError, match="distinct tokens"):
        select_boxes(spammy, None, 0, 9 * 10**9)
    assert MAX_CHANGE_TOKENS < 100
