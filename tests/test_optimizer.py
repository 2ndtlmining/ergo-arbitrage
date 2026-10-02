"""Optimal trade size (issue #10): search the profit curve instead of a fixed grid."""
import pytest

import config
from arbitrage.optimizer import maximize
from arbitrage.scanner import ArbitrageScanner
from exchanges.base import PoolState
from exchanges.sigmausd import BankState
from logging_config import console
from tests.test_scanner_paths import ORACLE_R4, make_prices


class TestMaximize:
    def test_interior_maximum(self):
        x, fx = maximize(lambda x: -(x - 37.3) ** 2, 1, 1000)
        assert x == pytest.approx(37.3, abs=0.05)
        assert fx == pytest.approx(0, abs=1e-2)

    def test_maximum_at_upper_bound(self):
        x, _ = maximize(lambda x: x, 1, 250)
        assert x == pytest.approx(250)

    def test_maximum_at_lower_bound(self):
        x, _ = maximize(lambda x: -x, 1, 250)
        assert x == pytest.approx(1)

    def test_flat_fee_shape(self):
        # concave profit with a flat cost: k*x - c - x^2/d
        f = lambda x: 0.05 * x - 0.5 - x * x / 4000
        x, _ = maximize(f, 1, 1000)
        assert x == pytest.approx(100, abs=0.2)


@pytest.fixture
def scanner(tmp_path):
    s = ArbitrageScanner(db_path=str(tmp_path / "t.db"))
    yield s
    s.tracker.close()


def discount_prices(pool_erg=20_000):
    """Pool sells SigUSD below the bank's redeem value -> pool buy -> bank redeem is profitable."""
    state = BankState(bank_erg_nano=3_000_000 * 10**9, sigusd_circ_cents=10_000_000, oracle_r4=ORACLE_R4)
    spot = 0.35  # SigUSD per ERG; the bank pays ~3.1 ERG per SigUSD (oracle 0.3226)
    pool = PoolState(exchange="t", pool_id="p", token_x="ERG", token_y="SigUSD",
                     reserve_x=pool_erg, reserve_y=pool_erg * spot, fee_num=995, fee_denom=1000)
    prices = make_prices(state, spot)
    prices["spectrum_pool"] = pool
    return prices


class TestScannerOptimum:
    KEY = "Spectrum buy->Bank redeem"

    def test_optimum_beats_every_grid_point(self, scanner, monkeypatch):
        monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 1000.0)
        monkeypatch.setattr(config, "SIZE_PROFIT_CAPTURE", 1.0)  # pure profit maximising
        prices = discount_prices()
        scanner._find_opportunities(prices)
        from arbitrage.sizing import Market, profit_nanoerg
        best = scanner.last_sizing[self.KEY]
        market = Market.from_pool_state(prices["spectrum_pool"], prices["bank"]["state"])
        grid = [profit_nanoerg("redeem", market, x * 10**9)[0] / 1e9 for x in range(1, 1001, 7)]
        assert best.profit_erg >= max(grid) - 1e-9
        assert scanner.last_optima[self.KEY].input_erg == pytest.approx(best.size_erg)

    def test_thin_pool_gives_interior_optimum(self, scanner, monkeypatch):
        monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 1000.0)
        scanner._find_opportunities(discount_prices(pool_erg=2_000))
        best = scanner.last_optima[self.KEY]
        assert 1 < best.input_erg < 1000
        assert best.profit_erg > 0

    def test_optimum_capped_by_max_trade_size(self, scanner, monkeypatch):
        monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 10.0)
        scanner._find_opportunities(discount_prices())
        assert scanner.last_optima[self.KEY].input_erg <= 10.0

    def test_default_capture_keeps_most_profit_on_a_smaller_size(self, scanner, monkeypatch):
        monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 1000.0)
        scanner._find_opportunities(discount_prices())
        choice = scanner.last_sizing[self.KEY]
        assert choice.ok
        assert choice.profit_erg >= config.SIZE_PROFIT_CAPTURE * choice.max_profit_erg - 1e-9
        assert choice.size_erg < choice.max_profit_size_erg

    def test_display_shows_best_size(self, scanner, monkeypatch):
        monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 1000.0)
        prices = discount_prices(pool_erg=2_000)
        opps = scanner._find_opportunities(prices)
        with console.capture() as cap:
            scanner._display_opportunities(opps, prices)
        assert f"BEST SIZE {self.KEY}" in cap.get()


def test_unprofitable_path_says_no_profitable_size(scanner):
    from tests.test_scanner_paths import ORACLE_R4 as R4
    state = BankState(bank_erg_nano=3_000_000 * 10**9, sigusd_circ_cents=10_000_000, oracle_r4=R4)
    prices = make_prices(state, 0.31)  # SigUSD at a premium: buy+redeem always loses
    opps = scanner._find_opportunities(prices)
    with console.capture() as cap:
        scanner._display_opportunities(opps, prices)
    assert "BEST SIZE Spectrum buy->Bank redeem: no profitable size" in cap.get()
