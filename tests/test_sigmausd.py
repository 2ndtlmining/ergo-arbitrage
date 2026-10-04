"""Unit tests for SigmaUSD bank rules and contract-exact quote math (issue #1)."""
from exchanges.sigmausd import (
    BankState,
    quote_redeem_sigusd,
    quote_mint_sigusd,
    mint_cost_nanoerg,
    can_mint_sigusd,
)

# Live-ish state: oracle 3.099 ERG/USD, RR ~ 2000% (well above 800%)
ORACLE_R4 = 3_099_010_592          # nanoERG per USD (rate per cent = R4 // 100)
HIGH_RR = BankState(bank_erg_nano=2_000_000 * 10**9, sigusd_circ_cents=3_000_000, oracle_r4=ORACLE_R4)


class TestReserveRatioRules:
    def test_mint_checks_rr_after_the_mint(self):
        # RR = 405% before the mint: 405 ERG reserve per 100 USD circ, at 1 ERG = 1 USD
        rate_r4 = 10**9  # 1 ERG per USD
        state = BankState(bank_erg_nano=405 * 10**9, sigusd_circ_cents=100 * 100, oracle_r4=rate_r4)
        assert state.reserve_ratio > 400
        # Minting 1 cent keeps RR >= 400, minting 50 USD does not
        assert can_mint_sigusd(state, 1) is True
        assert can_mint_sigusd(state, 50 * 100) is False

    def test_mint_blocked_below_400(self):
        state = BankState(bank_erg_nano=300 * 10**9, sigusd_circ_cents=100 * 100, oracle_r4=10**9)
        assert can_mint_sigusd(state, 1) is False


class TestRedeemQuote:
    def test_matches_execute_script_integer_math(self):
        # 1.00 SigUSD redeem with RR well above 100%: liable_rate > rate so nominal price = rate
        cents = 100
        rate = ORACLE_R4 // 100
        gross = rate * cents
        fee = gross * 2 // 100
        bc_delta = gross - fee
        ui_fee = max(bc_delta * 229 // 100000, 1_000_000)
        assert quote_redeem_sigusd(HIGH_RR, cents) == bc_delta - ui_fee

    def test_pro_rata_when_undercollateralised(self):
        # RR 50%: 50 ERG reserve for 100 USD circ at 1 ERG/USD -> each cent redeems at half the rate
        state = BankState(bank_erg_nano=50 * 10**9, sigusd_circ_cents=100 * 100, oracle_r4=10**9)
        full = BankState(bank_erg_nano=500 * 10**9, sigusd_circ_cents=100 * 100, oracle_r4=10**9)
        assert quote_redeem_sigusd(state, 1000) < quote_redeem_sigusd(full, 1000) * 0.55

    def test_parity_with_confirmed_tx_c7a08cda(self):
        # FLOWS.md: 1 SigUSD redeemed at oracle 3.136 ERG/USD -> 3.065 ERG net after 0.0011 miner fee
        state = BankState(bank_erg_nano=2_000_000 * 10**9, sigusd_circ_cents=3_000_000, oracle_r4=3_136_000_000)
        net_erg = quote_redeem_sigusd(state, 100) / 1e9 - 0.0011
        assert abs(net_erg - 3.065) < 0.001

    def test_zero_or_negative(self):
        assert quote_redeem_sigusd(HIGH_RR, 0) == 0


class TestMintQuote:
    def test_cost_includes_protocol_and_ui_fee(self):
        rate = ORACLE_R4 // 100
        cost = mint_cost_nanoerg(HIGH_RR, 100)
        base = rate * 100
        assert cost > base * 1.02
        assert cost < base * 1.03

    def test_mint_quote_is_max_affordable_cents(self):
        budget = 10 * 10**9
        cents = quote_mint_sigusd(HIGH_RR, budget)
        assert cents > 0
        assert mint_cost_nanoerg(HIGH_RR, cents) <= budget
        assert mint_cost_nanoerg(HIGH_RR, cents + 1) > budget

    def test_mint_quote_respects_rr(self):
        state = BankState(bank_erg_nano=405 * 10**9, sigusd_circ_cents=100 * 100, oracle_r4=10**9)
        cents = quote_mint_sigusd(state, 100 * 10**9)
        assert can_mint_sigusd(state, cents)
        assert not can_mint_sigusd(state, cents + 1)


class TestOracleDivergence:
    def test_bank_state_oracle_price_usd_per_erg(self):
        assert abs(HIGH_RR.oracle_usd_per_erg - 1e9 / ORACLE_R4) < 1e-12
