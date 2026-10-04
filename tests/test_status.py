"""`arb.py status`: is the bot running and is everything OK, from the database alone (no node needed)."""
from datetime import datetime, timedelta

import pytest

import config
from bot_status import status_lines
from instance_lock import InstanceLock
from tracker.profit_tracker import ProfitTracker

NOW = datetime(2026, 10, 5, 15, 30)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIVE_STOP_FILE", str(tmp_path / "STOP"))
    monkeypatch.setattr(config, "DISCORD_DIGEST_HOURS", [8, 14, 20])
    path = str(tmp_path / "t.db")
    ProfitTracker(path).close()
    return path


def text(db, now=NOW):
    return "\n".join(status_lines(db, now=now))


def test_a_bot_that_is_not_running(db):
    out = text(db)
    assert "not running" in out and "no scan recorded yet" in out


def test_a_running_bot_with_its_mode(db):
    with InstanceLock(db, "notify"):
        out = text(db)
    assert "running" in out and "notify" in out and "not running" not in out


def test_last_scan_age(db):
    t = ProfitTracker(db)
    t.conn.execute("INSERT INTO price_snapshots (timestamp, spectrum_erg_sigusd, oracle_erg_usd, bank_reserve_ratio)"
                   " VALUES (?, 0.3127, 0.3242, 316.0)", ((NOW - timedelta(seconds=42)).isoformat(),))
    t.conn.commit()
    t.close()
    out = text(db)
    assert "42s ago" in out and "RR 316%" in out


def test_an_old_last_scan_is_flagged(db):
    t = ProfitTracker(db)
    t.conn.execute("INSERT INTO price_snapshots (timestamp) VALUES (?)", ((NOW - timedelta(hours=3)).isoformat(),))
    t.conn.commit()
    t.close()
    assert "stale" not in text(db).lower()                     # a stopped bot is not expected to scan
    with InstanceLock(db, "notify"):
        out = text(db)
    assert "3h ago" in out and "stale" in out.lower()


def test_open_opportunities_and_todays_trades(db):
    t = ProfitTracker(db)
    t.conn.execute("INSERT INTO chain_episodes (path, opened_at, peak_profit_erg, peak_profit_percent, peak_size_erg)"
                   " VALUES ('pool→redeem', ?, 0.4, 2.1, 20)", ((NOW - timedelta(minutes=3)).isoformat(),))
    t.conn.execute("INSERT INTO trades (started_at, status, input_erg, actual_profit_erg) VALUES (?, 'completed', 10, 0.2)",
                   (NOW.replace(hour=9).isoformat(),))
    t.conn.execute("INSERT INTO trades (started_at, status, input_erg) VALUES (?, 'failed', 10)",
                   (NOW.replace(hour=10).isoformat(),))
    t.conn.commit()
    t.close()
    out = text(db)
    assert "pool→redeem" in out and "+2.10%" in out and "3m" in out
    assert "2 today" in out and "+0.2000 ERG" in out and "1 failed" in out


def test_pause_and_stop_file_are_shown(db):
    t = ProfitTracker(db)
    t.set_meta("live_paused", "leg 2 failed (oracle moved)")
    t.close()
    open(config.LIVE_STOP_FILE, "w").close()
    out = text(db)
    assert "PAUSED" in out and "leg 2 failed" in out and "arb.py resume" in out
    assert "STOP file present" in out


def test_next_digest(db):
    assert "next digest 20:00" in text(db)
    assert "next digest 08:00 tomorrow" in text(db, now=NOW.replace(hour=21))


def test_reading_status_changes_nothing(db):
    import os
    before = os.path.getmtime(db)
    text(db)
    assert os.path.getmtime(db) == before


def test_cli(db, capsys):
    import arb
    arb.main(["status", "--db", db])
    assert "Bot" in capsys.readouterr().out
