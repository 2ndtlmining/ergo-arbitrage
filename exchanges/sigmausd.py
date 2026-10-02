import json
import logging
import math
from dataclasses import dataclass
from typing import Optional

import aiohttp

import config
from exchanges.base import DEXBase, PriceQuote, PoolState

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

    can_mint_sigusd is monotone decreasing in cents whenever RR > 102% (a mint adds ERG at ~102%
    collateral); below that, minting one cent is already blocked, so the search returns 0.
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


class SigmaUSDBank(DEXBase):
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.state: Optional[BankState] = None
        self._oracle_price: Optional[float] = None  # USD per ERG
        self._oracle_r4: Optional[int] = None
        self._bank_erg_reserve: Optional[float] = None
        self._sigusd_circulating: Optional[float] = None
        self._bank_erg_nano: Optional[int] = None
        self._sigusd_circ_cents: Optional[int] = None

    async def connect(self):
        self.session = aiohttp.ClientSession()
        logger.info("[exchange]SigmaUSD Bank[/exchange] connected")

    async def disconnect(self):
        if self.session:
            await self.session.close()
            self.session = None
        logger.info("SigmaUSD Bank disconnected")

    async def _explorer_unspent_by_token(self, token_id: str) -> Optional[dict]:
        url = f"{config.ERGO_EXPLORER_API_URL}/boxes/unspent/byTokenId/{token_id}?limit=1"
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning(f"Explorer byTokenId {token_id[:8]} returned {resp.status}")
                return None
            data = await resp.json()
            items = data.get("items", []) if isinstance(data, dict) else data
            return items[0] if items else None

    async def _fetch_frontend_oracle_price(self) -> Optional[float]:
        async with self.session.get(ORACLE_POOL_API, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            raw = await resp.json(content_type=None)
            data = json.loads(raw) if isinstance(raw, str) else raw
            price = data.get("latest_price", 0)
            return float(price) if isinstance(price, (int, float)) and price > 0 else None

    async def fetch_oracle_price(self) -> Optional[float]:
        """ERG/USD from the on-chain oracle pool box (what the bank contract uses).

        Falls back to the spirepools frontend if the box can't be read, and warns
        when the two disagree.
        """
        if not self.session:
            return None
        onchain = frontend = None
        try:
            box = await self._explorer_unspent_by_token(config.SIGMAUSD_ORACLE_NFT)
            if box:
                self._oracle_r4 = parse_oracle_box(box)
                onchain = 1e9 / self._oracle_r4
        except Exception as e:
            logger.error(f"On-chain oracle fetch error: {e}")
        try:
            frontend = await self._fetch_frontend_oracle_price()
        except Exception as e:
            logger.debug(f"Frontend oracle fetch error: {e}")

        if onchain and frontend and abs(onchain - frontend) / onchain > ORACLE_DIVERGENCE_WARN:
            logger.warning(f"Oracle divergence: on-chain ${onchain:.4f} vs frontend ${frontend:.4f}")
        if onchain:
            self._oracle_price = onchain
        elif frontend:
            logger.warning("On-chain oracle unavailable, using frontend price (bank quotes disabled)")
            self._oracle_price = frontend
            self._oracle_r4 = None
        else:
            return None
        return self._oracle_price

    async def fetch_bank_state(self) -> bool:
        """Fetch bank box: reserve (nanoERG) and SigUSD circulating (R4, cents)."""
        if not self.session:
            return False
        try:
            box = await self._explorer_unspent_by_token(config.SIGMAUSD_BANK_NFT)
            if not box:
                return False
            self._bank_erg_nano, self._sigusd_circ_cents = parse_bank_box(box)
            self._bank_erg_reserve = self._bank_erg_nano / 1e9
            self._sigusd_circulating = self._sigusd_circ_cents / 100
            return True
        except Exception as e:
            logger.error(f"Bank state fetch error: {e}")
            return False

    def _refresh_state(self):
        if None not in (self._bank_erg_nano, self._sigusd_circ_cents, self._oracle_r4):
            self.state = BankState(self._bank_erg_nano, self._sigusd_circ_cents, self._oracle_r4)
        else:
            self.state = None

    @property
    def reserve_ratio(self) -> Optional[float]:
        """Current reserve ratio as a percentage."""
        if (
            self._bank_erg_reserve is not None
            and self._oracle_price is not None
            and self._sigusd_circulating is not None
            and self._sigusd_circulating > 0
        ):
            reserve_value_usd = self._bank_erg_reserve * self._oracle_price
            return (reserve_value_usd / self._sigusd_circulating) * 100
        return None

    def can_mint_sigusd(self, cents: int = 1) -> bool:
        """True if minting `cents` SigUSD keeps RR >= 400% after the mint."""
        return self.state is not None and can_mint_sigusd(self.state, cents)

    def can_redeem_sigusd(self) -> bool:
        """SigUSD redeem has no RR restriction; only needs known bank state."""
        return self.reserve_ratio is not None

    def erg_to_sigusd(self, erg_amount: float) -> float:
        """SigUSD received for minting with `erg_amount` ERG (contract-exact)."""
        if self.state is None:
            return 0
        return quote_mint_sigusd(self.state, int(erg_amount * 1e9)) / 100

    def sigusd_to_erg(self, sigusd_amount: float) -> float:
        """ERG received for redeeming SigUSD, after receipt box and miner fee."""
        if self.state is None:
            return 0
        net = quote_redeem_sigusd(self.state, int(sigusd_amount * 100)) / 1e9
        return max(net - config.SIGMAUSD_REDEEM_EXTRA_ERG, 0)

    async def get_price(self, pair: str = "ERG/SigUSD") -> Optional[PriceQuote]:
        oracle_price = await self.fetch_oracle_price()
        if oracle_price is None:
            return None
        bid = oracle_price * (1 - config.SIGMAUSD_PROTOCOL_FEE) * (1 - config.SIGMAUSD_FRONTEND_FEE)
        ask = oracle_price / ((1 - config.SIGMAUSD_PROTOCOL_FEE) * (1 - config.SIGMAUSD_FRONTEND_FEE))
        return PriceQuote(exchange="SigmaUSD Bank", pair=pair, bid=bid, ask=ask)

    async def get_pool_state(self, pair: str = "ERG/SigUSD") -> Optional[PoolState]:
        # SigmaUSD bank is not an AMM pool, but we can represent its state
        await self.fetch_bank_state()
        if self._bank_erg_reserve is None:
            return None
        return PoolState(
            exchange="SigmaUSD Bank",
            pool_id="sigmausd_bank",
            token_x="ERG",
            token_y="SigUSD",
            reserve_x=self._bank_erg_reserve or 0,
            reserve_y=self._sigusd_circulating or 0,
        )

    async def get_full_state(self) -> dict:
        """Bank state for the scanner. `state` is the contract-exact BankState (or None)."""
        import asyncio
        await asyncio.gather(self.fetch_oracle_price(), self.fetch_bank_state())
        self._refresh_state()
        return {
            "oracle_erg_usd": self._oracle_price,
            "bank_erg_reserve": self._bank_erg_reserve,
            "sigusd_circulating": self._sigusd_circulating,
            "reserve_ratio": self.reserve_ratio,
            "can_mint_sigusd": self.can_mint_sigusd(),
            "can_redeem_sigusd": self.can_redeem_sigusd() and self.state is not None,
            "state": self.state,
        }
