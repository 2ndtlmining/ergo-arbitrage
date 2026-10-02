"""Transaction-safety review fixes: leg-2 price floor, contract-box guard, input validation."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

import config
import ergo.arb_runner as runner
from ergo.amounts import erg_spendable
from ergo.chain_arb import plan_pool_buy_redeem
from ergo.codec import decode_int_register, encode_slong
from ergo.sigmausd_tx import build_redeem_tx
from ergo.tx_guard import SignPolicy, TxGuardError, verify_unsigned_tx
from tests.test_bank_redeem_tx import BANK_BOX, ORACLE_BOX, UI_TREE
from tests.test_chain_arb import OUR_TREE, WALLET, pool_box

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "crux_swap_erg_to_sigusd.json").read_text())
WALLET_TREE = FIXTURE["input_boxes"][1]["ergoTree"]
SIGUSD = config.SIGUSD_TOKEN_ID


# --- leg 2 price floor ------------------------------------------------------------------------------

def redeem_plan():
    return plan_pool_buy_redeem(pool_box(0.35, 2_000 * 10**9), BANK_BOX, ORACLE_BOX, WALLET, 10 * 10**9,
                                height=1_900_000, our_tree=OUR_TREE, ui_fee_tree=UI_TREE)


def oracle_with(r4: int) -> dict:
    o = copy.deepcopy(ORACLE_BOX)
    o["additionalRegisters"]["R4"] = {"serializedValue": encode_slong(r4), "renderedValue": str(r4)}
    return o


def build(plan, oracle, monkeypatch):
    async def fresh(ns, path):
        return [BANK_BOX, oracle]

    monkeypatch.setattr(runner, "_leg2_boxes", fresh)
    floor = runner.leg2_floor_nanoerg(plan, "redeem")
    return floor, asyncio.run(runner.build_leg2(None, "redeem", plan["leg1_output_box"], plan["sigusd_cents"],
                                                1_900_000, OUR_TREE, floor))


def test_floor_is_the_plan_minus_the_slippage_tolerance():
    plan = redeem_plan()
    assert runner.leg2_floor_nanoerg(plan, "redeem") == int(
        plan["leg2_info"]["user_receives"] * (1 - config.SLIPPAGE_TOLERANCE))


def test_leg2_on_unchanged_boxes_passes_and_the_guard_enforces_the_floor(monkeypatch):
    plan = redeem_plan()
    floor, (tx2, info2, policy2, erg_back) = build(plan, ORACLE_BOX, monkeypatch)
    assert erg_back == plan["leg2_info"]["user_receives"]
    assert policy2.min_erg_received >= floor - info2["miner_fee"]


def test_leg2_on_a_worse_price_waits_instead_of_selling_cheap(monkeypatch):
    """Review (critical): a rebuild after an oracle move must not be signed at any price."""
    plan = redeem_plan()
    worse = oracle_with(int(3_100_000_000 * 0.97))          # ERG 3% more expensive: 3% fewer ERG back
    with pytest.raises(runner.LegNotReady, match="below the floor"):
        build(plan, worse, monkeypatch)


# --- guard: only known contract boxes may be foreign inputs ------------------------------------------

def attack():
    """A foreign 'contract' box with a made-up singleton token, recreated with 0.5 ERG of our money."""
    tx, boxes = copy.deepcopy(FIXTURE["unsigned_tx"]), copy.deepcopy(FIXTURE["input_boxes"])
    evil_tree = "10010400d1" + "aa" * 8
    evil = {"boxId": "ee" * 32, "value": 1_000_000, "ergoTree": evil_tree,
            "assets": [{"tokenId": "ab" * 32, "amount": 1}]}
    tx["inputs"].append({"boxId": evil["boxId"]})
    boxes.append(evil)
    tx["outputs"].append({"value": 501_000_000, "ergoTree": evil_tree,
                          "assets": [{"tokenId": "ab" * 32, "amount": 1}]})
    return tx, boxes


def policy(**kw):
    return SignPolicy(max_erg_spent=int(3.0e9), min_received={SIGUSD: 60}, **kw)


def test_a_made_up_contract_box_is_refused():
    tx, boxes = attack()
    with pytest.raises(TxGuardError, match="not a known contract"):
        verify_unsigned_tx(tx, boxes, {WALLET_TREE}, policy())


def test_the_real_pool_swap_still_passes():
    tx, boxes = copy.deepcopy(FIXTURE["unsigned_tx"]), copy.deepcopy(FIXTURE["input_boxes"])
    verify_unsigned_tx(tx, boxes, {WALLET_TREE}, policy())


def test_someone_elses_wallet_box_is_never_a_contract_box():
    tx, boxes = attack()
    stranger = "0008cd03" + "cd" * 32
    boxes[-1]["ergoTree"] = stranger
    tx["outputs"][-1]["ergoTree"] = stranger
    with pytest.raises(TxGuardError, match="wallet box"):
        verify_unsigned_tx(tx, boxes, {WALLET_TREE}, policy(contract_nfts=None))


def test_opting_out_of_the_nft_list_keeps_the_old_rule():
    tx, boxes = attack()
    verify_unsigned_tx(tx, boxes, {WALLET_TREE}, policy(contract_nfts=None))   # e.g. Crux Dexy mint boxes


# --- input validation -----------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["05", "058080", "04ff"])
def test_truncated_registers_are_rejected(bad):
    with pytest.raises(ValueError):
        decode_int_register(bad)


def test_valid_registers_still_decode():
    assert decode_int_register("0500") == 0 and decode_int_register(encode_slong(16_000_000)) == 16_000_000


def test_redeem_of_nothing_is_a_clean_error():
    with pytest.raises(ValueError, match="cents"):
        build_redeem_tx(BANK_BOX, ORACLE_BOX, WALLET, 0, height=1_900_000, our_tree=OUR_TREE, ui_fee_tree=UI_TREE)


def test_selling_all_erg_leaves_room_for_the_swap_output_box():
    """Review: `swap --sell erg all` on a token-less wallet asked for MIN_BOX_VALUE more than it had."""
    boxes = [{"boxId": "w", "value": 5_000_000_000, "ergoTree": OUR_TREE, "assets": []}]
    assert erg_spendable(boxes, fee=1_100_000, keep_box=True) == 5_000_000_000 - 1_100_000 - config.ERG_MIN_BOX_NANO
    assert erg_spendable(boxes, fee=1_100_000) == 5_000_000_000 - 1_100_000      # sends: unchanged
