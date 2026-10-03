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


# ---------- silent failures (#71) ----------

def quiet(**kw):
    s = state()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_wallet_locked_alerts_and_pings_in_live_mode():
    m = monitor()
    m.update(0, quiet(wallet_locked=True, mode="live"))
    ev = m.update(120, quiet(wallet_locked=True, mode="live"))
    assert [(e.subject, e.ping) for e in ev] == [("wallet", True)] and "/wallet/unlock" in ev[0].text
    ev = m.update(200, quiet(wallet_locked=False, mode="live"))
    assert [(e.subject, e.kind) for e in ev] == [("wallet", "recovered")] and "Wallet" in ev[0].text


def test_wallet_locked_is_not_an_alert_outside_live_mode():
    m = monitor()                                    # a watch-only bot may run with the wallet locked
    m.update(0, quiet(wallet_locked=True, mode="notify"))
    assert m.update(120, quiet(wallet_locked=True, mode="notify")) == []


def test_full_scan_failing_alerts_after_the_threshold():
    m = monitor()
    assert m.update(0, quiet(scan_error="OperationalError: database is locked")) == []
    ev = m.update(120, quiet(scan_error="OperationalError: database is locked"))
    assert [e.subject for e in ev] == ["scan"] and "database is locked" in ev[0].text
    assert [e.kind for e in m.update(130, quiet())] == ["recovered"]


def test_database_writes_failing_alerts():
    m = monitor()
    m.update(0, quiet(db_error="disk I/O error"))
    ev = m.update(120, quiet(db_error="disk I/O error"))
    assert [e.subject for e in ev] == ["database"] and "not being recorded" in ev[0].text


def test_stop_file_alert_after_live_threshold():
    m = HealthMonitor(chain_s=120, venue_s=300, oracle_s=600, repeat_s=1800, started_at=0, live_s=600)
    guards = ["STOP file present (/bot/STOP)"]
    assert m.update(0, quiet(live_guards=guards, mode="live")) == []
    ev = m.update(600, quiet(live_guards=guards, mode="live"))
    assert [(e.subject, e.ping) for e in ev] == [("live:stop", False)] and "STOP" in ev[0].text


def test_drawdown_alerts_at_once_with_a_ping():
    ev = monitor().update(0, quiet(live_guards=["drawdown 6.00 ERG > LIVE_MAX_DRAWDOWN_ERG 5"], mode="live"))
    assert [(e.subject, e.ping) for e in ev] == [("live:drawdown", True)]


def test_daily_cap_alerts_at_once_without_a_ping():
    ev = monitor().update(0, quiet(live_guards=["max 10 trades per day reached"], mode="live"))
    assert [(e.subject, e.ping) for e in ev] == [("live:daily", False)]


def test_cooldown_and_oracle_guards_do_not_alert():
    m = monitor()
    guards = ["cooldown 120s", "oracle update pending (waiting for it to confirm)"]
    assert m.update(0, quiet(live_guards=guards, mode="live")) == []
    assert m.update(5000, quiet(live_guards=guards, mode="live")) == []
