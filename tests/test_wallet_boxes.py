"""Wallet box selection for self-built transactions (issue #7)."""
import pytest

from ergo.wallet import select_boxes, sum_assets

TOK = "aa" * 32
OTHER = "bb" * 32
P2PK = "0008cd" + "02" * 33


def box(box_id, value, tokens=None, tree=P2PK):
    return {"boxId": box_id, "value": value, "ergoTree": tree,
            "assets": [{"tokenId": t, "amount": a} for t, a in (tokens or {}).items()]}


def test_gathers_until_token_and_erg_targets_met():
    boxes = [box("a", 1_000_000, {TOK: 30}), box("b", 5_000_000, {TOK: 50}), box("c", 2_000_000_000)]
    chosen = select_boxes(boxes, token_id=TOK, token_amount=70, min_erg=1_000_000_000)
    ids = {b["boxId"] for b in chosen}
    assert {"a", "b", "c"} == ids


def test_prefers_token_boxes_and_stops_early():
    boxes = [box("plain", 9_000_000_000), box("t", 3_000_000_000, {TOK: 500})]
    chosen = select_boxes(boxes, token_id=TOK, token_amount=100, min_erg=2_200_000)
    assert [b["boxId"] for b in chosen] == ["t"]


def test_insufficient_token_raises():
    with pytest.raises(ValueError, match="token"):
        select_boxes([box("a", 10**9, {TOK: 5})], token_id=TOK, token_amount=100, min_erg=1)


def test_insufficient_erg_raises():
    with pytest.raises(ValueError, match="ERG"):
        select_boxes([box("a", 1_000, {TOK: 500})], token_id=TOK, token_amount=100, min_erg=10**9)


def test_skips_non_p2pk_boxes():
    boxes = [box("script", 10**10, {TOK: 999}, tree="100204"), box("ok", 10**9, {TOK: 100})]
    assert [b["boxId"] for b in select_boxes(boxes, TOK, 100, 1)] == ["ok"]


def test_sum_assets():
    assert sum_assets([box("a", 1, {TOK: 1, OTHER: 2}), box("b", 1, {TOK: 3})]) == {TOK: 4, OTHER: 2}


def test_erg_only_selection_uses_any_p2pk_box():
    boxes = [box("t", 3_000_000_000, {TOK: 500}), box("plain", 1_000_000_000)]
    chosen = select_boxes(boxes, token_id=None, token_amount=0, min_erg=3_500_000_000)
    assert {b["boxId"] for b in chosen} == {"t", "plain"}
