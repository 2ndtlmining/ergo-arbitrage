"""Dashboard renderer: pure function of DashboardState (spec: dashboard)."""
from rich.console import Console

from arbitrage.dashboard_state import DashboardState, VenueStatus
from arbitrage.dashboard_view import render, render_safe, sparkline
from tests.test_dashboard_state import MINT, POOL, blocked, go


def text(renderable, width=140, height=50):
    c = Console(record=True, width=width, height=height, color_system=None)
    c.print(renderable)
    return c.export_text()


def full_state():
    s = DashboardState("live")
    s.height, s.scan_count, s.read_ms, s.node_ok, s.wallet_ok, s.next_full_scan_in = 1885700, 4, 6.0, True, True, 9
    s.prices = {"spectrum_erg_sigusd": 0.3127,
                "bank": {"oracle_erg_usd": 0.3242, "reserve_ratio": 330.1, "can_mint_sigusd": False}}
    s.update_paths({POOL: go(3.36), MINT: blocked()}, {POOL: 1}, None,
                   {POOL: ["Swap 40 ERG -> SigUSD on the pool", "Redeem at the bank"]})
    s.update_venues([VenueStatus("ErgoDEX pool", "on-chain", "live", "0.3127 SigUSD/ERG", 1.0, 6.0),
                     VenueStatus("Kucoin", "CEX", "disabled", "ENABLE_CEX=false")])
    s.set_live("blocked", ["profitable 1/2 polls in a row"])
    s.wallet = {"erg": 20.7119, "sigusd": 0.0, "value_erg": 20.7119, "value_usd": 6.72}
    s.add_event("info", "CHAIN pool, bank changed")
    return s


def test_every_panel_and_key_value_is_shown():
    out = text(render(full_state()))
    for needle in ("LIVE", "h1885700", "Prices", "0.3127", "RR 330%", "Live", "blocked", "profitable 1/2 polls",
                   "Wallet", "20.7119 ERG", "Paths", "pool→redeem", "GO", "+3.36%", "1/2", "mint→pool sell",
                   "BLOCKED", "Events", "CHAIN pool, bank changed", "Venues", "ErgoDEX pool", "ENABLE_CEX=false"):
        assert needle in out, needle


def test_steps_of_the_go_path_are_listed():
    out = text(render(full_state()))
    assert "Swap 40 ERG -> SigUSD on the pool" in out and "Redeem at the bank" in out


def test_empty_state_before_the_first_scan():
    out = text(render(DashboardState("monitor")))
    assert "waiting for the first scan" in out


def test_small_terminal_does_not_raise():
    text(render(full_state()), width=80, height=24)


def test_render_error_lands_in_events_and_does_not_raise():
    s = full_state()
    s.prices = None  # a bug somewhere: prices_panel calls .get on it
    out = text(render_safe(s))
    assert "dashboard render error" in out
    assert any("dashboard render error" in t for _, _, t in s.events)


def test_sparkline():
    assert sparkline([]) == ""
    assert sparkline([1.0, 1.0, 1.0]) == "▁▁▁"
    assert sparkline([0.0, 5.0, 10.0]) == "▁▄█"
