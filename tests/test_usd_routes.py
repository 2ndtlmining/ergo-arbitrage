"""SigUSD exits in ERG and dollars, and the dashboard's USD view (toggle with u / e)."""
from types import SimpleNamespace

import pytest

from arbitrage.sizing import Market
from arbitrage.usd_routes import EXAMPLE_SIGUSD, SEND_MINER_FEE, sigusd_routes
from exchanges.base import OrderBook, OrderBookLevel
from exchanges.sigmausd import BankState, quote_redeem_sigusd
from exchanges.spectrum import amm_output_raw

# pool 0.3011 SigUSD/ERG (110k ERG), oracle $0.3107/ERG, RR ~318%
BANK = BankState(bank_erg_nano=2_000_000 * 10**9, sigusd_circ_cents=19_500_000, oracle_r4=int(1e9 / 0.3107))
MARKET = Market(pool_erg=110_000 * 10**9, pool_sigusd=int(110_000 * 0.3011 * 100), fee_num=995, bank=BANK)


def book(bid, qty=10_000):
    return OrderBook(exchange="x", pair="ERG/USDT", bids=[OrderBookLevel(bid, qty)], asks=[OrderBookLevel(bid * 1.001, qty)])


def quotes(**bids):
    return {name: SimpleNamespace(book=book(b)) for name, b in bids.items()}


class Fees:
    def get(self, name):
        return SimpleNamespace(taker=0.002)


def test_pool_and_bank_use_the_exact_contract_math():
    r = sigusd_routes(200, MARKET)
    by = {x.name: x for x in r.routes}
    pool = (amm_output_raw(MARKET.pool_sigusd, MARKET.pool_erg, 20_000, 995) - 1_100_000) / 1e9
    bank = (quote_redeem_sigusd(BANK, 20_000) - 1_100_000) / 1e9
    assert by["pool → ERG"].amount == pytest.approx(pool)
    assert by["bank redeem → ERG"].amount == pytest.approx(bank)
    assert r.premium_percent == pytest.approx((pool / bank - 1) * 100)
    assert by["pool → ERG"].usd == pytest.approx(pool * BANK.oracle_usd_per_erg)


def test_exchange_routes_sell_into_the_book_after_the_send_fee_and_taker_fee():
    r = sigusd_routes(200, MARKET, quotes(Gate=0.316, Kucoin=0.314), Fees())
    by = {x.name: x for x in r.routes}
    pool = by["pool → ERG"].amount
    assert by["pool → ERG → Gate"].amount == pytest.approx((pool - SEND_MINER_FEE / 1e9) * 0.316 * 0.998)
    assert [x.name for x in r.routes][:2] == ["pool → ERG → Gate", "pool → ERG → Kucoin"]   # best first
    assert r.best.name == "pool → ERG → Gate" and r.vs_face(r.best) > 0


def test_a_thin_book_is_shown_as_not_possible():
    thin = {"NonKYC": SimpleNamespace(book=book(0.32, qty=5))}
    r = sigusd_routes(200, MARKET, thin, Fees())
    row = next(x for x in r.routes if "NonKYC" in x.name)
    assert row.amount is None and "thin" in row.note and r.routes[-1] is row


def test_an_empty_wallet_quotes_an_example_amount():
    r = sigusd_routes(0, MARKET)
    assert r.example and r.sigusd == EXAMPLE_SIGUSD


def test_summary_line():
    s = sigusd_routes(200, MARKET, quotes(Gate=0.316), Fees()).summary()
    assert s.startswith("best pool → ERG → Gate") and "USDT" in s and "vs $200" in s and "premium" in s


# ---------- dashboard ----------

def test_usd_view_replaces_paths_with_the_routes_panel():
    from rich.console import Console
    from arbitrage.dashboard_view import render
    from tests.test_dashboard_view import full_state
    s = full_state()
    s.usd_routes = sigusd_routes(200, MARKET, quotes(Gate=0.316), Fees())
    s.on_key("u")
    c = Console(record=True, width=120, height=40, color_system=None)
    c.print(render(s, 120, 40))
    out = c.export_text()
    assert "SigUSD routes" in out and "pool → ERG → Gate" in out and "bank redeem → ERG" in out
    assert "Paths" not in out
    s.on_key("e")
    c = Console(record=True, width=120, height=40, color_system=None)
    c.print(render(s, 120, 40))
    assert "Paths" in c.export_text()


def test_keys_toggle_the_view():
    from arbitrage.dashboard_state import DashboardState
    s = DashboardState("notify")
    assert s.view_mode == "erg"
    s.on_key("u")
    assert s.view_mode == "usd"
    s.on_key("x")
    assert s.view_mode == "usd"
    s.on_key("E")
    assert s.view_mode == "erg"


def test_usd_view_without_prices_yet():
    from rich.console import Console
    from arbitrage.dashboard_state import DashboardState
    from arbitrage.dashboard_view import render
    s = DashboardState("notify")
    s.on_key("u")
    c = Console(record=True, width=120, height=40, color_system=None)
    c.print(render(s, 120, 40))
    assert "SigUSD routes" in c.export_text()


def test_digest_has_a_sigusd_line():
    from notifications.digest import Digest
    from notifications.embeds import digest_embed
    d = Digest(hours=6, wallet={"erg": 20.7, "sigusd": 200.0}, sigusd="best pool → ERG → Gate 207.41 USDT",
               trades={"count": 0, "net_erg": 0.0, "failed": 0})
    fields = {f["name"]: f["value"] for f in digest_embed(d)["fields"]}
    assert "Gate" in fields["SigUSD"]


def test_keys_are_not_read_without_a_terminal(monkeypatch):
    import io
    import sys
    from arbitrage import dashboard_keys
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    stop = dashboard_keys.start(lambda k: None, loop=None)
    stop()                                                      # a no-op, and nothing was started


def test_usd_flag_starts_in_the_usd_view(monkeypatch, tmp_path):
    import main
    seen = []

    class Fake:
        def __init__(self, *a, **k):
            from arbitrage.dashboard_state import DashboardState
            self.state = DashboardState("notify")

    async def fake_run(scanner, view, once):
        seen.append(scanner.state.view_mode)

    monkeypatch.setattr(main, "ArbitrageScanner", Fake)
    monkeypatch.setattr(main, "run", fake_run)
    main.main(["--notify", "--usd", "--db", str(tmp_path / "t.db")])
    assert seen == ["usd"]


def test_quote_lists_the_exchange_routes(monkeypatch):
    import asyncio
    from ergo import actions

    async def fake_quotes(session, names=None):
        return quotes(Gate=0.316)

    async def no_refresh(self, session, names=None):
        pass

    monkeypatch.setattr(actions, "fetch_all_quotes", fake_quotes)
    monkeypatch.setattr(actions.FeeBook, "refresh", no_refresh)
    lines = asyncio.run(actions.sigusd_route_lines(200, MARKET))
    text = "\n".join(lines)
    assert "pool → ERG → Gate" in text and "USDT" in text and "vs bank" in text
    assert text.index("pool → ERG → Gate") < text.index("bank redeem → ERG")      # best first
