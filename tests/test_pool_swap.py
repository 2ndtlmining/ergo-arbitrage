"""Direct ErgoDEX N2T pool swap: matches Crux's pool output, satisfies the pool contract, passes the guard."""
import copy
import json
from pathlib import Path

import pytest

import config
from ergo.pool_swap import build_pool_swap_tx
from ergo.sigmausd_tx import register_int
from ergo.tx_guard import MINER_FEE_TREE, SignPolicy, verify_unsigned_tx

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "crux_swap_erg_to_sigusd.json").read_text())
POOL_BOX = FIXTURE["input_boxes"][0]
POOL_BOX = dict(POOL_BOX, additionalRegisters={"R4": "04c60f"})  # node format: serialized hex
WALLET_BOXES = FIXTURE["input_boxes"][1:]
OUR_TREE = WALLET_BOXES[0]["ergoTree"]
SIGUSD = config.SIGUSD_TOKEN_ID
LOCKED_LP = 0x7FFFFFFFFFFFFFFF


def amounts(box):
    return {a["tokenId"]: int(a["amount"]) for a in box["assets"]}


def pool_contract_accepts(pool_in, pool_out) -> bool:
    """Re-implementation of contracts/amm/cfmm/v1/n2t/Pool.sc for a swap."""
    t0, t1 = pool_in["assets"], pool_out["assets"]
    if pool_out["ergoTree"] != pool_in["ergoTree"] or len(t1) != 3:
        return False
    if register_int(pool_out, "R4") != register_int(pool_in, "R4"):
        return False
    if t1[0]["tokenId"] != t0[0]["tokenId"] or t1[1]["tokenId"] != t0[1]["tokenId"] or t1[2]["tokenId"] != t0[2]["tokenId"]:
        return False
    if int(t1[1]["amount"]) != int(t0[1]["amount"]) or int(pool_out["value"]) <= 10_000_000:
        return False
    fee = register_int(pool_in, "R4")
    rx0, ry0 = int(pool_in["value"]), int(t0[2]["amount"])
    dx, dy = int(pool_out["value"]) - rx0, int(t1[2]["amount"]) - ry0
    if dx > 0:
        return ry0 * dx * fee >= -dy * (rx0 * 1000 + dx * fee)
    return rx0 * dy * fee >= -dx * (ry0 * 1000 + dy * fee)


def build(sell_erg=True, amount=2_000_000_000, wallet=None):
    return build_pool_swap_tx(POOL_BOX, wallet or WALLET_BOXES, SIGUSD, sell_erg=sell_erg,
                              amount_in=amount, height=1_885_400, our_tree=OUR_TREE)


class TestMatchesCrux:
    def test_pool_output_identical_to_crux_swap(self):
        tx, info = build()
        crux_pool_out = FIXTURE["unsigned_tx"]["outputs"][0]
        ours = tx["outputs"][0]
        assert int(ours["value"]) == int(crux_pool_out["value"])
        assert amounts(ours) == amounts(crux_pool_out)
        assert info["amount_out"] == 62  # same as Crux expected_output

    def test_no_service_fee_output(self):
        tx, _ = build()
        trees = [o["ergoTree"] for o in tx["outputs"]]
        assert trees == [POOL_BOX["ergoTree"], OUR_TREE, MINER_FEE_TREE]


class TestPoolContract:
    @pytest.mark.parametrize("sell_erg,amount", [(True, 2_000_000_000), (True, 50_000_000_000), (False, 1000)])
    def test_contract_accepts(self, sell_erg, amount):
        wallet = copy.deepcopy(WALLET_BOXES)
        wallet[0]["assets"] = [{"tokenId": SIGUSD, "amount": 5000}]
        wallet[2]["value"] = 60_000_000_000
        tx, _ = build(sell_erg, amount, wallet)
        assert pool_contract_accepts(POOL_BOX, tx["outputs"][0])

    def test_one_more_unit_would_be_rejected(self):
        tx, _ = build()
        greedy = copy.deepcopy(tx["outputs"][0])
        greedy["assets"][2]["amount"] = int(greedy["assets"][2]["amount"]) - 1
        assert not pool_contract_accepts(POOL_BOX, greedy)


class TestWalletSide:
    def test_passes_guard_with_zero_service_fee(self):
        tx, info = build()
        policy = SignPolicy(max_erg_spent=2_000_000_000 + info["miner_fee"],
                            min_received={SIGUSD: info["amount_out"]}, max_service_fee=0)
        report = verify_unsigned_tx(tx, [POOL_BOX] + info["user_boxes"], {OUR_TREE}, policy)
        assert report.erg_spent == 2_000_000_000 + info["miner_fee"]
        assert report.received == {SIGUSD: 62}

    def test_sell_sigusd_for_erg(self):
        wallet = copy.deepcopy(WALLET_BOXES)
        wallet[0]["assets"] = [{"tokenId": SIGUSD, "amount": 1500}]
        tx, info = build(sell_erg=False, amount=1000, wallet=wallet)
        assert info["amount_out"] == 31_808_475_717 or info["amount_out"] > 31_000_000_000
        user = tx["outputs"][1]
        assert amounts(user).get(SIGUSD) == 500
        policy = SignPolicy(max_erg_spent=0, max_token_spent={SIGUSD: 1000},
                            min_erg_received=info["amount_out"] - info["miner_fee"], max_service_fee=0)
        verify_unsigned_tx(tx, [POOL_BOX] + info["user_boxes"], {OUR_TREE}, policy)

    def test_not_enough_erg(self):
        with pytest.raises(ValueError, match="ERG"):
            build(amount=100_000_000_000)

    def test_wrong_token_rejected(self):
        with pytest.raises(ValueError, match="token"):
            build_pool_swap_tx(POOL_BOX, WALLET_BOXES, "ff" * 32, sell_erg=True, amount_in=10**9,
                               height=1, our_tree=OUR_TREE)
