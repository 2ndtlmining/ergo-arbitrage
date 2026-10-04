"""#8: the CEX books are fetched in the background so the 2 s chain poll never waits on an exchange;
per-source timings; a race-free rate limiter."""
import asyncio
import time

import pytest

import arbitrage.scanner as scanner_module
from arbitrage.scanner import ArbitrageScanner
from arbitrage.venues import VenueContext, describe_all
from exchanges import cex_public as cp
from tests.test_cex_public import FakeResp, FakeSession, GATE_BOOK
from tests.test_cex_sources import QUOTES
from tests.test_chain_scanner import HEALTHY, Reader, snap


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    s = ArbitrageScanner(db_path=str(tmp_path / "f.db"), cex_watch=True, view="dashboard")
    s._http = object()

    async def no_refresh(session, names=None):
        return None

    async def healthy():
        return dict(HEALTHY)

    monkeypatch.setattr(s.cex_fees, "refresh", no_refresh)
    monkeypatch.setattr(s.ergo_node, "get_health", healthy)
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    yield s
    s.tracker.close()


def test_a_full_scan_tick_does_not_wait_for_a_slow_exchange(scanner, monkeypatch):
    async def slow(session, names=None):
        await asyncio.sleep(5)
        return dict(QUOTES)

    monkeypatch.setattr(scanner_module, "fetch_all_quotes", slow)

    async def go():
        scanner._cex_quotes, scanner._cex_ready = dict(QUOTES), asyncio.Event()
        scanner._cex_ready.set()                         # a snapshot from the previous round exists
        scanner._start_cex_feed()                        # the next round hangs for 5 s
        t = time.perf_counter()
        await asyncio.wait_for(scanner.poll_once(0), 1.0)   # full scan due at t=0
        elapsed = time.perf_counter() - t
        await scanner._stop_cex_feed()
        return elapsed

    assert run(go()) < 1.0
    assert "Gate" in scanner.state.prices.get("cex", {}) or scanner._last_prices.get("cex")


def test_the_first_scan_waits_briefly_for_the_first_snapshot(scanner, monkeypatch):
    async def quick(session, names=None):
        await asyncio.sleep(0.05)
        return dict(QUOTES)

    monkeypatch.setattr(scanner_module, "fetch_all_quotes", quick)

    async def go():
        scanner._start_cex_feed()
        prices = await scanner.fetch_all_prices()
        await scanner._stop_cex_feed()
        return prices

    assert set(run(go())["cex"]) == set(QUOTES)


def test_old_quotes_are_shown_as_stale(scanner):
    old = cp.CexQuote("Gate", QUOTES["Gate"].book, timestamp=time.time() - 120)
    scanner._cex_quotes = {"Gate": old}
    snapshot = scanner._cex_snapshot()
    assert snapshot["Gate"].book is None and snapshot["Gate"].error.startswith("stale")


def test_each_quote_records_its_response_time():
    q = run(cp.fetch_quote(cp.VENUES["Gate"], FakeSession({"order_book": FakeResp(200, GATE_BOOK)})))
    assert q.latency_ms is not None and q.latency_ms >= 0


def test_the_venue_row_shows_the_response_time():
    q = cp.CexQuote("Gate", QUOTES["Gate"].book, latency_ms=123.0)
    prices = {"bank": {}, "cex": {"Gate": q}, "cex_watch": {"Gate": q}}
    ctx = VenueContext(prices=prices, timestamps={}, now=q.timestamp, chain_error=None, pending=frozenset(),
                       read_ms=None, enable_cex=False, enable_use=False, cex_watch=True)
    (gate,) = [r for r in describe_all(ctx) if r.name == "Gate"]
    assert gate.latency_ms == 123.0
