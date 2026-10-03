"""DashboardState: what every view shows, plus the events it generates (spec: dashboard)."""
import json

from arbitrage.dashboard_state import MAX_EVENTS, MAX_VENUES, DashboardState, VenueStatus, path_status
from arbitrage.sizing import SizeChoice

POOL = "Spectrum buy->Bank redeem"
MINT = "Bank mint->Spectrum sell"


def go(pct=3.0):
    return SizeChoice("redeem", size_nanoerg=40 * 10**9, sigusd_cents=1400, profit_erg=1.2, profit_percent=pct,
                      cost_erg=40.0, max_profit_size_erg=55.0, max_profit_erg=1.5, break_even_erg=0.1, cap_erg=100)


def no_edge():
    return SizeChoice("redeem", max_profit_size_erg=1.0, max_profit_erg=-0.06, cap_erg=100,
                      reason="no profitable size up to 100.00 ERG")


def blocked():
    return SizeChoice("mint", cap_erg=100, reason="bank mint blocked (RR 330%, must stay >= 400% after minting)")


def texts(state):
    return [t for _, _, t in state.events]


def test_path_status():
    assert path_status(go(), None)[0] == "GO"
    assert path_status(no_edge(), None)[0] == "no edge"
    assert path_status(blocked(), None)[0] == "BLOCKED"
    assert path_status(go(), "node down") == ("stale", "chain state unavailable (node down)")
    assert path_status(None, None)[0] == "no data"


def test_opportunity_opened_and_closed_events():
    s = DashboardState("live")
    s.update_paths({POOL: no_edge(), MINT: blocked()}, {}, None, {})
    assert not any("opportunity" in t for t in texts(s))
    s.update_paths({POOL: go(), MINT: blocked()}, {POOL: 1}, None, {POOL: ["step 1", "step 2"]})
    assert any(t.startswith("opportunity opened: pool→redeem") for t in texts(s))
    assert s.paths[POOL].status == "GO" and s.paths[POOL].streak == 1 and s.paths[POOL].steps == ["step 1", "step 2"]
    s.update_paths({POOL: no_edge(), MINT: blocked()}, {}, None, {})
    assert any(t.startswith("opportunity closed: pool→redeem") for t in texts(s))


def test_history_is_capped_and_skips_stale_polls():
    s = DashboardState()
    for _ in range(40):
        s.update_paths({POOL: go(2.0)}, {}, None, {})
    assert len(s.paths[POOL].history) == 30
    s.update_paths({POOL: go(2.0)}, {}, "node down", {})
    assert len(s.paths[POOL].history) == 30 and s.paths[POOL].status == "stale"


def test_venue_down_recovered_and_oracle_pending_events():
    s = DashboardState()
    live = VenueStatus("ErgoDEX pool", "on-chain", "live")
    s.update_venues([live, VenueStatus("Oracle", "on-chain", "live")])
    s.update_venues([VenueStatus("ErgoDEX pool", "on-chain", "down", error="timeout"),
                     VenueStatus("Oracle", "on-chain", "pending")])
    s.update_venues([live, VenueStatus("Oracle", "on-chain", "live")])
    t = texts(s)
    assert "ErgoDEX pool down: timeout" in t and "ErgoDEX pool recovered" in t
    assert "Oracle: update pending" in t and "Oracle: update confirmed" in t


def test_pool_pending_is_not_an_event():
    s = DashboardState()
    s.update_venues([VenueStatus("ErgoDEX pool", "on-chain", "live")])
    s.update_venues([VenueStatus("ErgoDEX pool", "on-chain", "pending")])
    assert texts(s) == []


def test_at_most_max_venues():
    s = DashboardState()
    s.update_venues([VenueStatus(f"v{i}", "CEX", "live") for i in range(MAX_VENUES + 2)])
    assert len(s.venues) == MAX_VENUES


def test_live_category_change_is_an_event_detail_change_is_not():
    s = DashboardState("live")
    s.set_live("blocked", ["cooldown 120s"])
    s.set_live("blocked", ["cooldown 118s"])
    s.set_live("armed", [])
    assert texts(s) == ["live: armed"]


def test_events_are_capped():
    s = DashboardState()
    for i in range(MAX_EVENTS + 50):
        s.add_event("info", f"e{i}")
    assert len(s.events) == MAX_EVENTS and texts(s)[-1] == f"e{MAX_EVENTS + 49}"


def test_to_json_is_serialisable_and_complete():
    s = DashboardState("live")
    s.height, s.scan_count = 1885700, 3
    s.update_paths({POOL: go(), MINT: blocked()}, {POOL: 2}, None, {})
    s.update_venues([VenueStatus("ErgoDEX pool", "on-chain", "live", quote="0.3127 SigUSD/ERG")])
    s.set_live("armed", [])
    s.wallet = {"erg": 20.7, "sigusd": 0.0}
    d = json.loads(json.dumps(s.to_json()))
    assert d["height"] == 1885700 and d["live"]["state"] == "armed"
    assert d["paths"]["pool→redeem"] == {"status": "GO", "size_erg": 40.0, "profit_erg": 1.2, "profit_percent": 3.0,
                                         "break_even_erg": 0.1, "streak": 2, "reason": ""}
    assert d["paths"]["mint→pool sell"]["status"] == "BLOCKED" and d["paths"]["mint→pool sell"]["size_erg"] is None
    assert d["venues"][0]["quote"] == "0.3127 SigUSD/ERG" and d["wallet"]["erg"] == 20.7
