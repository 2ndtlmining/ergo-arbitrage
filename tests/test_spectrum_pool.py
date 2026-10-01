"""On-chain ERG/SigUSD N2T pool parsing and swap math (issue #4)."""
import pytest

import config
from exchanges.spectrum import amm_output_raw, parse_n2t_pool_box

# Mainnet pool box 9916d751... (2026-10-02) and matching Crux /dex/quote outputs
POOL_BOX = {
    "value": 111869130364855,
    "assets": [
        {"tokenId": "9916d75132593c8b07fe18bd8d583bda1652eed7565cf41a4738ddd90fc992ec", "amount": 1},
        {"tokenId": "303f39026572bcb4060b51fafc93787a236bb243744babaa99fceb833d61e198", "amount": 9223372022176659008},
        {"tokenId": config.SIGUSD_TOKEN_ID, "amount": 3498380},
    ],
    "additionalRegisters": {"R4": {"serializedValue": "04c60f", "sigmaType": "SInt", "renderedValue": "995"}},
}


class TestAmmOutputRaw:
    def test_sigusd_to_erg_matches_crux(self):
        assert amm_output_raw(3498380, 111869130364855, 1000, 995) == 31808475717

    def test_erg_to_sigusd_matches_crux(self):
        assert amm_output_raw(111869130364855, 3498380, 100 * 10**9, 995) == 3108

    def test_zero_input(self):
        assert amm_output_raw(100, 100, 0, 995) == 0


class TestParsePoolBox:
    def test_reserves_and_fee(self):
        pool = parse_n2t_pool_box(POOL_BOX, config.SIGUSD_TOKEN_ID, decimals_y=2)
        assert pool.reserve_x == pytest.approx(111869.130364855)
        assert pool.reserve_y == pytest.approx(34983.80)
        assert pool.fee_num == 995
        assert pool.fee_denom == 1000

    def test_float_swap_close_to_raw(self):
        pool = parse_n2t_pool_box(POOL_BOX, config.SIGUSD_TOKEN_ID, decimals_y=2)
        assert pool.swap_output(10.00, input_is_x=False) == pytest.approx(31.808475717, rel=1e-9)

    def test_rejects_box_without_token(self):
        box = dict(POOL_BOX, assets=POOL_BOX["assets"][:2])
        with pytest.raises(ValueError):
            parse_n2t_pool_box(box, config.SIGUSD_TOKEN_ID, decimals_y=2)
