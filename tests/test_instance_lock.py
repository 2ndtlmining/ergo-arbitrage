"""One bot per database (#63) and arb.py --execute guardrails (#64)."""
import asyncio

import pytest

import config
import ergo.arb_runner as runner
from instance_lock import InstanceLock, LockHeld, holder
from tests.test_runner_sizing import offline  # noqa: F401  (fixture)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "t.db")


# ---------- the lock ----------

def test_a_second_holder_is_refused_and_told_who_holds_it(db):
    with InstanceLock(db, "live"):
        with pytest.raises(LockHeld) as e:
            InstanceLock(db, "notify").acquire()
        assert e.value.holder["mode"] == "live" and e.value.holder["pid"]
        assert "live" in str(e.value)


def test_release_frees_it(db):
    with InstanceLock(db, "live"):
        assert holder(db)["mode"] == "live"
    assert holder(db) is None
    with InstanceLock(db, "notify"):
        pass


# ---------- main.py ----------

@pytest.fixture
def no_bot(monkeypatch):
    import main
    started = []

    class Fake:
        def __init__(self, *a, **k):
            started.append(k.get("mode"))
            self.state = None

    async def fake_run(scanner, view, once):
        started.append(holder(config_db[0]))

    config_db = []
    monkeypatch.setattr(main, "ArbitrageScanner", Fake)
    monkeypatch.setattr(main, "run", fake_run)
    return main, started, config_db


def test_a_second_live_bot_refuses_to_start(db, no_bot, capsys):
    main, started, _ = no_bot
    with InstanceLock(db, "notify"):
        with pytest.raises(SystemExit) as e:
            main.main(["--live", "--plain", "--db", db])
    assert e.value.code == 2 and started == []
    assert "already running" in capsys.readouterr().err


def test_a_second_notify_bot_refuses_too(db, no_bot):
    main, started, _ = no_bot
    with InstanceLock(db, "notify"):
        with pytest.raises(SystemExit):
            main.main(["--notify", "--plain", "--db", db])
    assert started == []


def test_once_without_live_runs_next_to_a_bot(db, no_bot):
    main, started, config_db = no_bot
    config_db.append(db)
    with InstanceLock(db, "notify"):
        main.main(["--once", "--plain", "--db", db])
    assert started[0] == "monitor"


def test_the_bot_holds_the_lock_while_it_runs_and_releases_it(db, no_bot):
    main, started, config_db = no_bot
    config_db.append(db)
    main.main(["--notify", "--plain", "--db", db])
    assert started[1]["mode"] == "notify"                  # held during run()
    assert holder(db) is None


# ---------- arb.py --execute ----------

@pytest.fixture
def arb_cli(monkeypatch, db, tmp_path):
    import arb
    ran = []

    async def fake(args):
        ran.append((args.command, holder(db)))

    monkeypatch.setattr(arb, "run", fake)
    monkeypatch.setattr(arb, "DEFAULT_DB", db)
    monkeypatch.setattr(config, "LIVE_STOP_FILE", str(tmp_path / "STOP"))
    return arb, ran


def test_execute_refuses_while_a_live_bot_runs(arb_cli, db, capsys):
    arb, ran = arb_cli
    with InstanceLock(db, "live"):
        with pytest.raises(SystemExit) as e:
            arb.main(["redeem", "--sigusd", "1", "--execute"])
    assert e.value.code == 2 and ran == [] and "--ignore-lock" in capsys.readouterr().out


def test_ignore_lock_overrides(arb_cli, db):
    arb, ran = arb_cli
    with InstanceLock(db, "live"):
        arb.main(["redeem", "--sigusd", "1", "--execute", "--ignore-lock"])
    assert [c for c, _ in ran] == ["redeem"]


def test_execute_runs_next_to_a_notify_bot(arb_cli, db):
    arb, ran = arb_cli
    with InstanceLock(db, "notify"):
        arb.main(["redeem", "--sigusd", "1", "--execute"])
    assert [c for c, _ in ran] == ["redeem"]


def test_execute_holds_the_lock_so_live_cannot_start_meanwhile(arb_cli, db):
    arb, ran = arb_cli
    arb.main(["redeem", "--sigusd", "1", "--execute"])
    assert ran[0][1]["mode"] == "arb.py redeem"
    assert holder(db) is None


def test_check_is_allowed_while_live_runs(arb_cli, db):
    arb, ran = arb_cli
    with InstanceLock(db, "live"):
        arb.main(["arb", "--check"])
    assert [c for c, _ in ran] == ["arb"]


def test_arb_execute_refuses_with_the_stop_file(arb_cli):
    arb, ran = arb_cli
    open(config.LIVE_STOP_FILE, "w").close()
    with pytest.raises(SystemExit):
        arb.main(["arb", "--execute"])
    assert ran == []
    arb.main(["redeem", "--sigusd", "all", "--execute"])    # recovery commands still work
    assert [c for c, _ in ran] == ["redeem"]


def test_arb_execute_refuses_while_live_is_paused(arb_cli, db, capsys):
    from arbitrage.scanner import LIVE_PAUSE_KEY
    from tracker.profit_tracker import ProfitTracker
    arb, ran = arb_cli
    t = ProfitTracker(db)
    t.set_meta(LIVE_PAUSE_KEY, "leg 2 failed (oracle moved)")
    t.close()
    with pytest.raises(SystemExit):
        arb.main(["arb", "--execute"])
    assert ran == [] and "leg 2 failed" in capsys.readouterr().out
    arb.main(["redeem", "--sigusd", "all", "--execute"])
    assert [c for c, _ in ran] == ["redeem"]


# ---------- --force --execute below MIN_PROFIT_PERCENT ----------

def run_forced(confirm):
    logs = []
    r = asyncio.run(runner.run_arb(None, "redeem", 3 * 10**9, execute=True, force=True, confirm=confirm,
                                   log=logs.append))
    return r, "\n".join(logs)


def test_forced_loss_needs_confirmation(offline, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "MIN_PROFIT_PERCENT", 50.0)
    asked = []
    r, out = run_forced(lambda msg, erg: asked.append(msg) or False)
    assert r.status == "not_confirmed" and asked and "ERG" in asked[0]


def test_confirmed_forced_trade_goes_on_to_signing(offline, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "MIN_PROFIT_PERCENT", 50.0)
    r, _ = run_forced(lambda msg, erg: True)
    assert r.status == "dry_run"                              # reached guarded_sign


def test_forced_loss_without_a_prompt_is_refused(offline, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "MIN_PROFIT_PERCENT", 50.0)
    r, _ = run_forced(None)
    assert r.status == "not_confirmed"


def test_profitable_trades_are_not_asked(offline):  # noqa: F811
    r, _ = run_forced(lambda msg, erg: pytest.fail("asked"))
    assert r.status == "dry_run"


def test_cli_prompt_wants_the_exact_amount_typed_back(monkeypatch):
    import arb
    monkeypatch.setattr("builtins.input", lambda prompt: "-0.0644")
    assert arb.confirm_loss("expected -0.0644 ERG (-2.15%)", -0.0644)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    assert not arb.confirm_loss("expected -0.0644 ERG (-2.15%)", -0.0644)

    def eof(prompt):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert not arb.confirm_loss("expected -0.0644 ERG", -0.0644)
