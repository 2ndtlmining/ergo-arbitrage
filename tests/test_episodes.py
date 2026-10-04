"""Episode lifecycle: open after confirm, throttled edits, close with hysteresis (spec: discord episodes)."""
from types import SimpleNamespace

from notifications.episodes import EpisodeTracker

KEY = "ErgoDEX buy->Bank redeem"


def row(pct=3.0, erg=1.2, status="GO"):
    choice = SimpleNamespace(size_erg=40.0, profit_erg=erg, profit_percent=pct, break_even_erg=0.1, ok=status == "GO")
    return {KEY: SimpleNamespace(label="pool→redeem", status=status, choice=choice, steps=["a", "b"])}


def tracker():
    return EpisodeTracker(confirm_s=10, close_s=10, edit_s=30, min_percent=1.0, min_erg=0.5)


def kinds(events):
    return [e.kind for e in events]


def test_opens_only_after_confirm_seconds():
    t = tracker()
    assert t.update(0, row()) == [] and t.update(8, row()) == []
    ev = t.update(10, row())
    assert kinds(ev) == ["open"] and ev[0].episode.opened_at == 0 and ev[0].episode.steps == ["a", "b"]


def test_a_blip_below_confirm_sends_nothing():
    t = tracker()
    t.update(0, row())
    t.update(4, row(status="no edge"))
    assert t.update(12, row()) == []  # confirm restarted at 12
    assert kinds(t.update(22, row())) == ["open"]


def test_thresholds_apply():
    t = tracker()
    for now in (0, 10, 20):
        assert t.update(now, row(pct=0.8)) == []   # below DISCORD_MIN_PROFIT_PERCENT
        assert t.update(now, row(erg=0.3)) == []   # below DISCORD_MIN_PROFIT_ERG


def test_edits_are_throttled_by_time_and_change():
    t = tracker()
    t.update(0, row(3.0))
    t.update(10, row(3.0))                          # open
    assert t.update(20, row(3.5)) == []              # < 30 s since the open
    assert t.update(40, row(3.05)) == []             # 30 s passed but change <= 0.1 pp
    ev = t.update(45, row(3.5))
    assert kinds(ev) == ["update"] and ev[0].episode.profit_percent == 3.5
    assert t.update(50, row(4.0)) == []              # throttled again


def test_close_needs_close_seconds_and_requalifying_cancels_it():
    t = tracker()
    t.update(0, row())
    t.update(10, row())
    assert t.update(12, row(status="no edge")) == []
    assert t.update(18, row()) == []                 # back within 10 s: still open, no close
    assert t.update(20, row(status="no edge")) == []
    ev = t.update(30, row(status="no edge"))
    assert kinds(ev) == ["close"] and ev[0].episode.closed_at == 30 and KEY not in t.open


def test_missing_row_counts_as_not_qualifying():
    t = tracker()
    t.update(0, row())
    t.update(10, row())
    t.update(11, {})
    assert kinds(t.update(21, {})) == ["close"]


def test_peak_is_tracked():
    t = tracker()
    t.update(0, row(3.0, 1.2))
    t.update(10, row(3.0, 1.2))
    t.update(12, row(4.0, 1.6))
    t.update(14, row(2.0, 0.8))
    ep = t.open[KEY]
    assert (ep.peak_percent, ep.peak_erg, ep.profit_percent) == (4.0, 1.6, 2.0)


def test_trade_result_and_close_all():
    t = tracker()
    t.update(0, row())
    t.update(10, row())
    t.note_trade(KEY, "executed +0.42 ERG")
    ev = t.close_all(15, "bot stopped")
    assert kinds(ev) == ["close"] and ev[0].episode.trade == "executed +0.42 ERG"
    assert ev[0].episode.close_reason == "bot stopped" and t.open == {}


def test_flicker_every_poll_does_not_storm():
    t = tracker()
    events = []
    for i in range(60):  # 2 minutes of GO / not GO alternating every 2 s
        events += t.update(i * 2, row(status="GO" if i % 2 == 0 else "no edge"))
    assert events == []


def test_chain_outage_holds_an_open_episode():
    """Review: 'stale' (chain unreadable) neither closes an open episode nor reopens it afterwards."""
    t = tracker()
    t.update(0, row())
    t.update(10, row())
    for now in range(12, 50, 2):
        assert t.update(now, row(status="stale")) == []
    assert t.update(52, row()) == [] and KEY in t.open
