"""--live auto-execution of pool buy -> bank redeem, with every safety check."""
import asyncio

import pytest

import arbitrage.scanner as scanner_module
import config
from arbitrage.scanner import ArbitrageScanner
from ergo.arb_runner import ArbResult
from tests.test_optimizer import discount_prices

KEY = "Spectrum buy->Bank redeem"
HEALTHY = {"reachable": True, "synced": True, "unlocked": True, "height": 1, "headers": 1, "ok_to_trade": True}
WALLET = {"erg": 50.0, "sigusd": 0.0, "use": 0.0}


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 1000.0)
    monkeypatch.setattr(config, "LIVE_STOP_FILE", str(tmp_path / "STOP"))
    s = ArbitrageScanner(mode="live", db_path=str(tmp_path / "t.db"))
    calls = []

    async def fake_run(ns, path, erg_in, **kw):
        calls.append((erg_in, dict(kw, path=path)))
        return s._next_result

    async def healthy():
        return dict(HEALTHY)

    monkeypatch.setattr(scanner_module, "run_arb", fake_run)
    monkeypatch.setattr(s.ergo_node, "get_health", healthy)
    s._next_result = ArbResult("executed", erg_in=1, sigusd_cents=100, profit_nanoerg=10**8,
                               profit_percent=1.0, tx1="t1", tx2="t2")
    s.calls = calls
    prices = discount_prices(pool_erg=2_000)
    s._find_opportunities(prices)
    s._prices = prices
    for _ in range(config.LIVE_CONFIRM_SCANS):
        s._update_live_streak()
    yield s
    s.tracker.close()


def run(coro):
    return asyncio.run(coro)


def test_executes_best_size_when_ready(live):
    run(live._execute_trades(WALLET, live._prices))
    assert len(live.calls) == 1
    erg_in, kw = live.calls[0]
    expected = min(live.last_optima[KEY].input_erg, WALLET["erg"] - config.LIVE_ERG_RESERVE)
    assert erg_in == int(round(expected * 1e9))
    assert kw["execute"] is True
    row = live.tracker.conn.execute("SELECT status, tx_ids FROM trades").fetchone()
    assert row["status"] == "completed" and "t1" in row["tx_ids"]


def test_wallet_reserve_caps_size(live):
    run(live._execute_trades({"erg": 5.0, "sigusd": 0, "use": 0}, live._prices))
    assert live.calls[0][0] == int(round((5.0 - config.LIVE_ERG_RESERVE) * 1e9))


def test_not_in_monitor_mode(tmp_path, monkeypatch):
    s = ArbitrageScanner(mode="notify", db_path=str(tmp_path / "t.db"))
    called = []

    async def fake_run(*a, **k):
        called.append(1)

    monkeypatch.setattr(scanner_module, "run_arb", fake_run)
    run(s._execute_trades(WALLET, discount_prices()))
    assert called == []
    s.tracker.close()


@pytest.mark.parametrize("setup,reason", [
    (lambda s, tp: s._live_streak.__setitem__(KEY, 1), "scans"),
    (lambda s, tp: open(config.LIVE_STOP_FILE, "w").close(), "STOP"),
    (lambda s, tp: setattr(s, "_live_paused", "earlier failure"), "paused"),
])
def test_blockers(live, setup, reason, tmp_path):
    setup(live, tmp_path)
    blockers = run(live._live_blockers(WALLET, live._prices))
    assert any(reason in b for b in blockers)
    run(live._execute_trades(WALLET, live._prices))
    assert live.calls == []


def test_unhealthy_node_blocks(live, monkeypatch):
    async def locked():
        return dict(HEALTHY, unlocked=False, ok_to_trade=False)
    monkeypatch.setattr(live.ergo_node, "get_health", locked)
    assert any("node" in b for b in run(live._live_blockers(WALLET, live._prices)))


def test_small_wallet_blocks(live):
    assert any("wallet" in b for b in run(live._live_blockers({"erg": 1.5, "sigusd": 0, "use": 0}, live._prices)))


def test_unprofitable_blocks(live):
    from tests.test_scanner_paths import ORACLE_R4, make_prices
    from exchanges.sigmausd import BankState
    state = BankState(bank_erg_nano=3_000_000 * 10**9, sigusd_circ_cents=10_000_000, oracle_r4=ORACLE_R4)
    prices = make_prices(state, 0.31)
    live._find_opportunities(prices)
    assert any("profit" in b for b in run(live._live_blockers(WALLET, prices)))


def test_cooldown_after_trade(live):
    run(live._execute_trades(WALLET, live._prices))
    run(live._execute_trades(WALLET, live._prices))
    assert len(live.calls) == 1
    assert any("cooldown" in b for b in run(live._live_blockers(WALLET, live._prices)))


def test_max_trades_per_day(live, monkeypatch):
    monkeypatch.setattr(config, "LIVE_TRADE_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(config, "LIVE_MAX_TRADES_PER_DAY", 2)
    for _ in range(4):
        run(live._execute_trades(WALLET, live._prices))
    assert len(live.calls) == 2


def test_leg2_failure_pauses_and_alerts(live, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_ENABLED", True)
    sent = []

    async def fake_send(content):
        sent.append(content)
        return True

    monkeypatch.setattr(live.discord, "_send", fake_send)
    live._next_result = ArbResult("leg2_failed", "oracle moved", erg_in=10**10, sigusd_cents=310, tx1="t1")
    run(live._execute_trades(WALLET, live._prices))
    assert live._live_paused
    assert sent and "arb.py redeem --sigusd 3.10" in sent[-1]
    row = live.tracker.conn.execute("SELECT status FROM trades").fetchone()
    assert row["status"] == "failed"
    run(live._execute_trades(WALLET, live._prices))
    assert len(live.calls) == 1


def test_drawdown_limit(live, monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_DRAWDOWN_ERG", 5.0)
    run(live._live_blockers({"erg": 50.0, "sigusd": 0, "use": 0}, live._prices))  # sets the baseline
    blockers = run(live._live_blockers({"erg": 44.0, "sigusd": 0, "use": 0}, live._prices))
    assert any("drawdown" in b for b in blockers)


def test_redeem_path_passed_to_runner(live):
    run(live._execute_trades(WALLET, live._prices))
    assert live.calls[0][1]["path"] == "redeem"


def test_mint_path_executed_when_it_is_the_profitable_one(tmp_path, monkeypatch):
    """SigUSD at a premium on the pool and RR far above 400%: mint -> pool sell is the trade."""
    from exchanges.base import PoolState
    from exchanges.sigmausd import BankState
    from tests.test_scanner_paths import ORACLE_R4, make_prices
    monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 1000.0)
    monkeypatch.setattr(config, "LIVE_STOP_FILE", str(tmp_path / "STOP"))
    s = ArbitrageScanner(mode="live", db_path=str(tmp_path / "t.db"))
    calls = []

    async def fake_run(ns, path, erg_in, **kw):
        calls.append(path)
        return ArbResult("executed", erg_in=erg_in, sigusd_cents=100, profit_nanoerg=10**8, profit_percent=1.0,
                         tx1="m1", tx2="m2", path=path)

    async def healthy():
        return dict(HEALTHY)

    monkeypatch.setattr(scanner_module, "run_arb", fake_run)
    monkeypatch.setattr(s.ergo_node, "get_health", healthy)
    state = BankState(bank_erg_nano=5_000_000 * 10**9, sigusd_circ_cents=16_000_000, oracle_r4=ORACLE_R4)  # RR ~1000%
    spot = 0.30  # SigUSD ~7.5% above $1 on the pool
    pool = PoolState(exchange="t", pool_id="p", token_x="ERG", token_y="SigUSD",
                     reserve_x=20_000, reserve_y=20_000 * spot, fee_num=995, fee_denom=1000)
    prices = make_prices(state, spot)
    prices["spectrum_pool"] = pool
    s._find_opportunities(prices)
    for _ in range(config.LIVE_CONFIRM_SCANS):
        s._update_live_streak()
    run(s._execute_trades(WALLET, prices))
    assert calls == ["mint"]
    s.tracker.close()
