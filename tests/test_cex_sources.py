"""Scanner wiring for the extra CEX sources (issue #40): one fetch for all exchanges, the fee book
used by every CEX figure, the venue table, the cross-exchange spread, one combined Discord watch."""
import asyncio

import pytest

import arbitrage.scanner as scanner_module
import config
from arbitrage.scanner import ArbitrageScanner
from arbitrage.venues import VenueContext, describe_all
from exchanges import cex_public as cp
from exchanges.base import OrderBook, OrderBookLevel
from logging_config import console
from tests.test_scanner_paths import ORACLE_R4, make_prices
from exchanges.sigmausd import BankState


def run(coro):
    return asyncio.run(coro)


def book(bid, ask, qty=10_000):
    return OrderBook("x", "ERG/USDT", [OrderBookLevel(bid, qty)], [OrderBookLevel(ask, qty)])


QUOTES = {
    "Kucoin": cp.CexQuote("Kucoin", book(0.3240, 0.3250)),
    "NonKYC": cp.CexQuote("NonKYC", book(0.3255, 0.3265)),
    "Gate": cp.CexQuote("Gate", book(0.3249, 0.3263)),
    "MEXC": cp.CexQuote("MEXC", None, error="timeout"),
    "SafeTrade": cp.CexQuote("SafeTrade", None, error="disabled: SAFETRADE_ENABLED=false (Cloudflare ...)"),
}


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    s = ArbitrageScanner(db_path=str(tmp_path / "s.db"), cex_watch=True)

    async def quotes(session, names=None):
        return dict(QUOTES)

    async def no_refresh(session, names=None):
        return None

    monkeypatch.setattr(scanner_module, "fetch_all_quotes", quotes)
    monkeypatch.setattr(s.cex_fees, "refresh", no_refresh)
    yield s
    s.tracker.close()


def test_one_fetch_feeds_the_venues_and_the_watch(scanner, monkeypatch):
    async def no_chain():
        return None

    monkeypatch.setattr(scanner, "_read_chain", no_chain)
    scanner._http = object()          # the shared session connect_all opens (the fake fetch ignores it)
    prices = run(scanner.fetch_all_prices())
    assert set(prices["cex"]) == set(QUOTES)
    assert set(prices["cex_watch"]) == {"Kucoin", "NonKYC", "Gate"}          # live books only
    assert prices["cex_watch"]["Gate"].ask == pytest.approx(0.3263)


def test_every_exchange_has_a_venue_row(scanner):
    prices = {"bank": {}, "cex": QUOTES,
              "cex_watch": {n: q for n, q in QUOTES.items() if q.book}}
    ctx = VenueContext(prices=prices, timestamps={}, now=QUOTES["Gate"].timestamp + 3, chain_error=None,
                       pending=frozenset(), read_ms=None, enable_cex=False, enable_use=False, cex_watch=True)
    rows = {r.name: r for r in describe_all(ctx)}
    assert rows["Gate"].state == "watch" and "0.3249" in rows["Gate"].quote
    assert rows["MEXC"].state == "down" and "timeout" in rows["MEXC"].error
    assert rows["SafeTrade"].state == "disabled" and "Cloudflare" in rows["SafeTrade"].quote


def test_cex_paths_use_the_fee_book(tmp_path):
    s = ArbitrageScanner(db_path=str(tmp_path / "f.db"), enable_cex=True)
    try:
        s.cex_fees.set("Kucoin", cp.CexFees(taker=0.001, erg_withdraw=5.0, source="live"))
        state = BankState(bank_erg_nano=3_000_000 * 10**9, sigusd_circ_cents=10_000_000, oracle_r4=ORACLE_R4)
        p = make_prices(state, 0.31)
        p.update({"kucoin_erg_usdt": 0.30, "nonkyc_erg_usdt": 0.31})
        opps = s._find_opportunities(p)
        bank = next(o for o in opps if o.path.startswith("Kucoin<>Bank"))
        assert bank.fees.withdraw_fee_erg == 5.0
    finally:
        s.tracker.close()


def test_watch_gaps_cover_every_exchange_and_use_live_withdrawal_fees(scanner):
    scanner.cex_fees.set("Gate", cp.CexFees(taker=0.002, erg_withdraw=0.5, source="live"))
    prices = {"spectrum_erg_sigusd": 0.31, "bank": {}, "cex": QUOTES,
              "cex_watch": {n: q for n, q in QUOTES.items() if q.book}}
    gaps = {g["exchange"]: g for g in scanner._cex_watch_gaps(prices)}
    assert set(gaps) == {"Kucoin", "NonKYC", "Gate"} and "0.5 ERG withdrawal" in gaps["Gate"]["text"]


def test_spread_line_is_shown(scanner):
    prices = {"spectrum_erg_sigusd": 0.31, "bank": {}, "cex": QUOTES,
              "cex_watch": {n: q for n, q in QUOTES.items() if q.book}}
    with console.capture() as cap:
        scanner._display_cex_watch(prices)
    out = cap.get()
    assert "WATCH spread" in out and "after fees" in out


def test_discord_gets_one_combined_watch_message(scanner, monkeypatch):
    sent = []

    async def notify_watch(exchange, text):
        sent.append((exchange, text))
        return True

    monkeypatch.setattr(scanner.discord, "notify_watch", notify_watch)
    prices = {"spectrum_erg_sigusd": 0.25, "bank": {}, "cex": QUOTES,       # every CEX far above the pool
              "cex_watch": {n: q for n, q in QUOTES.items() if q.book}}
    run(scanner._notify_cex_watch(prices))
    assert len(sent) == 1 and sent[0][0] == "CEX"
    assert all(n in sent[0][1] for n in ("Kucoin", "NonKYC", "Gate")) and "spread" in sent[0][1].lower()


def test_grid_only_prices_sizes_the_wallet_can_fund(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRADE_SIZES", [1, 5, 10, 25, 50, 100])
    monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 100.0)
    s = ArbitrageScanner(db_path=str(tmp_path / "g.db"))
    try:
        assert s._analysis_sizes() == [1, 5, 10, 25, 50, 100]          # wallet unknown: up to the cap
        s._last_wallet = {"erg": 20.71, "sigusd": 0, "use": 0, "ok": True}
        assert s._analysis_sizes() == [1, 5, 10, 19.71]               # funded: wallet minus the 1 ERG reserve
        monkeypatch.setattr(config, "TRADE_SIZES_UNFUNDED", True)
        assert s._analysis_sizes() == [1, 5, 10, 25, 50, 100]
    finally:
        s.tracker.close()
