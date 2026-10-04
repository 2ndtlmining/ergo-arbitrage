import logging
import math
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger("ergo_arb.sigmausd")

# Off-chain oracle frontend, used only as a fallback for the on-chain oracle box
ORACLE_POOL_API = "https://erg-oracle-ergusd.spirepools.com/frontendData"
ORACLE_DIVERGENCE_WARN = 0.005  # warn when on-chain and frontend oracle differ by >0.5%

# AgeUSD contract constants
MIN_RESERVE_RATIO = 400  # percent, checked after a SigUSD mint
PROTOCOL_FEE_PERCENT = 2  # integer percent, truncated toward zero
UI_FEE_NUM = 229  # 0.229% of bc_delta
UI_FEE_DENOM = 100_000
UI_FEE_MIN_NANOERG = 1_000_000  # 0.001 ERG


def _trunc_div(a: int, b: int) -> int:
    """Integer division truncating toward zero (JVM/Scala semantics)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def ui_fee_nanoerg(bc_delta: int) -> int:
    return max(bc_delta * UI_FEE_NUM // UI_FEE_DENOM, UI_FEE_MIN_NANOERG)


@dataclass(frozen=True)
class BankState:
    """Raw on-chain SigmaUSD bank state, in contract units."""
    bank_erg_nano: int       # bank box value
    sigusd_circ_cents: int   # bank R4
    oracle_r4: int           # oracle pool box R4: nanoERG per USD

    @property
    def rate(self) -> int:
        """nanoERG per SigUSD cent, as the contract computes it."""
        return self.oracle_r4 // 100

    @property
    def oracle_usd_per_erg(self) -> float:
        return 1e9 / self.oracle_r4 if self.oracle_r4 else 0.0

    @property
    def reserve_ratio(self) -> float:
        """Reserve ratio in percent (float, for display)."""
        liabilities = self.sigusd_circ_cents * self.rate
        if liabilities <= 0:
            return float("inf")
        return self.bank_erg_nano * 100 / liabilities

    def nominal_price(self) -> int:
        """Contract scNominalPrice: min(rate, liabilities / circulating)."""
        if self.sigusd_circ_cents <= 0:
            return self.rate
        liabilities = max(min(self.bank_erg_nano, self.sigusd_circ_cents * self.rate), 0)
        return min(self.rate, liabilities // self.sigusd_circ_cents)


def quote_redeem_sigusd(state: BankState, cents: int) -> int:
    """nanoERG paid to the user for redeeming `cents` SigUSD.

    Contract-exact: nominal price (pro-rata when RR < 100%), 2% protocol fee
    truncated toward zero, then the UI fee. Miner fee and receipt box are not
    included (see config.SIGMAUSD_REDEEM_EXTRA_ERG).
    """
    if cents <= 0:
        return 0
    br_delta_expected = state.nominal_price() * -cents
    fee = abs(_trunc_div(br_delta_expected * PROTOCOL_FEE_PERCENT, 100))
    bc_delta = -(br_delta_expected + fee)  # ERG leaving the bank
    return max(bc_delta - ui_fee_nanoerg(bc_delta), 0)


def _mint_bc_delta(state: BankState, cents: int) -> int:
    br_delta_expected = state.nominal_price() * cents
    fee = abs(_trunc_div(br_delta_expected * PROTOCOL_FEE_PERCENT, 100))
    return br_delta_expected + fee


def mint_cost_nanoerg(state: BankState, cents: int) -> int:
    """Total nanoERG the user pays (bank + UI fee) to mint `cents` SigUSD."""
    if cents <= 0:
        return 0
    bc_delta = _mint_bc_delta(state, cents)
    return bc_delta + ui_fee_nanoerg(bc_delta)


def can_mint_sigusd(state: BankState, cents: int) -> bool:
    """True if minting `cents` SigUSD keeps the post-mint RR >= 400%."""
    if cents <= 0 or state.rate <= 0:
        return False
    # bank.es: reserveRatioPercentOut = bcReserveOut * 100 / (scCircOut * rate)
    bank_out = state.bank_erg_nano + _mint_bc_delta(state, cents)
    needed_out = (state.sigusd_circ_cents + cents) * state.rate
    if needed_out <= 0:
        return True
    return bank_out * 100 // needed_out >= MIN_RESERVE_RATIO


def _max_true(lo: int, hi: int, pred) -> int:
    """Largest n in [lo, hi] with pred(n) True, assuming pred is monotone decreasing."""
    if not pred(lo):
        return lo - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if pred(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def affordable_mint_cents(state: BankState, budget_nanoerg: int) -> int:
    """Max SigUSD cents whose mint cost fits `budget_nanoerg`, ignoring the RR rule."""
    if state.rate <= 0 or budget_nanoerg <= 0:
        return 0
    upper = budget_nanoerg // state.rate + 1
    return max(_max_true(1, upper, lambda c: mint_cost_nanoerg(state, c) <= budget_nanoerg), 0)


def quote_mint_sigusd(state: BankState, budget_nanoerg: int) -> int:
    """Max SigUSD cents mintable with `budget_nanoerg` (0 if minting is blocked)."""
    affordable = affordable_mint_cents(state, budget_nanoerg)
    if affordable < 1:
        return 0
    return max(_max_true(1, affordable, lambda c: can_mint_sigusd(state, c)), 0)


def mint_open_price(state: BankState) -> Optional[float]:
    """ERG/USD oracle price at which the reserve ratio reaches 400% (circulation and reserve fixed).

    RR is proportional to the oracle price, so it is the current price scaled by 400 / RR.
    None when there is no ratio to scale (no SigUSD circulating, or no oracle price).
    """
    rr, oracle = state.reserve_ratio, state.oracle_usd_per_erg
    if not oracle or not math.isfinite(rr) or rr <= 0:
        return None
    return oracle * MIN_RESERVE_RATIO / rr


def mint_room_cents(state: BankState) -> int:
    """Most SigUSD cents mintable now with the post-mint RR still >= 400% (0 when blocked).

    can_mint_sigusd is monotone decreasing in cents: each extra cent adds about 1.02 * nominal price
    of ERG but needs 4 * rate, and the nominal price never exceeds the rate.
    """
    if state.rate <= 0:
        return 0
    upper = state.bank_erg_nano // state.rate + 1   # more SigUSD than the whole reserve could ever cover
    return max(_max_true(1, upper, lambda c: can_mint_sigusd(state, c)), 0)


def mint_room_nanoerg(state: BankState) -> int:
    """ERG (nanoERG) the largest allowed mint costs: how much ERG the mint gate can take right now."""
    cents = mint_room_cents(state)
    return mint_cost_nanoerg(state, cents) if cents > 0 else 0


def parse_bank_box(box: dict) -> tuple[int, int]:
    """(bank nanoERG, SigUSD circulating cents) from an explorer/node bank box."""
    regs = box.get("additionalRegisters", {})
    r4 = regs.get("R4", {})
    circ = r4.get("renderedValue") if isinstance(r4, dict) else None
    return int(box["value"]), int(circ)


def parse_oracle_box(box: dict) -> int:
    regs = box.get("additionalRegisters", {})
    r4 = regs.get("R4", {})
    return int(r4.get("renderedValue") if isinstance(r4, dict) else r4)
