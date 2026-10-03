"""ProfitTracker episode logging, daily summary and schema (issue #6)."""
from datetime import datetime, timedelta

import pytest

from arbitrage.calculator import ArbitrageOpportunity, FeeBreakdown
from tracker.profit_tracker import ProfitTracker


def opp(path: str, size: float, profit: float) -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        path=f"{path} [{size:g} ERG]", input_erg=size, output_erg=size + profit,
        profit_erg=profit, profit_percent=profit / size * 100, fees=FeeBreakdown(),
        source_price=0, target_price=0, source_exchange="a", target_exchange="b",
        is_profitable=True,
    )


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "t.db")


@pytest.fixture
def tracker(db):
    t = ProfitTracker(db)
    yield t
    t.close()


class TestEpisodes:
    def test_path_key_strips_size(self):
        assert opp("Bank mint->Spectrum sell", 25, 1).path_key == "Bank mint->Spectrum sell"

    def test_long_opportunity_is_one_episode(self, tracker):
        for _ in range(40):
            tracker.record_scan([opp("A", 25, 1.0), opp("A", 50, 1.5)], scan_number=1)
        rows = tracker.conn.execute("SELECT * FROM opportunity_episodes").fetchall()
        assert len(rows) == 1
        assert rows[0]["scans"] == 40
        assert rows[0]["best_profit_erg"] == pytest.approx(1.5)
        assert rows[0]["best_size_erg"] == 50
        stats = tracker.get_session_stats()
        assert stats["opportunities_seen"] == 1
        assert stats["total_potential_profit_erg"] == pytest.approx(1.5)

    def test_episode_closes_and_reopens(self, tracker):
        tracker.record_scan([opp("A", 10, 0.5)], 1)
        tracker.record_scan([], 2)
        tracker.record_scan([opp("A", 10, 0.7)], 3)
        rows = tracker.conn.execute("SELECT closed_at FROM opportunity_episodes ORDER BY id").fetchall()
        assert len(rows) == 2
        assert rows[0]["closed_at"] is not None
        assert rows[1]["closed_at"] is None
        assert tracker.get_session_stats()["total_potential_profit_erg"] == pytest.approx(1.2)

    def test_unprofitable_and_blocked_are_ignored(self, tracker):
        o = opp("A", 10, 0.5)
        o.blocked = True
        p = opp("B", 10, -0.5)
        p.is_profitable = False
        tracker.record_scan([o, p], 1)
        assert tracker.conn.execute("SELECT COUNT(*) FROM opportunity_episodes").fetchone()[0] == 0


class TestDailySummary:
    def test_scans_accumulate_across_restarts(self, db):
        a = ProfitTracker(db)
        for _ in range(3):
            a.update_daily_summary(opportunities=0)
        a.close()
        b = ProfitTracker(db)
        for _ in range(2):
            b.update_daily_summary(opportunities=0)
        total = b.conn.execute("SELECT total_scans FROM daily_summary").fetchone()[0]
        b.close()
        assert total == 5


class TestSchema:
    def test_wal_indexes_and_version(self, tracker):
        assert tracker.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert tracker.conn.execute("PRAGMA user_version").fetchone()[0] >= 1
        names = {r[0] for r in tracker.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"ix_scan_results_ts", "ix_opportunities_ts"} <= names

    def test_prune_keeps_profitable_rows(self, tracker):
        old = (datetime.now() - timedelta(days=30)).isoformat()
        for profitable in (0, 1):
            tracker.conn.execute(
                "INSERT INTO scan_results (timestamp, scan_number, path, input_erg, output_erg,"
                " profit_erg, profit_percent, is_profitable) VALUES (?, 1, 'p', 1, 1, 0, 0, ?)",
                (old, profitable),
            )
        tracker.log_scan_results([opp("A", 10, 0.1)], 2)
        deleted, _ = tracker.prune(days=7)
        assert deleted == 1
        assert tracker.conn.execute("SELECT COUNT(*) FROM scan_results").fetchone()[0] == 2
