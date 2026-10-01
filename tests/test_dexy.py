"""Dexy USE mint math and LP pricing (issue #5)."""
import pytest

import config
from exchanges.dexy import dexy_mint_cost_nanoerg, quote_dexy_mint, parse_use_lp_box, mint_available
from exchanges.spectrum import amm_output_raw

BOX_STATE = {"oracle_rate": 3_097_154_000, "bank_fee_num": 3, "buyback_fee_num": 2, "fee_denom": 1000}

# Mainnet Dexy USE LP box (2026-10-02): essentially empty
LP_BOX = {
    "value": 2_000_000,
    "assets": [
        {"tokenId": config.DEXY_USE_LP_NFT, "amount": 1},
        {"tokenId": "804a66426283b8281240df8f9de783651986f20ad6391a71b26b9e7d6faad099", "amount": 1},
        {"tokenId": config.USE_TOKEN_ID, "amount": 1},
    ],
    "additionalRegisters": {},
}


class TestMint:
    def test_cost_is_oracle_plus_half_percent(self):
        unit_rate = BOX_STATE["oracle_rate"] // 1000  # nanoERG per raw USE unit (3 decimals)
        cost = dexy_mint_cost_nanoerg(1000, BOX_STATE)  # 1 USE
        assert cost == pytest.approx(unit_rate * 1000 * 1.005, rel=1e-6)

    def test_quote_is_max_affordable(self):
        budget = 10 * 10**9
        use_raw = quote_dexy_mint(budget, BOX_STATE)
        assert dexy_mint_cost_nanoerg(use_raw, BOX_STATE) <= budget
        assert dexy_mint_cost_nanoerg(use_raw + 1, BOX_STATE) > budget

    def test_mint_available_reads_is_available(self):
        assert mint_available({"free_mint": {"is_available": True}, "arb_mint": {"is_available": False}})
        assert not mint_available({"free_mint": {"is_available": False}, "arb_mint": {"is_available": False}})
        assert not mint_available(None)


class TestLp:
    def test_lp_parity_with_crux_quote(self):
        # Crux /dex/quote: 10000 raw USE -> 1999799 nanoERG through this LP
        assert amm_output_raw(1, 2_000_000, 10_000, 997) == 1_999_799

    def test_parse_lp_box(self):
        lp = parse_use_lp_box(LP_BOX)
        assert lp.reserve_x == pytest.approx(0.002)
        assert lp.reserve_y == pytest.approx(0.001)
        assert lp.fee_num == 997
        assert lp.swap_output(10.0, input_is_x=False) == pytest.approx(0.001999799, rel=1e-6)
