"""Contract-exact trade sizing for the two on-chain paths (issue #10).

profit_nanoerg() reproduces, in integer contract units, exactly what the planners
in ergo/chain_arb.py compute for the same boxes (tests/test_sizing.py checks this),
so the size the scanner or runner picks is the size the transactions deliver.

best_size() picks a size in [MIN_TRADE_SIZE_ERG, cap]:
  1. the size with the most profit,
  2. shrunk if needed so profit % stays >= MIN_PROFIT_PERCENT,
  3. shrunk to the smallest size earning SIZE_PROFIT_CAPTURE of that profit
     (still meeting MIN_PROFIT_PERCENT).
The search runs over SigUSD cents, not ERG: see by_cents(). Profit is concave in
cents (AMM price impact, flat miner fees), so it rises up to c_max; profit % peaks
at c_pct (< c_max), rising before it and falling after it. Each step is therefore
a bisection over a monotone stretch.
"""
from dataclasses import dataclass
from typing import Callable, Optional

import config
from arbitrage.optimizer import maximize
from ergo.pool_swap import MINER_FEE as POOL_MINER_FEE
from ergo.sigmausd_tx import MINER_FEE as BANK_MINER_FEE, register_int
from exchanges.base import PoolState
from exchanges.sigmausd import (
    BankState,
    affordable_mint_cents,
    can_mint_sigusd,
    mint_cost_nanoerg,
    quote_mint_sigusd,
    quote_redeem_sigusd,
)
from exchanges.spectrum import FEE_DENOM, amm_output_raw

_NO_TRADE = -10.0 ** 18  # nanoERG "profit" for a size that cannot be traded


@dataclass(frozen=True)
class Market:
    """Pool reserves and bank state in raw contract units."""
    pool_erg: int      # nanoERG
    pool_sigusd: int   # cents
    fee_num: int       # pool fee numerator, out of 1000
    bank: BankState

    @classmethod
    def from_boxes(cls, pool_box: dict, bank_box: dict, oracle_box: dict) -> "Market":
        sigusd = next(int(a["amount"]) for a in pool_box["assets"] if a["tokenId"] == config.SIGUSD_TOKEN_ID)
        bank = BankState(int(bank_box["value"]), register_int(bank_box, "R4"), register_int(oracle_box, "R4"))
        return cls(int(pool_box["value"]), sigusd, register_int(pool_box, "R4"), bank)

    @classmethod
    def from_pool_state(cls, pool: PoolState, bank: BankState) -> "Market":
        return cls(round(pool.reserve_x * 1e9), round(pool.reserve_y * 100), pool.fee_num, bank)

    def mint_budget_for(self, cents: int) -> int:
        """Mint budget (bank + UI fee, as the mint planner takes it) that buys exactly `cents`."""
        return mint_cost_nanoerg(self.bank, cents)


def profit_nanoerg(path: str, m: Market, size: int) -> Optional[tuple[int, int]]:
    """(profit, ERG spent) in nanoERG for `size`, or None if it cannot be traded.

    redeem: `size` = ERG swapped on the pool; spent = size.
    mint:   `size` = mint budget; spent = mint cost incl. the leg 1 miner fee.
    """
    if size <= 0:
        return None
    if path == "redeem":
        cents = amm_output_raw(m.pool_erg, m.pool_sigusd, size, m.fee_num)
        if cents <= 0:
            return None
        erg_back = quote_redeem_sigusd(m.bank, cents) - BANK_MINER_FEE
        return erg_back - (size + POOL_MINER_FEE), size
    if path == "mint":
        cents = affordable_mint_cents(m.bank, size)
        if cents <= 0 or not can_mint_sigusd(m.bank, cents):
            return None
        cost = mint_cost_nanoerg(m.bank, cents) + BANK_MINER_FEE
        erg_back = amm_output_raw(m.pool_sigusd, m.pool_erg, cents, m.fee_num) - POOL_MINER_FEE
        return erg_back - cost, cost
    raise ValueError(f"unknown path {path!r}")




def erg_for_cents(m: Market, cents: int) -> Optional[int]:
    """Least nanoERG the pool takes to pay out `cents` SigUSD (exact inverse of amm_output_raw)."""
    if cents <= 0 or cents >= m.pool_sigusd:
        return None
    # ry*x*f // (rx*1000 + x*f) >= c  <=>  x*f*(ry - c) >= c*rx*1000
    num, den = cents * m.pool_erg * FEE_DENOM, m.fee_num * (m.pool_sigusd - cents)
    return -(-num // den)


def by_cents(path: str, m: Market, cents: int) -> Optional[tuple[int, int, int]]:
    """(planner size, profit, ERG spent) in nanoERG for a trade moving exactly `cents` SigUSD.

    SigUSD moves in whole cents, so profit as a function of ERG is a sawtooth whose
    peaks sit exactly at these sizes: any more ERG buys nothing until the next cent.
    """
    if path == "redeem":
        size = erg_for_cents(m, cents)
    elif path == "mint":
        size = m.mint_budget_for(cents) if cents > 0 else None
    else:
        raise ValueError(f"unknown path {path!r}")
    r = profit_nanoerg(path, m, size) if size else None
    return (size, *r) if r else None


def cents_for_size(path: str, m: Market, size: int) -> int:
    """SigUSD cents a trade of `size` nanoERG moves (RR-limited for mint)."""
    if size <= 0:
        return 0
    if path == "redeem":
        return amm_output_raw(m.pool_erg, m.pool_sigusd, size, m.fee_num)
    return quote_mint_sigusd(m.bank, size)


@dataclass
class SizeChoice:
    path: str
    size_nanoerg: int = 0           # exact size to pass the planner (pool ERG in / mint budget)
    sigusd_cents: int = 0
    profit_erg: float = 0.0
    profit_percent: float = 0.0
    cost_erg: float = 0.0           # ERG actually spent
    max_profit_size_erg: float = 0.0
    max_profit_erg: float = 0.0
    break_even_erg: Optional[float] = None
    cap_erg: float = 0.0
    reason: str = ""                # why not to trade; empty = trade size_nanoerg

    @property
    def ok(self) -> bool:
        return not self.reason

    @property
    def size_erg(self) -> float:
        return self.size_nanoerg / 1e9

    def summary(self) -> str:
        be = f", break-even {self.break_even_erg:.2f} ERG" if self.break_even_erg else ""
        if self.ok:
            return (f"{self.size_erg:.4f} ERG -> {self.profit_erg:+.4f} ERG ({self.profit_percent:+.2f}%)"
                    f"; peak {self.max_profit_erg:+.4f} ERG at {self.max_profit_size_erg:.2f} ERG{be}")
        return f"{self.reason}{be}"


def _int_boundary(good: Callable[[int], bool], inside: int, outside: int) -> int:
    """Last integer from `inside` towards `outside` where `good` holds (good(inside) is True)."""
    if good(outside):
        return outside
    while abs(outside - inside) > 1:
        mid = (inside + outside) // 2
        if good(mid):
            inside = mid
        else:
            outside = mid
    return inside


def _int_argmax(f: Callable[[int], float], lo: int, hi: int) -> int:
    """Integer maximising a (near-)concave f on [lo, hi]: search, then polish around it."""
    x, _ = maximize(lambda v: f(int(round(v))), lo, hi, tol=1.0) if hi > lo else (lo, None)
    x = int(round(x))
    window = range(max(lo, x - 3), min(hi, x + 3) + 1)
    return max(window, key=f)


def best_size(path: str, m: Market, cap_erg: float, *, min_erg: Optional[float] = None,
              min_pct: Optional[float] = None, capture: Optional[float] = None) -> SizeChoice:
    """Size to trade on `path`, spending between min_erg and cap_erg; defaults come from config."""
    lo_erg = config.MIN_TRADE_SIZE_ERG if min_erg is None else min_erg
    min_pct = config.MIN_PROFIT_PERCENT if min_pct is None else min_pct
    capture = config.SIZE_PROFIT_CAPTURE if capture is None else capture
    capture = min(max(capture, 0.0), 1.0)
    choice = SizeChoice(path, cap_erg=cap_erg)

    if path == "mint" and not can_mint_sigusd(m.bank, 1):
        choice.reason = f"bank mint blocked (RR {m.bank.reserve_ratio:.0f}%, must stay >= 400% after minting)"
        return choice
    c_hi = cents_for_size(path, m, int(cap_erg * 1e9))
    c_lo = max(cents_for_size(path, m, int(lo_erg * 1e9)), 1)
    lo_size = by_cents(path, m, c_lo)
    if lo_size and lo_size[0] < lo_erg * 1e9:  # those cents cost a little less than the minimum
        c_lo += 1
    if cap_erg < lo_erg or c_hi < c_lo:
        choice.reason = f"cap {cap_erg:.2f} ERG is below MIN_TRADE_SIZE_ERG {lo_erg:g}"
        return choice

    cache: dict[int, Optional[tuple[int, int, int]]] = {}

    def exact(c: int):
        if c not in cache:
            cache[c] = by_cents(path, m, c)
        return cache[c]

    def profit(c: int) -> float:
        r = exact(c)
        return r[1] if r else _NO_TRADE

    def pct(c: int) -> float:
        r = exact(c)
        return r[1] / r[2] * 100 if r else _NO_TRADE

    c_max = _int_argmax(profit, c_lo, c_hi)
    choice.max_profit_size_erg = exact(c_max)[0] / 1e9 if exact(c_max) else 0.0
    choice.max_profit_erg = profit(c_max) / 1e9
    if profit(c_max) <= 0:
        choice.reason = (f"no profitable size up to {cap_erg:.2f} ERG "
                         f"(best {choice.max_profit_erg:+.4f} ERG at {choice.max_profit_size_erg:.2f})")
        return choice
    if profit(1) >= 0:
        choice.break_even_erg = exact(1)[0] / 1e9
    else:
        be = _int_boundary(lambda c: profit(c) < 0, 1, c_max) + 1
        choice.break_even_erg = exact(be)[0] / 1e9

    c_pct = _int_argmax(pct, c_lo, c_max)
    if pct(c_pct) < min_pct:
        choice.reason = (f"best {pct(c_pct):+.2f}% (at {exact(c_pct)[0] / 1e9:.2f} ERG) is below "
                         f"MIN_PROFIT_PERCENT {min_pct:g}%")
        return choice

    meets_pct = lambda c: pct(c) >= min_pct
    c_best = c_max if meets_pct(c_max) else _int_boundary(meets_pct, c_pct, c_max)
    # Smallest size earning `capture` of that profit. Profit rises with size up to c_best;
    # left of the % peak, % rises with size too, so step up until the % floor holds.
    target = capture * profit(c_best)
    earns = lambda c: profit(c) >= target
    c = c_lo if earns(c_lo) else _int_boundary(earns, c_best, c_lo)
    if not meets_pct(c):
        c = _int_boundary(meets_pct, c_pct, c)

    size, prof, spent = exact(c)
    choice.size_nanoerg, choice.sigusd_cents = size, c
    choice.profit_erg, choice.cost_erg, choice.profit_percent = prof / 1e9, spent / 1e9, pct(c)
    return choice
