"""One pricing model and one episode table for the on-chain paths (#78)."""
import pytest

import config
from arbitrage.scanner import ArbitrageScanner
from arbitrage.sizing import Market, profit_nanoerg
from tests.test_optimizer import discount_prices

REDEEM = "Spectrum buy->Bank redeem"


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 1000.0)
    s = ArbitrageScanner(mode="monitor", db_path=str(tmp_path / "t.db"))
    yield s
    s.tracker.close()


def market(prices):
    return Market.from_pool_state(prices["spectrum_pool"], prices["bank"]["state"])


@pytest.mark.parametrize("size", [1, 10, 100])
def test_grid_cells_use_the_contract_exact_numbers(scanner, size):
    prices = discount_prices(pool_erg=2_000)
    opps = [o for o in scanner._find_opportunities(prices) if o.path == f"{REDEEM} [{size:g} ERG]"]
    profit, spent = profit_nanoerg("redeem", market(prices), size * 10**9)
    assert len(opps) == 1
    assert opps[0].profit_erg == pytest.approx(profit / 1e9, abs=1e-9)
    assert opps[0].profit_percent == pytest.approx(profit / spent * 100, abs=1e-9)
    assert opps[0].is_profitable == (profit / spent * 100 >= config.MIN_PROFIT_PERCENT)


def test_grid_and_live_gate_agree_at_the_best_size(scanner):
    prices = discount_prices(pool_erg=2_000)
    scanner._find_opportunities(prices)
    choice, best = scanner.last_sizing[REDEEM], scanner.last_optima[REDEEM]
    assert choice.ok
    assert best.profit_erg == pytest.approx(choice.profit_erg, abs=1e-9)
    assert best.profit_percent == pytest.approx(choice.profit_percent, abs=1e-6)


def test_the_last_step_shows_the_exact_result(scanner):
    prices = discount_prices(pool_erg=2_000)
    opp = next(o for o in scanner._find_opportunities(prices) if o.path == f"{REDEEM} [10 ERG]")
    assert f"net {opp.profit_erg:+.2f} ERG" in opp.steps[-1]


def test_only_one_episode_table_is_written(scanner):
    prices = discount_prices(pool_erg=2_000)
    opps = scanner._find_opportunities(prices)
    for n in (1, 2, 3):
        scanner.tracker.record_scan(opps, n)
    count = lambda t: scanner.tracker.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
    assert count("opportunity_episodes") == 0
    profitable_paths = {o.path_key for o in opps if o.is_profitable and not o.blocked}
    assert count("opportunities") == len(profitable_paths)             # one row when a path opens
    scanner.tracker.record_scan([], 4)                                   # closes them
    scanner.tracker.record_scan(opps, 5)                                 # opens again: new rows
    assert count("opportunities") == 2 * len(profitable_paths)


def test_use_mint_status_lives_with_the_dexy_code():
    from exchanges import dexy
    assert callable(dexy.fetch_use_mint_status)
