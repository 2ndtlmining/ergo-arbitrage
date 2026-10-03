"""Live limits that hold across restarts (#59), validated live settings (#60), MAX_FEE_BUDGET_ERG (#61)."""
import asyncio
from datetime import datetime, timedelta

import pytest

import arbitrage.scanner as scanner_module
import config
from arbitrage.scanner import ArbitrageScanner
from ergo.arb_runner import ArbResult
from tests.test_live import HEALTHY, WALLET
from tests.test_optimizer import discount_prices


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def start(tmp_path, monkeypatch):
    """start() = a --live bot on the same database each time, as after a restart."""
    monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 1000.0)
    monkeypatch.setattr(config, "LIVE_STOP_FILE", str(tmp_path / "STOP"))
    calls, bots = [], []

    async def fake_run(ns, path, erg_in, **kw):
        calls.append(path)
        return bots[-1]._next_result

    monkeypatch.setattr(scanner_module, "run_arb", fake_run)

    def make(mode="live"):
        s = ArbitrageScanner(mode=mode, db_path=str(tmp_path / "t.db"))

        async def healthy():
            return dict(HEALTHY)

        monkeypatch.setattr(s.ergo_node, "get_health", healthy)
        s._next_result = ArbResult("executed", erg_in=10**9, sigusd_cents=100, profit_nanoerg=10**8,
                                   profit_percent=1.0, tx1="t1", tx2="t2")
        s.calls = calls
        prices = discount_prices(pool_erg=2_000)
        s._find_opportunities(prices)
        s._prices = prices
        for _ in range(config.LIVE_CONFIRM_POLLS):
            s._update_live_streak()
        bots.append(s)
        return s

    yield make
    for s in bots:
        s.tracker.close()


def blockers(s, wallet=WALLET):
    return run(s._live_blockers(wallet, s._prices))


# ---------- #59: limits survive a restart ----------

def test_a_restart_after_a_leg2_failure_stays_paused(start):
    bot = start()
    bot._next_result = ArbResult("leg2_failed", "oracle moved", erg_in=10**10, sigusd_cents=310, tx1="t1")
    run(bot._execute_trades(WALLET, bot._prices))
    again = start()
    assert again._live_paused and "leg 2 failed" in again._live_paused
    assert any("paused" in b and "arb.py resume" in b for b in blockers(again))
    run(again._execute_trades(WALLET, again._prices))
    assert len(again.calls) == 1                                    # only the first bot's trade


def test_daily_count_and_cooldown_carry_over(start, monkeypatch):
    bot = start()
    run(bot._execute_trades(WALLET, bot._prices))
    again = start()
    assert again._trades_today_count() == 1
    assert any("cooldown" in b for b in blockers(again))
    monkeypatch.setattr(config, "LIVE_TRADE_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(config, "LIVE_MAX_TRADES_PER_DAY", 1)
    assert any("max 1 trades per day" in b for b in blockers(start()))


def test_trades_from_yesterday_do_not_count(start):
    bot = start()
    run(bot._execute_trades(WALLET, bot._prices))
    yesterday = (datetime.now() - timedelta(days=1)).isoformat()
    bot.tracker.conn.execute("UPDATE trades SET started_at = ?", (yesterday,))
    bot.tracker.conn.commit()
    assert start()._trades_today_count() == 0


def test_a_trade_left_executing_by_a_killed_bot_pauses_the_next_start(start):
    bot = start()
    trade_id = bot.tracker.start_trade(None, 10.0, 10.1, 0.1)       # the process died mid-trade
    again = start()
    assert again._live_paused and f"trade #{trade_id}" in again._live_paused
    row = again.tracker.conn.execute("SELECT status FROM trades WHERE id = ?", (trade_id,)).fetchone()
    assert row["status"] == "failed"                                # marked, so it is reported once
    assert start()._live_paused                                     # still paused: only resume clears it


def test_resume_clears_the_pause(start, tmp_path, capsys):
    import arb
    bot = start()
    bot._next_result = ArbResult("error", "boom", erg_in=10**9)
    run(bot._execute_trades(WALLET, bot._prices))
    arb.main(["resume", "--db", str(tmp_path / "t.db")])
    assert "runner error" in capsys.readouterr().out                # says what it cleared
    assert start()._live_paused is None


def test_resume_without_a_pause_says_so(tmp_path, capsys):
    import arb
    from tracker.profit_tracker import ProfitTracker
    ProfitTracker(str(tmp_path / "t.db")).close()
    arb.main(["resume", "--db", str(tmp_path / "t.db")])
    assert "not paused" in capsys.readouterr().out


def test_drawdown_baseline_survives_a_restart(start, monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_DRAWDOWN_ERG", 5.0)
    blockers(start(), {"erg": 50.0, "sigusd": 0, "use": 0})        # sets today's baseline
    assert any("drawdown" in b for b in blockers(start(), {"erg": 44.0, "sigusd": 0, "use": 0}))


def test_yesterdays_baseline_is_replaced(start, monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_DRAWDOWN_ERG", 5.0)
    bot = start()
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    bot.tracker.set_meta("live_baseline", f'{{"day": "{yesterday}", "value": 100.0}}')
    assert not any("drawdown" in b for b in blockers(start(), {"erg": 44.0, "sigusd": 0, "use": 0}))


def test_notify_mode_ignores_live_state(start):
    bot = start()
    bot.tracker.start_trade(None, 10.0, 10.1, 0.1)
    assert start(mode="notify")._live_paused is None


# ---------- #60: dangerous live settings ----------

@pytest.mark.parametrize("name,value,needle", [
    ("SLIPPAGE_TOLERANCE", 1.0, "SLIPPAGE_TOLERANCE"),
    ("SLIPPAGE_TOLERANCE", 0.0, "SLIPPAGE_TOLERANCE"),
    ("MIN_PROFIT_PERCENT", 0.0, "MIN_PROFIT_PERCENT"),
    ("MAX_TRADE_SIZE_ERG", 10_000.0, "MAX_TRADE_SIZE_ERG"),
    ("MAX_TRADE_SIZE_ERG", 0.0, "MAX_TRADE_SIZE_ERG"),
    ("LIVE_ERG_RESERVE", -1.0, "LIVE_ERG_RESERVE"),
    ("LIVE_CONFIRM_POLLS", 1, "LIVE_CONFIRM_POLLS"),
    ("LIVE_TRADE_COOLDOWN_SECONDS", 0, "LIVE_TRADE_COOLDOWN_SECONDS"),
    ("LIVE_MAX_DRAWDOWN_ERG", 0.0, "LIVE_MAX_DRAWDOWN_ERG"),
    ("LIVE_MAX_DRAWDOWN_ERG", 500.0, "LIVE_MAX_DRAWDOWN_ERG"),
    ("MAX_FEE_BUDGET_ERG", 0.0, "MAX_FEE_BUDGET_ERG"),
    ("MAX_FEE_BUDGET_ERG", 5.0, "MAX_FEE_BUDGET_ERG"),
    ("EXECUTION_BUFFER", 0.5, "EXECUTION_BUFFER"),
])
def test_dangerous_values_are_refused(monkeypatch, name, value, needle):
    monkeypatch.setattr(config, name, value)
    errors = config.live_config_errors()
    assert errors and any(needle in e for e in errors)


def test_the_defaults_pass():
    assert config.live_config_errors() == []


def test_live_refuses_to_start_before_connecting(monkeypatch, capsys):
    import main
    monkeypatch.setattr(config, "SLIPPAGE_TOLERANCE", 1.0)
    monkeypatch.setattr(main, "ArbitrageScanner", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(SystemExit) as e:
        main.main(["--live", "--plain"])
    assert e.value.code == 2 and "SLIPPAGE_TOLERANCE" in capsys.readouterr().err


def test_max_trade_erg_may_only_lower_the_cap(monkeypatch, capsys):
    import main
    monkeypatch.setattr(main, "ArbitrageScanner", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(SystemExit) as e:
        main.main(["--notify", "--max-trade-erg", "500"])               # .env cap is 100 in tests
    assert e.value.code == 2 and "--max-trade-erg" in capsys.readouterr().err


def test_max_trade_erg_lowers_the_cap(monkeypatch):
    import main
    monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 100.0)
    main.apply_overrides(main.build_parser().parse_args(["--max-trade-erg", "5"]))
    assert config.MAX_TRADE_SIZE_ERG == 5.0


def test_arb_execute_refuses_dangerous_settings(monkeypatch, capsys):
    import arb
    monkeypatch.setattr(config, "SLIPPAGE_TOLERANCE", 1.0)

    async def never(*a, **k):
        pytest.fail("must not run")

    monkeypatch.setattr(arb, "run", never)
    with pytest.raises(SystemExit) as e:
        arb.main(["redeem", "--sigusd", "1", "--execute"])
    assert e.value.code == 2 and "SLIPPAGE_TOLERANCE" in capsys.readouterr().out


def test_arb_dry_run_and_check_are_allowed(monkeypatch):
    import arb
    monkeypatch.setattr(config, "SLIPPAGE_TOLERANCE", 1.0)
    ran = []

    async def fake(args):
        ran.append(args.command)

    monkeypatch.setattr(arb, "run", fake)
    arb.main(["redeem", "--sigusd", "1", "--check"])
    assert ran == ["redeem"]


# ---------- #61: MAX_FEE_BUDGET_ERG ----------

def test_fee_budget_setting_bounds_the_service_fee(monkeypatch):
    from ergo.tx_guard import SignPolicy
    monkeypatch.setattr(config, "MAX_FEE_BUDGET_ERG", 0.05)
    policy = SignPolicy(max_erg_spent=10**9)
    assert policy.max_service_fee == 50_000_000


def test_guard_refuses_fees_over_the_budget(monkeypatch):
    from ergo.tx_guard import TxGuardError, check_fee_budget
    monkeypatch.setattr(config, "MAX_FEE_BUDGET_ERG", 0.05)
    check_fee_budget(service_fee=40_000_000, miner_fee=1_100_000)
    with pytest.raises(TxGuardError, match="MAX_FEE_BUDGET_ERG"):
        check_fee_budget(service_fee=785_000_000, miner_fee=1_100_000)
