"""The scanner fills the new dashboard fields: exchanges, spread, health, history, RR trend."""
import asyncio

import pytest

import arbitrage.scanner as scanner_module
import config
from arbitrage.scanner import ArbitrageScanner
from exchanges import cex_public as cp
from tests.test_cex_sources import QUOTES
from tests.test_chain_scanner import HEALTHY, WALLET, Reader, snap


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_ENABLED", True)
    s = ArbitrageScanner(mode="notify", db_path=str(tmp_path / "w.db"), cex_watch=True, view="dashboard")
    s._http = object()

    async def quotes(session, names=None):
        return dict(QUOTES)

    async def no_refresh(session, names=None):
        return None

    async def healthy():
        return dict(HEALTHY)

    async def wallet():
        return dict(WALLET)

    async def quiet(*a, **k):
        return True

    monkeypatch.setattr(scanner_module, "fetch_all_quotes", quotes)
    monkeypatch.setattr(s.cex_fees, "refresh", no_refresh)
    monkeypatch.setattr(s.ergo_node, "get_health", healthy)
    monkeypatch.setattr(s, "_fetch_wallet_balances", wallet)
    monkeypatch.setattr(s.discord, "_send", quiet)
    monkeypatch.setattr(s.discord, "post", lambda *a, **k: None)
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    yield s
    s.tracker.close()


def test_a_full_scan_fills_exchanges_spread_and_health(scanner):
    run(scanner.poll_once(0))
    s = scanner.state
    rows = {e.name: e for e in s.exchanges}
    assert rows["Gate"].state == "live" and rows["Gate"].bid == pytest.approx(0.3249)
    assert rows["Gate"].vs_oracle_percent is not None and "ERG" in rows["Gate"].fees
    assert rows["MEXC"].state == "down" and rows["MEXC"].error == "timeout"
    assert rows["SafeTrade"].state == "disabled"
    assert s.spread_text and "after fees" in s.spread_text
    assert s.health["full_scan_ms"] >= 0 and isinstance(s.health["discord_queue"], int)
    assert set(s.health["cex_ms"]) <= set(QUOTES)


def test_a_full_scan_records_the_reserve_ratio_and_loads_history(scanner):
    from types import SimpleNamespace
    ep = SimpleNamespace(label="pool→redeem", peak_erg=1.0, peak_percent=3.0, peak_size_erg=10.0,
                         profit_percent=2.0, trade=None)
    scanner.tracker.close_chain_episode(scanner.tracker.open_chain_episode(ep), ep)
    run(scanner.poll_once(0))
    s = scanner.state
    assert len(s.rr_history) == 1 and s.rr_history[0][1] == pytest.approx(322.58, abs=0.01)
    assert [e["path"] for e in s.recent_episodes] == ["pool→redeem"]


def test_fast_ticks_do_not_add_reserve_ratio_samples(scanner):
    run(scanner.poll_once(0))
    run(scanner.poll_once(2))
    assert len(scanner.state.rr_history) == 1
