"""Two-leg chained arbitrage: pool buy (leg 1) -> bank redeem of leg 1's output (leg 2)."""
import pytest

import config
from ergo.chain_arb import LEG1_OUTPUT_PLACEHOLDER, plan_pool_buy_redeem, tx_output_box
from ergo.tx_guard import SignPolicy, verify_unsigned_tx
from exchanges.sigmausd import quote_redeem_sigusd
from tests.test_bank_redeem_tx import BANK_BOX, ORACLE_BOX, UI_TREE

SIGUSD = config.SIGUSD_TOKEN_ID
OUR_TREE = "0008cd02" + "22" * 32
POOL_TREE = "1999030f" + "ab" * 40
POOL_NFT = "9916d75132593c8b07fe18bd8d583bda1652eed7565cf41a4738ddd90fc992ec"
WALLET = [{"boxId": "w1", "value": 30_000_000_000, "ergoTree": OUR_TREE, "assets": []}]


def pool_box(sigusd_per_erg: float, erg_reserve: int = 100_000 * 10**9) -> dict:
    return {
        "boxId": "pool", "value": erg_reserve, "ergoTree": POOL_TREE,
        "assets": [{"tokenId": POOL_NFT, "amount": 1},
                   {"tokenId": "303f39026572bcb4060b51fafc93787a236bb243744babaa99fceb833d61e198", "amount": 10**18},
                   {"tokenId": SIGUSD, "amount": int(erg_reserve / 1e9 * sigusd_per_erg * 100)}],
        "additionalRegisters": {"R4": "04c60f"},
    }


def plan(sigusd_per_erg, erg_in=10 * 10**9):
    return plan_pool_buy_redeem(pool_box(sigusd_per_erg), BANK_BOX, ORACLE_BOX, WALLET, erg_in,
                                height=1_900_000, our_tree=OUR_TREE, ui_fee_tree=UI_TREE)


class TestPlan:
    def test_leg2_spends_leg1_output(self):
        p = plan(0.32)
        assert p["leg2_tx"]["inputs"][1]["boxId"] == LEG1_OUTPUT_PLACEHOLDER
        sim = p["leg1_output_box"]
        assert sim["value"] == p["leg1_tx"]["outputs"][1]["value"]
        assert {a["tokenId"]: a["amount"] for a in sim["assets"]}[SIGUSD] == p["leg1_info"]["amount_out"]

    def test_profit_accounts_for_every_fee(self):
        erg_in = 10 * 10**9
        p = plan(0.32, erg_in)
        cents = p["leg1_info"]["amount_out"]
        state = p["leg2_info"]["state"]
        expected = quote_redeem_sigusd(state, cents) - p["leg2_info"]["miner_fee"] - erg_in - p["leg1_info"]["miner_fee"]
        assert p["profit_nanoerg"] == expected
        assert p["profit_percent"] == pytest.approx(expected / erg_in * 100)

    def test_sigusd_premium_on_pool_loses(self):
        # bank pays 3.1 ERG per SigUSD; pool sells SigUSD dearer (0.31 SigUSD per ERG)
        assert plan(0.31)["profit_nanoerg"] < 0

    def test_sigusd_discount_on_pool_profits(self):
        # pool sells SigUSD cheap (0.35 per ERG -> 2.86 ERG each) vs bank redeem ~3.03 ERG
        assert plan(0.35)["profit_percent"] > 3


class TestGuards:
    def test_leg1_passes_guard(self):
        p = plan(0.35)
        i1 = p["leg1_info"]
        policy = SignPolicy(max_erg_spent=10 * 10**9 + i1["miner_fee"], min_received={SIGUSD: i1["amount_out"]},
                            max_service_fee=0)
        verify_unsigned_tx(p["leg1_tx"], [pool_box(0.35)] + i1["user_boxes"], {OUR_TREE}, policy)

    def test_leg2_passes_guard_against_simulated_leg1_output(self):
        p = plan(0.35)
        verify_unsigned_tx(p["leg2_tx"], [BANK_BOX, p["leg1_output_box"]], {OUR_TREE}, p["leg2_policy"])

    def test_leg2_returns_everything_from_leg1_output(self):
        p = plan(0.35)
        payout = p["leg2_tx"]["outputs"][2]
        assert {a["tokenId"] for a in payout["assets"]} <= {SIGUSD}  # all SigUSD redeemed or returned


def test_tx_output_box_from_signed_tx():
    signed = {"id": "tx1", "outputs": [
        {"boxId": "b0", "value": 1, "ergoTree": "t0", "assets": [], "additionalRegisters": {}},
        {"boxId": "b1", "value": 5, "ergoTree": "t1", "assets": [{"tokenId": "x", "amount": 3}],
         "additionalRegisters": {}, "creationHeight": 9},
    ]}
    box = tx_output_box(signed, 1)
    assert box == {"boxId": "b1", "value": 5, "ergoTree": "t1", "assets": [{"tokenId": "x", "amount": 3}],
                   "additionalRegisters": {}}


class TestMintSellPlan:
    """Leg 1 mints SigUSD at the bank, leg 2 sells it on the pool."""

    def plan(self, sigusd_per_erg, erg_budget=10 * 10**9):
        from ergo.chain_arb import plan_bank_mint_pool_sell
        from tests.test_mint_tx import ORACLE_BOX as MINT_ORACLE, bank_box as mint_bank
        return plan_bank_mint_pool_sell(pool_box(sigusd_per_erg), mint_bank(), MINT_ORACLE, WALLET, erg_budget,
                                        height=1_900_000, our_tree=OUR_TREE, ui_fee_tree=UI_TREE)

    def test_leg2_spends_leg1_payout(self):
        p = self.plan(0.30)
        assert p["leg1_output_index"] == 2
        assert p["leg2_tx"]["inputs"][1]["boxId"] == LEG1_OUTPUT_PLACEHOLDER
        assert {a["tokenId"]: a["amount"] for a in p["leg1_output_box"]["assets"]}[SIGUSD] == p["sigusd_cents"]

    def test_profit_is_pool_erg_minus_mint_cost_minus_fees(self):
        p = self.plan(0.30)
        expected = p["leg2_info"]["amount_out"] - p["leg2_info"]["miner_fee"] - p["leg1_info"]["cost_nanoerg"]
        assert p["profit_nanoerg"] == expected

    def test_sigusd_premium_on_pool_profits(self):
        # bank mints at ~0.3226 USD/ERG minus 2.2%; pool buys SigUSD at 0.30 per ERG -> ~5% premium
        assert self.plan(0.30)["profit_percent"] > 1

    def test_sigusd_discount_on_pool_loses(self):
        assert self.plan(0.35)["profit_nanoerg"] < 0

    def test_both_legs_pass_guard(self):
        from tests.test_mint_tx import bank_box as mint_bank
        p = self.plan(0.30)
        verify_unsigned_tx(p["leg1_tx"], [mint_bank()] + p["leg1_info"]["user_boxes"], {OUR_TREE}, p["leg1_policy"])
        verify_unsigned_tx(p["leg2_tx"], [pool_box(0.30), p["leg1_output_box"]], {OUR_TREE}, p["leg2_policy"])
