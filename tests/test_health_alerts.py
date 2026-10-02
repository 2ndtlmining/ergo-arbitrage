"""Health alerts: thresholds, repeats, recoveries, pings (spec: discord episodes)."""
from types import SimpleNamespace

from notifications.health import HealthMonitor


def state(chain_error=None, venues=(), live="armed", detail=()):
    return SimpleNamespace(chain_error=chain_error, venues=list(venues), live=live, live_detail=list(detail))


def venue(name, st, error=""):
    return SimpleNamespace(name=name, kind="CEX", state=st, error=error)


def monitor():
    return HealthMonitor(chain_s=120, venue_s=300, oracle_s=600, repeat_s=1800, started_at=0)


def test_chain_alert_after_threshold_with_ping_and_recovery():
    m = monitor()
    assert m.update(0, state("timeout")) == [] and m.update(100, state("timeout")) == []
    ev = m.update(120, state("timeout"))
    assert [(e.subject, e.kind, e.ping) for e in ev] == [("chain", "alert", True)] and "timeout" in ev[0].text
    ev = m.update(370, state())
    assert [(e.subject, e.kind, e.ping) for e in ev] == [("chain", "recovered", False)]
    assert "6m 10s" in ev[0].text


def test_short_outage_has_no_alert_and_no_recovery():
    m = monitor()
    m.update(0, state("timeout"))
    assert m.update(60, state()) == []


def test_no_repeat_within_repeat_seconds_then_repeat():
    m = monitor()
    m.update(0, state("x"))
    assert len(m.update(120, state("x"))) == 1
    assert m.update(1000, state("x")) == []
    assert [e.kind for e in m.update(1920, state("x"))] == ["alert"]


def test_cex_venue_down_is_silent_and_chain_venues_are_not_alerted():
    m = monitor()
    down = [venue("Kucoin", "down", "no quote"), venue("ErgoDEX pool", "down", "timeout")]
    m.update(0, state(venues=down))
    ev = m.update(300, state(venues=down))
    assert [(e.subject, e.ping) for e in ev] == [("venue:Kucoin", False)] and "no quote" in ev[0].text


def test_stuck_oracle_update():
    m = monitor()
    pend = [venue("Oracle", "pending")]
    m.update(0, state(venues=pend))
    assert m.update(500, state(venues=pend)) == []
    assert [(e.subject, e.ping) for e in m.update(600, state(venues=pend))] == [("oracle", False)]
    assert [e.kind for e in m.update(650, state(venues=[venue("Oracle", "live")]))] == ["recovered"]


def test_live_pause_alerts_at_once():
    m = monitor()
    ev = m.update(5, state(live="paused", detail=["leg 2 failed (oracle moved)"]))
    assert [(e.subject, e.ping) for e in ev] == [("live", False)] and "leg 2 failed" in ev[0].text


def test_outage_log_for_the_digest():
    m = monitor()
    m.update(0, state("x"))
    m.update(130, state("x"))
    m.update(250, state())
    m.update(300, state(venues=[venue("Kucoin", "down")]))
    m.update(700, state(venues=[venue("Kucoin", "down")]))
    out = m.outages(now=800, since=0)
    assert ("chain", 250.0, False) in out and ("venue:Kucoin", 500.0, True) in out


def test_live_pause_does_not_ping_twice():
    """The live trade alert (notify_live) already pings for the failure that paused trading."""
    m = monitor()
    (e,) = m.update(5, state(live="paused", detail=["leg 2 failed"]))
    assert e.subject == "live" and e.ping is False


def test_old_outages_are_pruned():
    m = monitor()
    m.update(0, state("x"))
    m.update(130, state("x"))
    m.update(250, state())
    m.update(250 + 49 * 3600, state())
    assert m._log == []
