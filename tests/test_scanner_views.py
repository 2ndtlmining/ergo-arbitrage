"""Scanner views: plain prints as before, dashboard/json print nothing but fill the state (spec: dashboard)."""
import asyncio
import json

import pytest

import arbitrage.scanner as scanner_module
import config
from arbitrage.scanner import ArbitrageScanner
from tests.test_chain_scanner import HEALTHY, KEY, WALLET, Reader, snap


def view_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 1000.0)
    monkeypatch.setattr(config, "LIVE_STOP_FILE", str(tmp_path / "STOP"))
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    made = []

    def factory(view, mode="monitor"):
        s = ArbitrageScanner(mode=mode, db_path=str(tmp_path / f"{view}.db"), view=view)

        async def healthy():
            return dict(HEALTHY)

        async def wallet():
            return dict(WALLET)

        monkeypatch.setattr(s.ergo_node, "get_health", healthy)
        monkeypatch.setattr(s, "_fetch_wallet_balances", wallet)
        made.append(s)
        return s

    yield factory
    for s in made:
        s.tracker.close()


def test_plain_view_prints_the_scan(make, capsys):
    from logging_config import console
    s = make("plain")
    with console.capture() as cap:
        asyncio.run(s.poll_once(0))
    assert "Current Prices" in cap.get()


@pytest.mark.parametrize("view", ["dashboard", "json"])
def test_other_views_print_nothing_while_scanning(make, view):
    from logging_config import console
    s = make(view)
    with console.capture() as cap:
        asyncio.run(s.poll_once(0))
        asyncio.run(s.poll_once(2))
    assert cap.get() == ""


def test_state_is_filled_after_a_full_scan_and_a_fast_poll(make):
    s = make("dashboard", mode="live")
    asyncio.run(s.poll_once(0))
    st = s.state
    assert st.height == 1_885_700 and st.scan_count == 1 and st.node_ok is True
    assert st.paths[KEY].status == "GO" and st.paths[KEY].steps
    assert {v.name for v in st.venues} >= {"ErgoDEX pool", "SigmaUSD bank", "Oracle"}
    assert st.live in ("blocked", "armed") and st.wallet["erg"] == WALLET["erg"]
    assert any(t.startswith("opportunity opened") for _, _, t in st.events)
    asyncio.run(s.poll_once(2))
    assert len(st.paths[KEY].history) == 2 and st.next_full_scan_in == pytest.approx(13, abs=0.5)


def test_monitor_mode_live_panel_is_off(make):
    s = make("dashboard", mode="monitor")
    asyncio.run(s.poll_once(0))
    assert s.state.live == "off"


def test_chain_outage_shows_stale_paths_and_down_venues(make, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), RuntimeError("node down")))
    s = make("dashboard", mode="live")
    asyncio.run(s.poll_once(0))
    asyncio.run(s.poll_once(2))
    assert s.state.paths[KEY].status == "stale" and s.state.chain_error == "node down"
    assert {v.name: v.state for v in s.state.venues}["ErgoDEX pool"] == "down"
    assert s.state.live == "blocked" and any("chain state unavailable" in d for d in s.state.live_detail)


def test_json_view_writes_one_clean_line_per_full_scan(make, capsys):
    s = make("json")
    for t in (0, 2, 4, 15):
        asyncio.run(s.poll_once(t))
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 2
    assert all(json.loads(l)["paths"]["pool→redeem"]["status"] == "GO" for l in lines)


def test_trade_progress_goes_to_events(make, monkeypatch):
    from ergo.arb_runner import ArbResult
    s = make("dashboard", mode="live")

    async def fake_run(ns, path, erg_in, *, log, **kw):
        log("  Leg 1 submitted: https://explorer/tx/t1")
        return ArbResult("executed", erg_in=5 * 10**9, sigusd_cents=100, profit_nanoerg=10**8,
                         profit_percent=2.0, tx1="t1", tx2="t2", path=path)

    monkeypatch.setattr(scanner_module, "run_arb", fake_run)
    asyncio.run(s.poll_once(0))
    asyncio.run(s.poll_once(2))
    kinds = [(lvl, t) for _, lvl, t in s.state.events]
    assert ("trade", "Leg 1 submitted: https://explorer/tx/t1") in kinds
    assert any(lvl == "good" and "executed" in t for lvl, t in kinds)


def test_once_stops_after_the_first_full_scan(make, monkeypatch):
    s = make("json")
    calls = []
    real = s.poll_once

    async def counting(now):
        calls.append(now)
        await real(now)

    async def no_connect():
        return None

    monkeypatch.setattr(s, "poll_once", counting)
    monkeypatch.setattr(s, "connect_all", no_connect)
    monkeypatch.setattr(s, "disconnect_all", no_connect)
    asyncio.run(s.run(once=True))
    assert len(calls) == 1
