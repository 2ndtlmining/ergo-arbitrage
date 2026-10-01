"""Scanner path tests against a fixed `prices` snapshot (no network)."""
import pytest

from arbitrage.scanner import ArbitrageScanner
from exchanges.sigmausd import BankState, quote_redeem_sigusd

ORACLE_R4 = 3_100_000_000  # 3.1 ERG per USD -> $0.3226/ERG


def bank_dict(state: BankState) -> dict:
    return {
        "oracle_erg_usd": state.oracle_usd_per_erg,
        "bank_erg_reserve": state.bank_erg_nano / 1e9,
        "sigusd_circulating": state.sigusd_circ_cents / 100,
        "reserve_ratio": state.reserve_ratio,
        "can_mint_sigusd": True,
        "can_redeem_sigusd": True,
        "state": state,
    }


@pytest.fixture
def scanner(tmp_path):
    s = ArbitrageScanner(db_path=str(tmp_path / "t.db"))
    yield s
    s.tracker.close()


def make_prices(state: BankState, spectrum_price: float) -> dict:
    return {
        "nonkyc_erg_usdt": None,
        "kucoin_erg_usdt": None,
        "spectrum_erg_sigusd": spectrum_price,
        "bank": bank_dict(state),
        "use_data": None,
        "use_mint": None,
    }


def by_path(opps, prefix):
    return [o for o in opps if o.path.startswith(prefix)]


class TestBankPaths:
    def test_redeem_path_not_blocked_above_800_rr(self, scanner):
        state = BankState(bank_erg_nano=3_000_000 * 10**9, sigusd_circ_cents=10_000_000, oracle_r4=ORACLE_R4)
        assert state.reserve_ratio > 800
        opps = scanner._find_opportunities(make_prices(state, 0.31))
        redeem = by_path(opps, "Spectrum buy->Bank redeem")
        assert redeem and not any(o.blocked for o in redeem)

    def test_redeem_leg_uses_contract_quote(self, scanner):
        state = BankState(bank_erg_nano=3_000_000 * 10**9, sigusd_circ_cents=10_000_000, oracle_r4=ORACLE_R4)
        opps = scanner._find_opportunities(make_prices(state, 0.31))
        opp = next(o for o in by_path(opps, "Spectrum buy->Bank redeem") if o.input_erg == 10)
        cents = opp.details["sigusd_cents"]
        assert cents > 0
        assert opp.details["bank_erg"] == pytest.approx(quote_redeem_sigusd(state, cents) / 1e9)

    def test_mint_path_blocked_when_post_mint_rr_below_400(self, scanner):
        # RR 401%: even a 1 ERG mint pushes it under 400%? Use a tiny bank to make sure.
        state = BankState(bank_erg_nano=401 * 10**9, sigusd_circ_cents=100 * 100, oracle_r4=10**9)
        prices = make_prices(state, 0.95)
        prices["bank"]["can_mint_sigusd"] = True
        opps = scanner._find_opportunities(prices)
        mint_100 = next(o for o in by_path(opps, "Bank mint->Spectrum sell") if o.input_erg == 100)
        assert mint_100.blocked
        assert "400%" in mint_100.blocked_reason


class TestWalletAnalysis:
    def test_sigusd_bank_redeem_offered_above_800_rr(self, scanner):
        state = BankState(bank_erg_nano=3_000_000 * 10**9, sigusd_circ_cents=10_000_000, oracle_r4=ORACLE_R4)
        analysis = scanner._build_wallet_analysis({"erg": 0, "sigusd": 10, "use": 0}, make_prices(state, 0.31))
        redeem = next(o for o in analysis["sigusd"]["options"] if o["name"] == "Bank redeem")
        assert not redeem["blocked"]
        expected = quote_redeem_sigusd(state, 1000) / 1e9 - 0.0021
        assert f"{expected:.2f} ERG" in redeem["result"]

    def test_erg_mint_option_runs_with_bank_state(self, scanner):
        state = BankState(bank_erg_nano=3_000_000 * 10**9, sigusd_circ_cents=10_000_000, oracle_r4=ORACLE_R4)
        analysis = scanner._build_wallet_analysis({"erg": 50, "sigusd": 0, "use": 0}, make_prices(state, 0.31))
        names = {o["name"]: o for o in analysis["erg"]["options"]}
        assert not names["Bank mint -> Spectrum sell"]["blocked"]
        assert not names["Spectrum buy -> Bank redeem"]["blocked"]


class TestPoolLegs:
    def _pool(self, erg, sigusd):
        from exchanges.base import PoolState
        return PoolState(exchange="t", pool_id="p", token_x="ERG", token_y="SigUSD",
                         reserve_x=erg, reserve_y=sigusd, fee_num=995, fee_denom=1000)

    def test_dex_buy_leg_uses_pool_reserves(self, scanner):
        state = BankState(bank_erg_nano=3_000_000 * 10**9, sigusd_circ_cents=10_000_000, oracle_r4=ORACLE_R4)
        pool = self._pool(1_000, 310)  # thin pool: 100 ERG moves the price ~10%
        prices = make_prices(state, pool.price_x_in_y)
        prices["spectrum_pool"] = pool
        opps = scanner._find_opportunities(prices)
        opp = next(o for o in by_path(opps, "Spectrum buy->Bank redeem") if o.input_erg == 100)
        assert opp.details["sigusd_cents"] == int(pool.swap_output(100, input_is_x=True) * 100)

    def test_thin_pool_makes_large_size_worse(self, scanner):
        state = BankState(bank_erg_nano=3_000_000 * 10**9, sigusd_circ_cents=10_000_000, oracle_r4=ORACLE_R4)
        pool = self._pool(1_000, 330)
        prices = make_prices(state, pool.price_x_in_y)
        prices["spectrum_pool"] = pool
        opps = {o.input_erg: o for o in by_path(scanner._find_opportunities(prices), "Spectrum buy->Bank redeem")}
        assert opps[100].profit_percent < opps[25].profit_percent


class TestOnChainOnly:
    CEX_PREFIXES = ("NonKYC", "Kucoin")

    def _prices(self):
        state = BankState(bank_erg_nano=3_000_000 * 10**9, sigusd_circ_cents=10_000_000, oracle_r4=ORACLE_R4)
        p = make_prices(state, 0.31)
        p["nonkyc_erg_usdt"] = 0.33
        p["kucoin_erg_usdt"] = 0.34
        return p

    def test_cex_paths_skipped_when_disabled(self, tmp_path):
        s = ArbitrageScanner(db_path=str(tmp_path / "t.db"), enable_cex=False)
        try:
            opps = s._find_opportunities(self._prices())
            assert opps
            assert not [o for o in opps if o.path.startswith(self.CEX_PREFIXES)]
            analysis = s._build_wallet_analysis({"erg": 50, "sigusd": 10, "use": 0}, self._prices())
            names = [o["name"] for a in analysis.values() for o in a["options"]]
            assert not [n for n in names if "Kucoin" in n or "NonKYC" in n]
        finally:
            s.tracker.close()

    def test_cex_paths_present_when_enabled(self, tmp_path):
        s = ArbitrageScanner(db_path=str(tmp_path / "t.db"), enable_cex=True)
        try:
            opps = s._find_opportunities(self._prices())
            assert [o for o in opps if o.path.startswith(self.CEX_PREFIXES)]
        finally:
            s.tracker.close()


class TestUsePath:
    BOX_STATE = {"oracle_rate": ORACLE_R4, "bank_fee_num": 3, "buyback_fee_num": 2, "fee_denom": 1000}

    def _prices(self, lp, available=True):
        state = BankState(bank_erg_nano=3_000_000 * 10**9, sigusd_circ_cents=10_000_000, oracle_r4=ORACLE_R4)
        p = make_prices(state, 0.31)
        p["use_lp"] = lp
        p["use_mint"] = {
            "free_mint": {"is_available": available, "box_state": self.BOX_STATE},
            "arb_mint": {"is_available": False, "box_state": self.BOX_STATE},
        }
        return p

    def _lp(self, erg, use):
        from exchanges.base import PoolState
        return PoolState(exchange="t", pool_id="lp", token_x="ERG", token_y="USE",
                         reserve_x=erg, reserve_y=use, fee_num=997, fee_denom=1000)

    def test_empty_lp_makes_path_lose_everything(self, scanner):
        opps = scanner._find_opportunities(self._prices(self._lp(0.002, 0.001)))
        o = next(o for o in opps if o.path.startswith("Crux mint+sell USE") and o.input_erg == 25)
        assert o.profit_percent < -90
        assert not o.blocked

    def test_rich_lp_above_oracle_is_profitable(self, scanner):
        # LP prices USE at 3.4 ERG vs ~3.1 ERG at the oracle: mint at oracle, sell to LP
        opps = scanner._find_opportunities(self._prices(self._lp(340_000, 100_000)))
        o = next(o for o in opps if o.path.startswith("Crux mint+sell USE") and o.input_erg == 100)
        assert o.profit_erg > 0

    def test_blocked_when_mint_unavailable(self, scanner):
        opps = scanner._find_opportunities(self._prices(self._lp(340_000, 100_000), available=False))
        o = next(o for o in opps if o.path.startswith("Crux mint+sell USE"))
        assert o.blocked and not o.is_profitable
