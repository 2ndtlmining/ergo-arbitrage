"""Public (no API key) CEX market data and fees for the watch-only exchanges (issue #40).

One `Venue` per exchange: where its ERG/USDT order book and its published fees live, and how to
parse them. Nothing here signs or trades. Every fetch failure becomes a visible error on the
quote (shown as "down" on the dashboard) instead of an exception.

SafeTrade (safe.trade) is off by default: from many hosts Cloudflare answers every API path with an
"Attention Required" captcha page, which is reported as "blocked by Cloudflare". Set
SAFETRADE_ENABLED=true to try it from your own connection. Its response format follows the
Peatio/OpenDAX public API and has not been verified against a live answer.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import aiohttp

import config
from exchanges.base import OrderBook, OrderBookLevel

logger = logging.getLogger("ergo_arb.cex_public")

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=8)
HEADERS = {"User-Agent": "Mozilla/5.0 (ergo-arbitrage watch-only)", "Accept": "application/json"}
BOOK_DEPTH = 20


def _levels(rows, price_key=0, qty_key=1) -> list[OrderBookLevel]:
    return [OrderBookLevel(price=float(r[price_key]), quantity=float(r[qty_key])) for r in rows]


def _book(name: str, bids: list[OrderBookLevel], asks: list[OrderBookLevel]) -> OrderBook:
    if not bids or not asks:
        raise ValueError("empty order book")
    return OrderBook(exchange=name, pair="ERG/USDT", bids=sorted(bids, key=lambda lv: lv.price, reverse=True),
                     asks=sorted(asks, key=lambda lv: lv.price))


@dataclass(frozen=True)
class Venue:
    name: str
    book_url: str
    parse_book: Callable[[dict], OrderBook]
    fee_url: Optional[str]
    parse_fees: Callable[[dict], dict]
    taker_setting: str                  # config attribute holding the default taker fee (fraction)
    withdraw_setting: str               # config attribute holding the default ERG withdrawal fee (ERG)
    enabled_setting: Optional[str] = None   # config flag; None = always on (watch-only)
    disabled_reason: str = ""

    def enabled(self) -> bool:
        return True if self.enabled_setting is None else bool(getattr(config, self.enabled_setting, False))


def _kucoin_book(d):
    data = d["data"]
    return _book("Kucoin", _levels(data["bids"]), _levels(data["asks"]))


def _nonkyc_book(d):
    return _book("NonKYC", _levels(d["bids"], "price", "quantity"), _levels(d["asks"], "price", "quantity"))


def _gate_book(d):
    return _book("Gate", _levels(d["bids"]), _levels(d["asks"]))


def _mexc_book(d):
    return _book("MEXC", _levels(d["bids"]), _levels(d["asks"]))


def _safetrade_book(d):
    return _book("SafeTrade", _levels(d["bids"], "price", "remaining_volume"),
                 _levels(d["asks"], "price", "remaining_volume"))


def _kucoin_fees(d):
    return {"erg_withdraw": float(d["data"]["withdrawalMinFee"])}


def _nonkyc_fees(d):
    return {"erg_withdraw": float(d["withdrawFee"])}


def _gate_fees(d):
    return {"taker": float(d["fee"]) / 100}          # published in percent


def _mexc_fees(d):
    (sym,) = [s for s in d["symbols"] if s["symbol"] == "ERGUSDT"]
    return {"taker": float(sym["takerCommission"])}


def _safetrade_fees(d):
    return {"erg_withdraw": float(d["withdraw_fee"])}


VENUES: dict[str, Venue] = {v.name: v for v in (
    Venue("Kucoin", f"https://api.kucoin.com/api/v1/market/orderbook/level2_{BOOK_DEPTH}?symbol=ERG-USDT",
          _kucoin_book, "https://api.kucoin.com/api/v1/currencies/ERG", _kucoin_fees,
          "KUCOIN_TRADING_FEE", "KUCOIN_ERG_WITHDRAW_FEE"),
    Venue("NonKYC", f"https://api.nonkyc.io/api/v2/market/orderbook?symbol=ERG_USDT&limit={BOOK_DEPTH}",
          _nonkyc_book, "https://api.nonkyc.io/api/v2/asset/getbyticker/ERG", _nonkyc_fees,
          "NONKYC_TRADING_FEE", "NONKYC_ERG_WITHDRAW_FEE"),
    Venue("Gate", f"https://api.gateio.ws/api/v4/spot/order_book?currency_pair=ERG_USDT&limit={BOOK_DEPTH}",
          _gate_book, "https://api.gateio.ws/api/v4/spot/currency_pairs/ERG_USDT", _gate_fees,
          "GATE_TRADING_FEE", "GATE_ERG_WITHDRAW_FEE"),
    Venue("MEXC", f"https://api.mexc.com/api/v3/depth?symbol=ERGUSDT&limit={BOOK_DEPTH}",
          _mexc_book, "https://api.mexc.com/api/v3/exchangeInfo?symbol=ERGUSDT", _mexc_fees,
          "MEXC_TRADING_FEE", "MEXC_ERG_WITHDRAW_FEE"),
    Venue("SafeTrade", "https://safe.trade/api/v2/peatio/public/markets/ergusdt/order-book"
                       f"?asks_limit={BOOK_DEPTH}&bids_limit={BOOK_DEPTH}",
          _safetrade_book, "https://safe.trade/api/v2/peatio/public/currencies/erg", _safetrade_fees,
          "SAFETRADE_TRADING_FEE", "SAFETRADE_ERG_WITHDRAW_FEE", enabled_setting="SAFETRADE_ENABLED",
          disabled_reason="SAFETRADE_ENABLED=false (Cloudflare often blocks scripted clients)"),
)}


@dataclass
class CexQuote:
    name: str
    book: Optional[OrderBook]
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    latency_ms: Optional[float] = None    # how long the exchange took to answer

    @property
    def bid(self) -> Optional[float]:
        return self.book.best_bid.price if self.book and self.book.best_bid else None

    @property
    def ask(self) -> Optional[float]:
        return self.book.best_ask.price if self.book and self.book.best_ask else None


async def _get_json(session, url: str):
    """(json, None) or (None, error text). Never raises."""
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS) as r:
            if r.status != 200:
                text = await r.text()
                if "Cloudflare" in text or "Attention Required" in text:
                    return None, f"blocked by Cloudflare (HTTP {r.status})"
                return None, f"HTTP {r.status}"
            return await r.json(content_type=None), None
    except (asyncio.TimeoutError, TimeoutError):
        return None, "timeout"
    except (aiohttp.ClientError, ValueError, OSError) as e:
        return None, f"{e.__class__.__name__}: {e}"[:120]


async def fetch_quote(venue: Venue, session, enabled: Optional[bool] = None) -> CexQuote:
    """The venue's ERG/USDT order book, or a quote carrying the reason there is none."""
    if not (venue.enabled() if enabled is None else enabled):
        return CexQuote(venue.name, None, error=f"disabled: {venue.disabled_reason}")
    started = time.perf_counter()
    data, error = await _get_json(session, venue.book_url)
    ms = (time.perf_counter() - started) * 1000
    logger.debug(f"{venue.name} order book: {ms:.0f} ms{f' ({error})' if error else ''}")
    if error:
        return CexQuote(venue.name, None, error=error, latency_ms=ms)
    try:
        return CexQuote(venue.name, venue.parse_book(data), latency_ms=ms)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        return CexQuote(venue.name, None, error=f"unexpected order book ({e.__class__.__name__})", latency_ms=ms)


async def fetch_all_quotes(session, names=None) -> dict[str, CexQuote]:
    names = list(names or VENUES)
    quotes = await asyncio.gather(*(fetch_quote(VENUES[n], session) for n in names))
    return dict(zip(names, quotes))


@dataclass
class CexFees:
    taker: float                      # fraction of the traded amount
    erg_withdraw: Optional[float]     # ERG per withdrawal; None = unknown (no net figure is computed)
    source: str = "default"           # default (config) | live (published by the exchange)
    live_fields: tuple = ()           # which of "taker", "erg_withdraw" the exchange published

    def describe(self) -> str:
        def tag(f):
            return "live" if f in self.live_fields else "default"
        wd = "withdrawal unknown" if self.erg_withdraw is None else             f"withdrawal {self.erg_withdraw:g} ERG ({tag('erg_withdraw')})"
        return f"taker {self.taker:.2%} ({tag('taker')}), {wd}"


class FeeBook:
    """Per-exchange fees: configured defaults, replaced by what the exchange publishes when it can."""

    def __init__(self):
        self._fees: dict[str, CexFees] = {}
        self.refreshed_at: float = 0.0

    @staticmethod
    def default(name: str) -> CexFees:
        v = VENUES[name]
        return CexFees(taker=float(getattr(config, v.taker_setting)),
                       erg_withdraw=getattr(config, v.withdraw_setting), source="default")

    def get(self, name: str) -> CexFees:
        return self._fees.get(name) or self.default(name)

    def set(self, name: str, fees: CexFees):
        self._fees[name] = fees

    async def refresh(self, session, names=None):
        """Take each exchange's published fees; on any failure keep what is there."""
        names = [n for n in (names or VENUES) if VENUES[n].fee_url and VENUES[n].enabled()]

        async def one(name):
            data, error = await _get_json(session, VENUES[name].fee_url)
            if error:
                logger.debug(f"{name} fee refresh failed: {error}")
                return
            try:
                published = VENUES[name].parse_fees(data)
            except (KeyError, IndexError, TypeError, ValueError) as e:
                logger.debug(f"{name} fee refresh: unexpected answer ({e.__class__.__name__})")
                return
            base = self.get(name)
            live = tuple(dict.fromkeys(base.live_fields + tuple(k for k in ("taker", "erg_withdraw")
                                                                  if k in published)))
            self._fees[name] = CexFees(taker=published.get("taker", base.taker),
                                       erg_withdraw=published.get("erg_withdraw", base.erg_withdraw),
                                       source="live", live_fields=live)

        await asyncio.gather(*(one(n) for n in names))
        self.refreshed_at = time.time()


@dataclass
class Spread:
    buy: str
    sell: str
    size_erg: float
    buy_price: float          # average ask paid for size_erg
    sell_price: float         # average bid received for the ERG that arrives
    gross_percent: float      # price gap only
    net_usdt: float           # after taker fees both sides, the ERG withdrawal, and moving the USDT back
    net_percent: float
    profitable: bool


def best_spread(quotes: dict[str, CexQuote], fees: FeeBook, size_erg: float,
                usdt_transfer_fee: float) -> Optional[Spread]:
    """Best "buy on one exchange, withdraw the ERG, sell on another" at `size_erg`, every fee counted:
    taker fee on both trades, the buying exchange's ERG withdrawal fee, and `usdt_transfer_fee` to move
    the USDT back for the next round. Pairs with an unknown withdrawal fee or too thin a book are skipped."""
    best = None
    live = {n: q for n, q in quotes.items() if q.book is not None}
    for buy, qb in live.items():
        fb = fees.get(buy)
        if fb.erg_withdraw is None:
            continue
        ask = qb.book.effective_buy_price(size_erg)
        if not ask:
            continue
        erg_arriving = size_erg * (1 - fb.taker) - fb.erg_withdraw
        if erg_arriving <= 0:
            continue
        for sell, qs in live.items():
            if sell == buy:
                continue
            bid = qs.book.effective_sell_price(erg_arriving)
            if not bid:
                continue
            cost = size_erg * ask
            usdt_out = erg_arriving * bid * (1 - fees.get(sell).taker) - usdt_transfer_fee
            net = usdt_out - cost
            s = Spread(buy, sell, size_erg, ask, bid, (bid - ask) / ask * 100, net, net / cost * 100,
                       net / cost * 100 > config.MIN_PROFIT_PERCENT)
            if best is None or s.net_percent > best.net_percent:
                best = s
    return best
