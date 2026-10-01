"""Transaction guard: refuse to sign third-party-built TXs that don't match the policy (issue #7)."""
import copy
import json
from pathlib import Path

import pytest

import config
from ergo.tx_guard import SignPolicy, TxGuardError, verify_unsigned_tx

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "crux_swap_erg_to_sigusd.json").read_text())
WALLET_TREE = FIXTURE["input_boxes"][1]["ergoTree"]
ATTACKER_TREE = "0008cd03" + "ab" * 32
SIGUSD = config.SIGUSD_TOKEN_ID


def policy(**kw) -> SignPolicy:
    base = dict(
        max_erg_spent=int(2.0e9) + int(1.0e9),  # 2 ERG in + fee budget
        min_received={SIGUSD: 60},              # quote 62, 3% slippage
    )
    base.update(kw)
    return SignPolicy(**base)


def tx_and_inputs():
    return copy.deepcopy(FIXTURE["unsigned_tx"]), copy.deepcopy(FIXTURE["input_boxes"])


class TestAccepts:
    def test_real_crux_swap_passes(self):
        tx, boxes = tx_and_inputs()
        report = verify_unsigned_tx(tx, boxes, {WALLET_TREE}, policy())
        assert report.received[SIGUSD] == 62
        assert 2.0e9 < report.erg_spent < 2.9e9


class TestRejects:
    def test_output_to_foreign_address(self):
        tx, boxes = tx_and_inputs()
        change = next(o for o in tx["outputs"] if o["ergoTree"] == WALLET_TREE and not o["assets"])
        change["ergoTree"] = ATTACKER_TREE
        with pytest.raises(TxGuardError, match="unexpected output"):
            verify_unsigned_tx(tx, boxes, {WALLET_TREE}, policy())

    def test_received_below_minimum(self):
        tx, boxes = tx_and_inputs()
        with pytest.raises(TxGuardError, match="receives"):
            verify_unsigned_tx(tx, boxes, {WALLET_TREE}, policy(min_received={SIGUSD: 100}))

    def test_spends_more_erg_than_allowed(self):
        tx, boxes = tx_and_inputs()
        with pytest.raises(TxGuardError, match="spends"):
            verify_unsigned_tx(tx, boxes, {WALLET_TREE}, policy(max_erg_spent=int(2.5e9)))

    def test_service_fee_over_cap(self):
        tx, boxes = tx_and_inputs()
        with pytest.raises(TxGuardError, match="service fee"):
            verify_unsigned_tx(tx, boxes, {WALLET_TREE}, policy(max_service_fee=int(0.5e9)))

    def test_wallet_token_leaks(self):
        tx, boxes = tx_and_inputs()
        # Wallet input holds a token that the outputs don't return
        boxes[1]["assets"].append({"tokenId": "aa" * 32, "amount": 5})
        with pytest.raises(TxGuardError, match="loses token"):
            verify_unsigned_tx(tx, boxes, {WALLET_TREE}, policy())

    def test_contract_box_not_recreated(self):
        tx, boxes = tx_and_inputs()
        tx["outputs"][0]["ergoTree"] = ATTACKER_TREE
        with pytest.raises(TxGuardError):
            verify_unsigned_tx(tx, boxes, {WALLET_TREE}, policy())

    def test_input_list_mismatch(self):
        tx, boxes = tx_and_inputs()
        with pytest.raises(TxGuardError, match="input"):
            verify_unsigned_tx(tx, boxes[:-1], {WALLET_TREE}, policy())

    def test_trade_size_cap(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 1.0)
        tx, boxes = tx_and_inputs()
        with pytest.raises(TxGuardError, match="MAX_TRADE_SIZE_ERG"):
            verify_unsigned_tx(tx, boxes, {WALLET_TREE}, policy())


class TestMinErgReceived:
    def test_erg_purchase_requires_min_erg(self):
        # Swap fixture spends ERG, so any positive ERG minimum must fail
        tx, boxes = tx_and_inputs()
        with pytest.raises(TxGuardError, match="receives .* nanoERG"):
            verify_unsigned_tx(tx, boxes, {WALLET_TREE}, policy(min_erg_received=1))
