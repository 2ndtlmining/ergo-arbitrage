"""Public CEX market data and fees (issue #40): parsers against recorded responses, live fee
refresh, failure handling, and the cross-exchange spread with every fee counted."""
import asyncio

import pytest

import config
from exchanges import cex_public as cp
from exchanges.base import OrderBook, OrderBookLevel

# Recorded 2026-10-03 (trimmed)
GATE_BOOK = {"current": 1790989067110, "asks": [["0.3263", "64.08"], ["0.3264", "38.61"], ["0.3283", "1306.06"]],
             "bids": [["0.3249", "37.16"], ["0.3245", "21.32"], ["0.3222", "69.49"]]}
GATE_PAIR = {"id": "ERG_USDT", "base": "ERG", "quote": "USDT", "fee": "0.2", "trade_status": "tradable"}
MEXC_BOOK = {"lastUpdateId": 563191128, "bids": [["0.3243", "8.63"], ["0.3236", "3.80"], ["0.3224", "29.83"]],
             "asks": [["0.3266", "5.46"], ["0.3271", "71.55"], ["0.3272", "502.59"]]}
MEXC_INFO = {"symbols": [{"symbol": "ERGUSDT", "makerCommission": "0", "takerCommission": "0.0008"}]}
NONKYC_BOOK = {"symbol": "ERG/USDT", "bids": [{"price": "0.3258", "quantity": "27.2231"},
                                               {"price": "0.3251", "quantity": "142.3282"}],
               "asks": [{"price": "0.326392", "quantity": "0.15310000"},
                        {"price": "0.327045", "quantity": "0.15290000"}]}
NONKYC_ASSET = {"ticker": "ERG", "withdrawFee": "3.1", "withdrawalActive": True}
KUCOIN_BOOK = {"code": "200000", "data": {"bids": [["0.325", "3.8304"], ["0.3236", "5.7052"]],
                                          "asks": [["0.3262", "10.0"], ["0.3270", "200.0"]]}}
KUCOIN_CURRENCY = {"code": "200000", "data": {"currency": "ERG", "withdrawalMinFee": "2", "isWithdrawEnabled": True}}
SAFETRADE_BOOK = {"asks": [{"price": "0.3300", "remaining_volume": "50.0"}],
                  "bids": [{"price": "0.3200", "remaining_volume": "40.0"}]}
SAFETRADE_CURRENCY = {"id": "erg", "withdraw_fee": "1.0"}
CLOUDFLARE = "<html><head><title>Attention Required! | Cloudflare</title></head></html>"


def run(coro):
    return asyncio.run(coro)


# --- parsers ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,raw,bid,ask", [
    ("Gate", GATE_BOOK, 0.3249, 0.3263),
    ("MEXC", MEXC_BOOK, 0.3243, 0.3266),
    ("NonKYC", NONKYC_BOOK, 0.3258, 0.326392),
    ("Kucoin", KUCOIN_BOOK, 0.325, 0.3262),
    ("SafeTrade", SAFETRADE_BOOK, 0.32, 0.33),
])
def test_order_books_parse_best_bid_and_ask(name, raw, bid, ask):
    book = cp.VENUES[name].parse_book(raw)
    assert (book.best_bid.price, book.best_ask.price) == (pytest.approx(bid), pytest.approx(ask))
    assert all(a.price <= b.price for a, b in zip(book.asks, book.asks[1:]))      # asks ascending
    assert all(a.price >= b.price for a, b in zip(book.bids, book.bids[1:]))      # bids descending


@pytest.mark.parametrize("name,raw,expected", [
    ("Gate", GATE_PAIR, {"taker": 0.002}),
    ("MEXC", MEXC_INFO, {"taker": 0.0008}),
    ("NonKYC", NONKYC_ASSET, {"erg_withdraw": 3.1}),
    ("Kucoin", KUCOIN_CURRENCY, {"erg_withdraw": 2.0}),
    ("SafeTrade", SAFETRADE_CURRENCY, {"erg_withdraw": 1.0}),
])
def test_published_fees_parse(name, raw, expected):
    assert cp.VENUES[name].parse_fees(raw) == pytest.approx(expected)


def test_walking_a_thin_book_prices_the_real_size():
    """NonKYC's top asks hold 0.15 ERG each: 10 ERG cannot be bought at the top price."""
    book = cp.VENUES["NonKYC"].parse_book(NONKYC_BOOK)
    assert book.effective_buy_price(10) is None                     # not enough depth in the sample
    assert book.effective_buy_price(0.3) == pytest.approx((0.1531 * 0.326392 + 0.1469 * 0.327045) / 0.3)


# --- fetching: failures become a visible error, never an exception ------------------------------------

class FakeResp:
    def __init__(self, status, body, text=None):
        self.status, self._body, self._text = status, body, text

    async def json(self, content_type=None):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    async def text(self):
        return self._text if self._text is not None else str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, timeout=None, headers=None):
        for part, resp in self.routes.items():
            if part in url:
                if isinstance(resp, BaseException):
                    raise resp
                return resp
        return FakeResp(404, {"error": "not found"})


def test_a_good_quote():
    q = run(cp.fetch_quote(cp.VENUES["Gate"], FakeSession({"order_book": FakeResp(200, GATE_BOOK)})))
    assert q.error is None and (q.bid, q.ask) == (pytest.approx(0.3249), pytest.approx(0.3263))


def test_cloudflare_is_reported_as_blocked():
    q = run(cp.fetch_quote(cp.VENUES["SafeTrade"], FakeSession({"order-book": FakeResp(403, None, CLOUDFLARE)}),
                           enabled=True))
    assert q.book is None and "Cloudflare" in q.error


def test_network_errors_and_bad_payloads_become_errors():
    for resp in (asyncio.TimeoutError(), FakeResp(200, {"unexpected": True}), FakeResp(500, None, "oops")):
        q = run(cp.fetch_quote(cp.VENUES["MEXC"], FakeSession({"depth": resp})))
        assert q.book is None and q.error


def test_disabled_venue_is_not_fetched():
    q = run(cp.fetch_quote(cp.VENUES["SafeTrade"], FakeSession({}), enabled=False))
    assert q.book is None and "disabled" in q.error


# --- fee book ----------------------------------------------------------------------------------------

def test_fee_book_starts_from_configured_defaults():
    book = cp.FeeBook()
    k = book.get("Kucoin")
    assert (k.taker, k.erg_withdraw, k.source) == (config.KUCOIN_TRADING_FEE, config.KUCOIN_ERG_WITHDRAW_FEE, "default")


def test_fee_book_refresh_takes_published_fees_and_keeps_defaults_on_failure():
    book = cp.FeeBook()
    session = FakeSession({"currencies/ERG": FakeResp(200, KUCOIN_CURRENCY),
                           "currency_pairs/ERG_USDT": FakeResp(200, GATE_PAIR),
                           "exchangeInfo": asyncio.TimeoutError()})
    run(book.refresh(session, names=("Kucoin", "Gate", "MEXC")))
    assert (book.get("Kucoin").erg_withdraw, book.get("Kucoin").source) == (2.0, "live")
    assert (book.get("Gate").taker, book.get("Gate").source) == (0.002, "live")
    assert book.get("MEXC").source == "default" and book.get("MEXC").taker == config.MEXC_TRADING_FEE


def test_current_defaults_match_what_the_exchanges_publish():
    """Kucoin's ERG withdrawal fee rose to 2 ERG; NonKYC's is 3.1 (both published 2026-10-03)."""
    assert config.KUCOIN_ERG_WITHDRAW_FEE == 2.0 and config.NONKYC_ERG_WITHDRAW_FEE == 3.1


# --- cross-exchange spread -------------------------------------------------------------------------------

def book(bid, ask, qty=10_000):
    return OrderBook("x", "ERG/USDT", [OrderBookLevel(bid, qty)], [OrderBookLevel(ask, qty)])


def quotes(**books):
    return {name: cp.CexQuote(name, b) for name, b in books.items()}


def fees(**kw):
    fb = cp.FeeBook()
    for name, (taker, wd) in kw.items():
        fb.set(name, cp.CexFees(taker=taker, erg_withdraw=wd, source="test"))
    return fb


def test_spread_counts_every_fee():
    # buy on A at 0.30, sell on B at 0.33 (+10% gross); taker 0.1% each side, 2 ERG withdrawal, 1 USDT back
    s = cp.best_spread(quotes(A=book(0.29, 0.30), B=book(0.33, 0.34)), fees(A=(0.001, 2.0), B=(0.001, 0.5)),
                       size_erg=100, usdt_transfer_fee=1.0)
    erg = 100 * (1 - 0.001) - 2.0                                    # bought, then withdrawn to B
    usdt_out = erg * 0.33 * (1 - 0.001) - 1.0                        # sold on B, USDT moved back to A
    assert (s.buy, s.sell) == ("A", "B")
    assert s.gross_percent == pytest.approx(10.0)
    assert s.net_usdt == pytest.approx(usdt_out - 100 * 0.30)
    assert s.net_percent == pytest.approx((usdt_out - 30.0) / 30.0 * 100)


def test_a_gap_smaller_than_the_withdrawal_fee_is_never_profitable():
    """Issue #40: +0.74% gross between two exchanges, but a 3.1 ERG withdrawal at 20 ERG is ~15%."""
    s = cp.best_spread(quotes(NonKYC=book(0.3261, 0.3290), MEXC=book(0.3200, 0.3237)),
                       fees(NonKYC=(0.002, 3.1), MEXC=(0.0008, 0.1)), size_erg=20, usdt_transfer_fee=1.0)
    assert s.gross_percent > 0.5 and s.net_percent < 0 and not s.profitable


def test_unknown_withdrawal_fee_means_no_net_figure():
    s = cp.best_spread(quotes(A=book(0.29, 0.30), B=book(0.33, 0.34)), fees(A=(0.001, None), B=(0.001, 0.5)),
                       size_erg=100, usdt_transfer_fee=1.0)
    assert s is None or (s.buy, s.sell) != ("A", "B")


def test_no_spread_without_two_live_books():
    assert cp.best_spread(quotes(A=book(0.29, 0.30)), fees(A=(0.001, 1.0)), size_erg=10,
                          usdt_transfer_fee=1.0) is None
