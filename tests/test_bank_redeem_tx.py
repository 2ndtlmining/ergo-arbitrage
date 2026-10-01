"""Self-built SigmaUSD redeem TX: contract-exact amounts, multi-box inputs, passes the TX guard."""
import pytest

import config
from ergo.codec import encode_slong
from ergo.sigmausd_tx import build_redeem_tx, register_int
from ergo.tx_guard import MINER_FEE_TREE, SignPolicy, verify_unsigned_tx
from exchanges.sigmausd import BankState, quote_redeem_sigusd

SIGUSD = config.SIGUSD_TOKEN_ID
SIGRSV = config.SIGRSV_TOKEN_ID
NFT = config.SIGMAUSD_BANK_NFT
BANK_TREE = "102a0400" + "cd" * 40
UI_TREE = next(t for t in config.SERVICE_FEE_ERGO_TREES if t.startswith("0008cd02c5f6"))
OUR_TREE = "0008cd02" + "11" * 32
OTHER_TOKEN = "cc" * 32
ORACLE_R4 = 3_100_000_000

BANK_BOX = {
    "boxId": "bank", "value": 1_600_000 * 10**9, "ergoTree": BANK_TREE,
    "assets": [{"tokenId": SIGUSD, "amount": 10**12}, {"tokenId": SIGRSV, "amount": 10**12}, {"tokenId": NFT, "amount": 1}],
    "additionalRegisters": {"R4": encode_slong(16_000_000), "R5": encode_slong(500_000_000)},
}
ORACLE_BOX = {"boxId": "oracle", "value": 10**9, "ergoTree": "00", "assets": [],
              "additionalRegisters": {"R4": {"serializedValue": encode_slong(ORACLE_R4), "renderedValue": str(ORACLE_R4)}}}
USER_BOXES = [
    {"boxId": "u1", "value": 1_000_000, "ergoTree": OUR_TREE, "assets": [{"tokenId": SIGUSD, "amount": 60}]},
    {"boxId": "u2", "value": 2_000_000, "ergoTree": OUR_TREE,
     "assets": [{"tokenId": SIGUSD, "amount": 70}, {"tokenId": OTHER_TOKEN, "amount": 9}]},
]


def build(cents=100):
    return build_redeem_tx(BANK_BOX, ORACLE_BOX, USER_BOXES, cents, height=1_900_000,
                           our_tree=OUR_TREE, ui_fee_tree=UI_TREE)


def test_register_int_reads_node_and_explorer_formats():
    assert register_int(BANK_BOX, "R4") == 16_000_000
    assert register_int(ORACLE_BOX, "R4") == ORACLE_R4


def test_amounts_match_contract_quote():
    tx, info = build()
    state = BankState(BANK_BOX["value"], 16_000_000, ORACLE_R4)
    assert info["user_receives"] == quote_redeem_sigusd(state, 100)
    bank_out = tx["outputs"][0]
    assert bank_out["ergoTree"] == BANK_TREE
    assert bank_out["value"] == BANK_BOX["value"] - info["bc_delta"]
    assert {a["tokenId"]: a["amount"] for a in bank_out["assets"]}[SIGUSD] == 10**12 + 100


def test_change_spans_multiple_small_boxes_without_going_negative():
    tx, info = build(cents=100)  # 60 + 70 cents across two boxes, only 3M nanoERG in total
    payout = tx["outputs"][2]
    assets = {a["tokenId"]: a["amount"] for a in payout["assets"]}
    assert assets[SIGUSD] == 30
    assert assets[OTHER_TOKEN] == 9
    assert payout["value"] > 0
    assert all(o["value"] > 0 for o in tx["outputs"])
    assert tx["outputs"][4]["ergoTree"] == MINER_FEE_TREE


def test_self_built_tx_passes_guard():
    tx, info = build()
    policy = SignPolicy(
        max_erg_spent=0, max_token_spent={SIGUSD: 100},
        min_erg_received=info["user_receives"] - info["miner_fee"],
        max_service_fee=info["ui_fee"],
    )
    report = verify_unsigned_tx(tx, [BANK_BOX] + info["user_boxes"], {OUR_TREE}, policy)
    assert -report.erg_spent == info["user_receives"] - info["miner_fee"]


def test_not_enough_sigusd_raises():
    with pytest.raises(ValueError):
        build(cents=1000)
