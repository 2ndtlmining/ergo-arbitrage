"""Startup summary and the --live arming step (#65); silent failures surfaced (#71)."""
import asyncio
import logging

import pytest

import config
from arbitrage.scanner import ArbitrageScanner


@pytest.fixture
def bot(tmp_path):
    made = []

    def make(mode="notify"):
        s = ArbitrageScanner(mode=mode, db_path=str(tmp_path / "t.db"), view="dashboard")
        made.append(s)
        return s

    yield make
    for s in made:
        s.tracker.close()


def run(coro):
    return asyncio.run(coro)


# ---------- #65 startup summary ----------

def test_startup_summary_lists_the_limits_in_force(bot, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_ENABLED", False)
    text = "\n".join(bot("live").startup_lines())
    for needle in ("LIVE", config.ERGO_NODE_URL, "max trade 100 ERG", "reserve 1 ERG", "drawdown 5 ERG",
                   "10 trades/day", "slippage 1.0%", "min profit 0.5%", "STOP", "Discord: NOT CONFIGURED"):
        assert needle in text, needle


def test_startup_summary_reaches_the_log_in_every_view(bot, caplog):
    s = bot("notify")
    with caplog.at_level(logging.INFO, logger="ergo_arb.scanner"):
        s._log_startup()
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "NOTIFY" in logged and "max trade" in logged


def test_live_startup_embed_shows_the_limits():
    from notifications.embeds import startup_embed
    fields = {f["name"]: f["value"] for f in startup_embed("live")["fields"]}
    assert "100" in fields["Max trade"] and "Reserve" in fields and "Drawdown" in fields
    assert "Trades/day" in fields and "Slippage" in fields


def test_dashboard_header_shows_discord():
    from arbitrage.dashboard_state import DashboardState
    from arbitrage.dashboard_view import header
    s = DashboardState("notify")
    s.discord_on = False
    assert "discord off" in header(s).plain
    s.discord_on = True
    assert "discord ●" in header(s).plain


# ---------- #65 arming --live ----------

@pytest.fixture
def no_start(monkeypatch, tmp_path):
    import main
    started = []

    class Fake:
        def __init__(self, *a, **k):
            started.append(k.get("mode"))
            self.state = None

    async def fake_run(scanner, view, once):
        pass

    monkeypatch.setattr(main, "ArbitrageScanner", Fake)
    monkeypatch.setattr(main, "run", fake_run)
    return main, started, str(tmp_path / "t.db")


def test_live_does_not_arm_without_typing_live(no_start, monkeypatch, capsys):
    main, started, db = no_start
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    with pytest.raises(SystemExit) as e:
        main.main(["--live", "--plain", "--db", db])
    assert e.value.code == 1 and started == []
    out = capsys.readouterr().out
    assert "max trade" in out and "not armed" in out.lower()


def test_typing_live_arms_it(no_start, monkeypatch):
    main, started, db = no_start
    monkeypatch.setattr("builtins.input", lambda prompt: "LIVE")
    main.main(["--live", "--plain", "--db", db])
    assert started == ["live"]


def test_no_terminal_does_not_arm(no_start, monkeypatch):
    main, started, db = no_start

    def eof(prompt):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    with pytest.raises(SystemExit):
        main.main(["--live", "--plain", "--db", db])
    assert started == []


def test_yes_arms_unattended_runs(no_start, monkeypatch):
    main, started, db = no_start
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked"))
    main.main(["--live", "--yes", "--plain", "--db", db])
    assert started == ["live"]


def test_notify_is_not_asked(no_start, monkeypatch):
    main, started, db = no_start
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked"))
    main.main(["--notify", "--plain", "--db", db])
    assert started == ["notify"]


# ---------- #71 silent failures ----------

def test_a_failing_state_refresh_still_runs_the_discord_tick(bot, monkeypatch):
    s = bot()
    ticked = []

    async def ok(now):
        pass

    def broken(now):
        raise RuntimeError("refresh broke")

    monkeypatch.setattr(s, "_poll", ok)
    monkeypatch.setattr(s, "_refresh_state", broken)
    monkeypatch.setattr(s, "_discord_tick", lambda now: ticked.append(now))
    run(s.poll_once(100.0))
    assert ticked == [100.0]


def test_a_failing_poll_is_recorded_and_cleared(bot, monkeypatch):
    s = bot()

    async def fail(now):
        raise OSError("database is locked")

    async def ok(now):
        pass

    monkeypatch.setattr(s, "_poll", fail)
    with pytest.raises(OSError):
        run(s.poll_once(1.0))
    assert "database is locked" in s.state.scan_error
    monkeypatch.setattr(s, "_poll", ok)
    run(s.poll_once(2.0))
    assert s.state.scan_error is None


def test_a_repeating_error_logs_one_traceback_then_a_line_per_interval(bot, caplog):
    s = bot()
    err = OSError("database is locked")
    with caplog.at_level(logging.ERROR, logger="ergo_arb.scanner"):
        s._log_poll_error(err, now=0)
        s._log_poll_error(err, now=2)
        s._log_poll_error(err, now=100)
        s._log_poll_error(err, now=301)
    records = [r for r in caplog.records if "database is locked" in r.getMessage()]
    assert len(records) == 2
    assert records[0].exc_info is not None and records[1].exc_info is None
    assert "repeated" in records[1].getMessage()


def test_a_new_error_logs_its_traceback_at_once(bot, caplog):
    s = bot()
    with caplog.at_level(logging.ERROR, logger="ergo_arb.scanner"):
        s._log_poll_error(OSError("a"), now=0)
        s._log_poll_error(ValueError("b"), now=1)
    assert sum(1 for r in caplog.records if r.exc_info) == 2


def test_database_write_errors_skip_the_write_and_are_reported(bot):
    s = bot()

    def broken(*a):
        raise OSError("disk I/O error")

    assert s._db_write(broken, 1) is None
    assert "disk I/O error" in s._db_error
    assert s._db_write(lambda x: x + 1, 1) == 2
    assert s._db_error is None


def test_wallet_lock_and_live_guards_reach_the_state(bot, monkeypatch, tmp_path):
    s = bot("live")
    monkeypatch.setattr(config, "LIVE_STOP_FILE", str(tmp_path / "STOP"))
    open(config.LIVE_STOP_FILE, "w").close()

    async def locked():
        return {"reachable": True, "synced": True, "unlocked": False, "height": 1, "headers": 1,
                "ok_to_trade": False}

    monkeypatch.setattr(s.ergo_node, "get_health", locked)
    run(s._refresh_node_health())
    s._refresh_state(0.0)
    assert s.state.wallet_locked is True
    assert any(g.startswith("STOP file") for g in s.state.live_guards)
    assert s.state.mode == "live"
