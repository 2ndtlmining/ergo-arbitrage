"""Where SigUSD is worth the most: the exits for a SigUSD amount, in ERG and in dollars (information only).

  bank redeem -> ERG              at the oracle price, minus 2.229% fees and the miner fee
  pool -> ERG                     the ErgoDEX pool, exact contract math (fee and price impact)
  pool -> ERG -> <exchange> USDT  then sent to the exchange and sold into its order book (depth and taker fee)

ERG results are valued at the oracle price; USDT at $1. The "premium" is how much more ERG the pool pays
than the bank: high means selling SigUSD in the pool is the better exit, negative means SigUSD is cheap in
the pool. Nothing here trades; exchange deposits are assumed free (check your exchange).
"""
from dataclasses import dataclass
from typing import Optional

from arbitrage.sizing import BANK_MINER_FEE, POOL_MINER_FEE, Market
from exchanges.sigmausd import quote_redeem_sigusd
from exchanges.spectrum import amm_output_raw

SEND_MINER_FEE = 1_100_000          # nanoERG: sending the ERG to the exchange's deposit address
EXAMPLE_SIGUSD = 100.0              # quoted when the wallet holds no SigUSD


@dataclass
class Route:
    name: str
    amount: Optional[float]         # what you end up with (None: not possible right now)
    unit: str                       # "ERG" or "USDT"
    usd: Optional[float]            # its dollar value (ERG at the oracle price)
    note: str = ""

    @property
    def usd_text(self) -> str:
        return f"${self.usd:,.2f}" if self.usd is not None else "—"


@dataclass
class SigusdRoutes:
    sigusd: float                   # the amount quoted
    example: bool                   # True when the wallet holds none and EXAMPLE_SIGUSD is quoted
    routes: list                    # best first (by dollar value)
    premium_percent: Optional[float]   # pool ERG vs bank ERG for this amount

    @property
    def best(self) -> Optional[Route]:
        return next((r for r in self.routes if r.usd is not None), None)

    def vs_face(self, r: Route) -> Optional[float]:
        return (r.usd / self.sigusd - 1) * 100 if r.usd is not None and self.sigusd else None

    def vs_bank(self, r: Route) -> Optional[float]:
        bank = next((x for x in self.routes if x.name == "bank redeem → ERG"), None)
        if bank is None or bank.usd in (None, 0) or r.usd is None or r is bank:
            return None
        return (r.usd / bank.usd - 1) * 100

    def summary(self) -> str:
        """One line for the digest and the wallet panel, e.g. "best pool → ERG → Gate $207.41 (+3.7%)"."""
        b = self.best
        if b is None:
            return "no route priced right now"
        face = self.vs_face(b)
        premium = f", pool premium {self.premium_percent:+.1f}%" if self.premium_percent is not None else ""
        return f"best {b.name} {b.amount:,.2f} {b.unit} ({b.usd_text}, {face:+.1f}% vs ${self.sigusd:,.0f}){premium}"


def sigusd_routes(sigusd: float, market: Market, quotes: Optional[dict] = None, fees=None) -> SigusdRoutes:
    """Every exit for `sigusd` SigUSD (EXAMPLE_SIGUSD when it is 0), best dollar value first."""
    example = not sigusd or sigusd <= 0
    amount = EXAMPLE_SIGUSD if example else float(sigusd)
    cents = int(round(amount * 100))
    oracle = market.bank.oracle_usd_per_erg
    routes = []

    bank_nano = quote_redeem_sigusd(market.bank, cents) - BANK_MINER_FEE
    bank_erg = bank_nano / 1e9 if bank_nano > 0 else None
    routes.append(Route("bank redeem → ERG", bank_erg, "ERG", bank_erg * oracle if bank_erg else None))

    pool_nano = amm_output_raw(market.pool_sigusd, market.pool_erg, cents, market.fee_num) - POOL_MINER_FEE
    pool_erg = pool_nano / 1e9 if pool_nano > 0 else None
    routes.append(Route("pool → ERG", pool_erg, "ERG", pool_erg * oracle if pool_erg else None))

    for name, q in sorted((quotes or {}).items()):
        book = getattr(q, "book", None)
        if book is None or not pool_erg:
            continue
        erg = pool_erg - SEND_MINER_FEE / 1e9
        price = book.effective_sell_price(erg)
        if price is None:
            routes.append(Route(f"pool → ERG → {name}", None, "USDT", None, "order book too thin"))
            continue
        taker = fees.get(name).taker if fees is not None else 0.0
        usdt = erg * price * (1 - taker)
        routes.append(Route(f"pool → ERG → {name}", usdt, "USDT", usdt))

    routes.sort(key=lambda r: (r.usd is None, -(r.usd or 0)))
    premium = (pool_erg / bank_erg - 1) * 100 if pool_erg and bank_erg else None
    return SigusdRoutes(amount, example, routes, premium)
