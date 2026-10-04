"""chain_episodes storage and the daily digest (spec: discord episodes)."""
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from notifications.digest import build_digest, digest_due, mark_digest_sent
from notifications.health import HealthMonitor
from tracker.profit_tracker import ProfitTracker


@pytest.fixture
def tracker(tmp_path):
    t = ProfitTracker(str(tmp_path / "t.db"))
    yield t
    t.close()


def ep(label="pool→redeem", peak_erg=1.5, peak_pct=3.5, last=0.3, trade=None):
    return SimpleNamespace(key="ErgoDEX buy->Bank redeem", label=label, peak_erg=peak_erg, peak_percent=peak_pct,
                           peak_size_erg=45.0, profit_percent=last, trade=trade)


def test_chain_episode_roundtrip(tracker):
    e = ep()
    i = tracker.open_chain_episode(e)
    e.peak_erg, e.peak_percent = 2.0, 4.1
    tracker.update_chain_episode(i, e)
    tracker.close_chain_episode(i, ep(peak_erg=2.0, peak_pct=4.1, last=0.2, trade="executed +0.42 ERG"))
    (row,) = tracker.chain_episodes_since("2000-01-01")
    assert row["path"] == "pool→redeem" and row["peak_profit_erg"] == 2.0 and row["closed_at"]
    assert row["trade"] == "executed +0.42 ERG" and row["last_profit_percent"] == 0.2


def test_open_episode_is_closed_after_a_restart(tmp_path):
    t = ProfitTracker(str(tmp_path / "r.db"))
    t.open_chain_episode(ep())
    t.close()
    t2 = ProfitTracker(str(tmp_path / "r.db"))
    try:
        t2.claim_stale_chain_episodes()                      # what the next bot start does
        (row,) = t2.chain_episodes_since("2000-01-01")
        assert row["closed_at"] is not None
    finally:
        t2.close()


def test_meta_roundtrip(tracker):
    assert tracker.get_meta("digest_date") is None
    tracker.set_meta("digest_date", "2026-10-02")
    assert tracker.get_meta("digest_date") == "2026-10-02"


def test_digest_on_a_fresh_database(tracker):
    d = build_digest(tracker, HealthMonitor(120, 300, 600, 1800, started_at=time.time()), {"erg": 20.7},
                     datetime.now())
    assert d.paths == {} and d.potential_erg == 0 and d.trades == {"count": 0, "net_erg": 0.0, "failed": 0}
    assert d.outages == [] and d.wallet == {"erg": 20.7}


def test_digest_with_episodes_trades_and_outages(tracker):
    for peak in (1.0, 2.0):
        i = tracker.open_chain_episode(ep(peak_erg=peak, peak_pct=peak * 2))
        tracker.close_chain_episode(i, ep(peak_erg=peak, peak_pct=peak * 2))
    t1 = tracker.start_trade(None, 10.0, 10.4, 0.4)
    tracker.complete_trade(t1, actual_output=10.42)
    t2 = tracker.start_trade(None, 5.0, 5.1, 0.1)
    tracker.fail_trade(t2, "leg 2 failed")
    now_w = time.time()  # the health log uses wall-clock times, like the scanner feeds it
    health = HealthMonitor(120, 300, 600, 1800, started_at=now_w - 1000)
    health._log = [("chain", now_w - 900, now_w - 650)]
    d = build_digest(tracker, health, None, datetime.now())
    p = d.paths["pool→redeem"]
    assert p["count"] == 2 and p["best_peak_percent"] == 4.0 and d.potential_erg == pytest.approx(3.0)
    assert d.trades["count"] == 2 and d.trades["failed"] == 1 and d.trades["net_erg"] == pytest.approx(0.42)
    assert ("chain", 250.0, False) in d.outages


HOURS = [8, 14, 20]


def test_digest_three_times_a_day_and_after_a_restart(tmp_path):
    t = ProfitTracker(str(tmp_path / "d.db"))
    assert digest_due(t, datetime(2026, 10, 2, 9), HOURS) is None          # first run: the schedule starts
    assert digest_due(t, datetime(2026, 10, 2, 13, 59), HOURS) is None
    slot = digest_due(t, datetime(2026, 10, 2, 14, 1), HOURS)
    assert slot == datetime(2026, 10, 2, 14)
    mark_digest_sent(t, slot)
    t.close()
    t2 = ProfitTracker(str(tmp_path / "d.db"))
    try:
        assert digest_due(t2, datetime(2026, 10, 2, 19, 59), HOURS) is None
        assert digest_due(t2, datetime(2026, 10, 3, 7), HOURS) == datetime(2026, 10, 2, 20)   # missed while down
        assert digest_due(t2, datetime(2026, 10, 3, 7), []) is None                            # digest off
    finally:
        t2.close()


def test_each_digest_covers_the_time_since_the_previous_one():
    from notifications.digest import digest_period_hours
    assert digest_period_hours(datetime(2026, 10, 2, 8), HOURS) == 12
    assert digest_period_hours(datetime(2026, 10, 2, 14), HOURS) == 6
    assert digest_period_hours(datetime(2026, 10, 2, 8), [8]) == 24


def test_digest_hours_setting():
    import config
    assert config.parse_hours("20, 8,14") == [8, 14, 20]
    assert config.parse_hours("off") == [] and config.parse_hours("-1") == [] and config.parse_hours("") == []


def test_digest_without_a_health_monitor(tracker):
    d = build_digest(tracker, None, None, datetime.now())
    assert d.outages == [] and d.outage_since == ""


def test_restart_keeps_the_message_id_and_last_seen_time(tmp_path):
    t = ProfitTracker(str(tmp_path / "s.db"))
    i = t.open_chain_episode(ep())
    t.set_chain_episode_message(i, "m42")
    t.update_chain_episode(i, ep(peak_erg=2.0, peak_pct=4.0))
    t.close()
    t2 = ProfitTracker(str(tmp_path / "s.db"))
    try:
        (stale,) = t2.claim_stale_chain_episodes()
        assert stale["message_id"] == "m42" and stale["peak_profit_percent"] == 4.0
        (row,) = t2.chain_episodes_since("2000-01-01")
        assert row["closed_at"] == row["last_seen_at"] and row["closed_at"] > row["opened_at"]
    finally:
        t2.close()


from exchanges.sigmausd import BankState


def test_digest_mint_line_from_the_bank_state(tracker):
    blocked = {"state": BankState(1_006_250 * 10**9, 10_000_000, 3_125_000_000)}
    d = build_digest(tracker, None, None, datetime.now(), bank=blocked)
    assert d.mint == "✗ needs ERG $0.398 (+24.2%)"
    assert build_digest(tracker, None, None, datetime.now()).mint is None   # bank not read yet



def test_a_second_process_does_not_close_the_running_bots_episodes(tmp_path):
    """Review: opening the tracker (e.g. a --once run) must not claim another process's open episodes."""
    t = ProfitTracker(str(tmp_path / "p.db"))
    t.open_chain_episode(ep())
    other = ProfitTracker(str(tmp_path / "p.db"))
    try:
        (row,) = t.chain_episodes_since("2000-01-01")
        assert row["closed_at"] is None
        (claimed,) = other.claim_stale_chain_episodes()      # only the long-running bot calls this
        assert claimed["closed_at"] and claimed["path"] == "pool→redeem"
        assert other.claim_stale_chain_episodes() == []
    finally:
        other.close()
        t.close()


def test_touch_keeps_the_last_seen_time(tracker):
    i = tracker.open_chain_episode(ep())
    tracker.touch_chain_episode(i)
    (row,) = tracker.chain_episodes_since("2000-01-01")
    assert row["last_seen_at"] and row["last_seen_at"] >= row["opened_at"]
