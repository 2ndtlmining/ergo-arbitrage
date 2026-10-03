"""Dashboard additions: health strip, Exchanges panel, History panel, mint gate & RR trend."""
import time
from types import SimpleNamespace


from arbitrage.dashboard_state import DashboardState, ExchangeRow, VenueStatus
from arbitrage.dashboard_view import (exchanges_panel, health_strip, history_panel, prices_panel, render,
                                      rr_trend, venues_panel)
from tests.test_dashboard_view import full_state, text
from tracker.profit_tracker import ProfitTracker


def exchanges_state():
    s = full_state()
    s.exchanges = [
        ExchangeRow("Kucoin", "live", bid=0.3238, ask=0.3239, vs_oracle_percent=-0.12,
                    fees="0.10% · 2 ERG*", latency_ms=145),
        ExchangeRow("MEXC", "down", error="timeout", latency_ms=8000),
        ExchangeRow("SafeTrade", "disabled", error="SAFETRADE_ENABLED=false"),
    ]
    s.spread_text = "buy on MEXC (ask $0.3269), sell on Kucoin (bid $0.3227) at 20 ERG: -1.27% gross, -16.2% after fees"
    return s


# --- health strip -----------------------------------------------------------------------------------

def test_health_strip_lists_every_source_time():
    s = exchanges_state()
    s.read_ms = 3.0
    s.health = {"full_scan_ms": 15.0, "discord_queue": 0, "data_age_s": 2.0,
                "cex_ms": {"Kucoin": 145.0, "NonKYC": 333.0}}
    out = text(health_strip(s))
    for needle in ("chain 3 ms", "Kucoin 145", "NonKYC 333", "full scan 15 ms", "Discord queue 0", "data 2s"):
        assert needle in out, needle


def test_health_strip_flags_slow_and_backed_up_sources():
    s = DashboardState("notify")
    s.health = {"full_scan_ms": 15.0, "discord_queue": 60, "data_age_s": 90.0, "cex_ms": {"Gate": 4000.0}}
    strip = health_strip(s)
    styles = {span.style for span in strip.spans}
    assert "red" in styles                                   # queue backed up, data stale, Gate slow


# --- exchanges and venues -----------------------------------------------------------------------------

def test_exchanges_panel_shows_books_fees_and_the_spread():
    out = text(exchanges_panel(exchanges_state()), width=170)
    for needle in ("Kucoin", "0.3238", "0.3239", "-0.12%", "0.10% · 2 ERG*", "published by the exchange", "145",
                   "MEXC", "timeout", "SafeTrade", "disabled", "best spread", "after fees"):
        assert needle in out, needle


def test_venues_panel_keeps_only_on_chain_venues_when_exchanges_have_their_own_panel():
    s = exchanges_state()
    s.update_venues([VenueStatus("ErgoDEX pool", "on-chain", "live", "0.3051"),
                     VenueStatus("Kucoin", "CEX", "watch", "$0.3238 / $0.3239")])
    out = text(venues_panel(s))
    assert "ErgoDEX pool" in out and "Kucoin" not in out


# --- history ------------------------------------------------------------------------------------------

def test_history_panel_lists_episodes_and_trades():
    s = full_state()
    s.recent_episodes = [{"path": "pool→redeem", "opened_at": "2026-10-03T08:00:00",
                          "closed_at": "2026-10-03T08:06:40", "peak_profit_percent": 3.5}]
    s.recent_trades = [{"started_at": "2026-10-03T08:01:00", "status": "completed", "input_erg": 10.0,
                        "actual_profit_erg": 0.42, "notes": "redeem; expected"}]
    out = text(history_panel(s))
    for needle in ("pool→redeem", "08:00", "6m 40s", "+3.50%", "completed", "+0.4200 ERG", "redeem"):
        assert needle in out, needle


def test_history_panel_when_nothing_happened_yet():
    assert "no episodes or trades yet" in text(history_panel(DashboardState()))


def test_tracker_returns_the_most_recent_rows_first(tmp_path):
    t = ProfitTracker(str(tmp_path / "h.db"))
    try:
        for peak in (1.0, 2.0, 3.0):
            ep = SimpleNamespace(label=f"p{peak}", peak_erg=peak, peak_percent=peak, peak_size_erg=10.0,
                                 profit_percent=peak, trade=None)
            t.close_chain_episode(t.open_chain_episode(ep), ep)
        t.start_trade(None, 10.0, 10.4, 0.4)
        assert [r["path"] for r in t.recent_chain_episodes(2)] == ["p3.0", "p2.0"]
        assert len(t.recent_trades(5)) == 1
    finally:
        t.close()


# --- mint gate & RR trend --------------------------------------------------------------------------------

def test_rr_trend_downsamples_six_hours_into_a_short_sparkline():
    now = time.time()
    samples = [(now - 6 * 3600 + i * 15, 322 + i * 8 / 1440) for i in range(1440)]   # 322% -> 330%
    trend = rr_trend(samples, now)
    assert trend.startswith("▁") and trend.rstrip().endswith("(6h, 322→330%)")
    assert len(trend.split(" ")[0]) <= 24


def test_rr_trend_needs_a_few_samples():
    assert rr_trend([], time.time()) == ""


def test_bank_line_shows_the_trend_and_the_forecast():
    s = full_state()
    now = time.time()
    s.rr_history.extend((now - 600 + i * 60, 329 + i * 0.1) for i in range(10))
    s.mint_text = "✗ needs ERG $0.393 (+21.7%)"
    out = text(prices_panel(s), width=140)
    assert "RR 330%" in out and "needs ERG $0.393" in out and "(6h," in out


# --- layout and json ---------------------------------------------------------------------------------------

def test_the_whole_screen_renders_with_every_panel():
    s = exchanges_state()
    s.health = {"full_scan_ms": 15.0, "discord_queue": 0, "data_age_s": 2.0, "cex_ms": {"Kucoin": 145.0}}
    out = text(render(s), width=170, height=60)
    for title in ("Prices", "Live", "Paths", "Events", "History", "Venues", "Exchanges"):
        assert title in out, title
    assert "full scan 15 ms" in out


def test_json_carries_the_new_fields():
    s = exchanges_state()
    s.health = {"full_scan_ms": 15.0}
    s.recent_trades = [{"status": "completed"}]
    j = s.to_json()
    assert j["exchanges"][0]["name"] == "Kucoin" and j["spread"] and j["health"]["full_scan_ms"] == 15.0
    assert j["history"]["trades"] == [{"status": "completed"}] and "mint" in j["prices"]
