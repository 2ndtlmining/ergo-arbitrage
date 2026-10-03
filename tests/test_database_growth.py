"""Database growth: daily prune incl. old snapshots, checkpoint, grouped commits (#73)."""
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from arbitrage.scanner import ArbitrageScanner
from tracker.profit_tracker import ProfitTracker

NOW = datetime(2026, 10, 30, 12, 0)


@pytest.fixture
def tracker(tmp_path):
    t = ProfitTracker(str(tmp_path / "t.db"))
    yield t
    t.close()


def add_scan_day(t, day: datetime, rows: int = 200, profitable_every: int = 0):
    ts = day.isoformat()
    cur = t.conn.execute("INSERT INTO price_snapshots (timestamp) VALUES (?)", (ts,))
    snap = cur.lastrowid
    t.conn.executemany(
        "INSERT INTO scan_results (timestamp, scan_number, snapshot_id, path, input_erg, output_erg, profit_erg,"
        " profit_percent, is_profitable) VALUES (?, 1, ?, 'some path [10 ERG]', 10, 10, 0, 0, ?)",
        [(ts, snap, 1 if profitable_every and i % profitable_every == 0 else 0) for i in range(rows)])
    t.conn.commit()
    return snap


def count(t, table):
    return t.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_prune_removes_old_rows_and_unreferenced_snapshots(tracker):
    old = add_scan_day(tracker, NOW - timedelta(days=20))                    # all non-profitable
    kept = add_scan_day(tracker, NOW - timedelta(days=20), rows=3, profitable_every=1)
    recent = add_scan_day(tracker, NOW - timedelta(days=1))
    rows, snaps = tracker.prune(days=14, now=NOW)
    assert rows == 200 and snaps == 1
    ids = {r[0] for r in tracker.conn.execute("SELECT id FROM price_snapshots")}
    assert old not in ids and {kept, recent} <= ids                          # referenced or recent stay
    assert count(tracker, "scan_results") == 3 + 200


def test_prune_keeps_snapshots_an_opportunity_points_to(tracker):
    snap = add_scan_day(tracker, NOW - timedelta(days=20), rows=1)
    tracker.conn.execute("INSERT INTO opportunities (timestamp, path, input_erg, output_erg, profit_erg, "
                         "profit_percent, snapshot_id) VALUES (?, 'p', 1, 1.1, 0.1, 10, ?)",
                         ((NOW - timedelta(days=20)).isoformat(), snap))
    tracker.conn.commit()
    tracker.prune(days=14, now=NOW)
    assert snap in {r[0] for r in tracker.conn.execute("SELECT id FROM price_snapshots")}


def test_database_size_stays_bounded_over_a_long_run(tmp_path):
    t = ProfitTracker(str(tmp_path / "long.db"))
    sizes = []
    start = NOW - timedelta(days=60)
    for d in range(60):
        day = start + timedelta(days=d)
        add_scan_day(t, day, rows=1500)
        t.prune(days=14, now=day)
        sizes.append(os.path.getsize(tmp_path / "long.db") + os.path.getsize(tmp_path / "long.db-wal")
                     if os.path.exists(tmp_path / "long.db-wal") else os.path.getsize(tmp_path / "long.db"))
    t.close()
    assert sizes[-1] <= sizes[20] * 1.2                                      # flat once retention is reached


def test_a_scan_batch_commits_once(tracker, tmp_path):
    other = sqlite3.connect(tracker.db_path)
    with tracker.batch():
        add_scan_day_uncommitted = tracker.conn.execute("INSERT INTO price_snapshots (timestamp) VALUES ('x')")
        tracker.log_scan_results([], 1)                                       # would commit on its own
        assert other.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == 0
    assert other.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == 1
    assert add_scan_day_uncommitted is not None
    other.close()


def test_a_failing_batch_is_rolled_back(tracker):
    with pytest.raises(RuntimeError):
        with tracker.batch():
            tracker.conn.execute("INSERT INTO price_snapshots (timestamp) VALUES ('x')")
            raise RuntimeError("boom")
    assert count(tracker, "price_snapshots") == 0


def test_the_scanner_prunes_once_a_day(tmp_path, monkeypatch):
    s = ArbitrageScanner(mode="monitor", db_path=str(tmp_path / "s.db"))
    calls = []
    monkeypatch.setattr(s.tracker, "prune", lambda days, now=None: calls.append(days) or (0, 0))
    s._maybe_prune(datetime(2026, 10, 4, 9))
    s._maybe_prune(datetime(2026, 10, 4, 23))
    s._maybe_prune(datetime(2026, 10, 5, 0, 1))
    assert len(calls) == 2
    s.tracker.close()


def test_unused_queries_are_gone():
    for name in ("get_recent_opportunities", "get_opportunity_frequency", "get_opportunity_history"):
        assert not hasattr(ProfitTracker, name)
