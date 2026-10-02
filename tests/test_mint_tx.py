"""SigUSD mint TX (bank mint leg), checked against a re-implementation of bank.es v2."""
import pytest

import config
from ergo.codec import encode_slong
from ergo.sigmausd_tx import build_mint_tx, register_int
from ergo.tx_guard import SignPolicy, verify_unsigned_tx
from exchanges.sigmausd import BankState, mint_cost_nanoerg

SIGUSD, SIGRSV, NFT = config.SIGUSD_TOKEN_ID, config.SIGRSV_TOKEN_ID, config.SIGMAUSD_BANK_NFT
BANK_TREE = "102a0400" + "cd" * 40
UI_TREE = "0008cd02c5f61c83056a746a19a9e449e3c9596314cc417a2ef496b7567af558518f2bc7"
OUR_TREE = "0008cd02" + "44" * 32
ORACLE_R4 = 3_100_000_000  # nanoERG per USD


def bank_box(erg=5_000_000, circ_cents=16_000_000):
    """RR = erg / (circ_usd * 3.1): 5,000,000 ERG vs 160,000 USD -> ~1008%."""
    return {"boxId": "bank", "value": erg * 10**9, "ergoTree": BANK_TREE,
            "assets": [{"tokenId": SIGUSD, "amount": 10**12}, {"tokenId": SIGRSV, "amount": 10**12},
                       {"tokenId": NFT, "amount": 1}],
            "additionalRegisters": {"R4": encode_slong(circ_cents), "R5": encode_slong(500_000_000)}}


ORACLE_BOX = {"boxId": "oracle", "value": 10**9, "ergoTree": "00", "assets": [],
              "additionalRegisters": {"R4": encode_slong(ORACLE_R4)}}
WALLET = [{"boxId": "w", "value": 50 * 10**9, "ergoTree": OUR_TREE, "assets": []}]


def build(cents=1000, bank=None):
    return build_mint_tx(bank or bank_box(), ORACLE_BOX, WALLET, cents, height=1_900_000,
                         our_tree=OUR_TREE, ui_fee_tree=UI_TREE)


def bank_contract_accepts(bank_in, bank_out, receipt) -> bool:
    """bank.es v2 exchange branch for a SigUSD mint."""
    rate = ORACLE_R4 // 100
    sc_in, rc_in = register_int(bank_in, "R4"), register_int(bank_in, "R5")
    sc_out, rc_out = register_int(bank_out, "R4"), register_int(bank_out, "R5")
    tok = lambda b, i: int(b["assets"][i]["amount"])
    circ_delta, bc_delta = register_int(receipt, "R4"), register_int(receipt, "R5")
    valid_deltas = (sc_in + circ_delta == sc_out and rc_in == rc_out
                    and int(bank_in["value"]) + bc_delta == int(bank_out["value"]))
    conserved = tok(bank_in, 0) + sc_in == tok(bank_out, 0) + sc_out and tok(bank_in, 1) == tok(bank_out, 1)
    ids = all(bank_in["assets"][i]["tokenId"] == bank_out["assets"][i]["tokenId"] for i in range(3))
    rr_out = int(bank_out["value"]) * 100 // (sc_out * rate)
    liab_in = max(min(int(bank_in["value"]), sc_in * rate), 0)
    nominal = min(rate, liab_in // sc_in)
    br = nominal * circ_delta
    expected = br + abs(br * 2 // 100)
    return (valid_deltas and conserved and ids and bank_out["ergoTree"] == bank_in["ergoTree"]
            and bc_delta == expected and rr_out >= 400)


class TestMintTx:
    def test_bank_contract_accepts(self):
        tx, info = build()
        assert bank_contract_accepts(bank_box(), tx["outputs"][0], tx["outputs"][1])

    def test_user_gets_sigusd_and_pays_cost(self):
        tx, info = build(1000)
        state = BankState(5_000_000 * 10**9, 16_000_000, ORACLE_R4)
        assert info["cost_nanoerg"] == mint_cost_nanoerg(state, 1000) + info["miner_fee"]
        payout = tx["outputs"][2]
        assert {a["tokenId"]: a["amount"] for a in payout["assets"]}[SIGUSD] == 1000
        assert tx["outputs"][3]["ergoTree"] == UI_TREE

    def test_passes_guard(self):
        tx, info = build(1000)
        policy = SignPolicy(max_erg_spent=info["cost_nanoerg"], min_received={SIGUSD: 1000},
                            max_service_fee=info["ui_fee"], service_fee_trees=frozenset({UI_TREE}))
        report = verify_unsigned_tx(tx, [bank_box()] + info["user_boxes"], {OUR_TREE}, policy)
        assert report.erg_spent == info["cost_nanoerg"] and report.received == {SIGUSD: 1000}

    def test_refuses_when_rr_would_drop_below_400(self):
        low = bank_box(erg=500_000, circ_cents=16_000_000)  # RR ~ 100%
        with pytest.raises(ValueError, match="400"):
            build(1000, bank=low)

    def test_not_enough_erg(self):
        with pytest.raises(ValueError, match="ERG"):
            build(10_000_000)  # 100,000 SigUSD ~ 310,000 ERG
