import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class PriceQuote:
    exchange: str
    pair: str
    bid: float  # best buy price (what you get when selling)
    ask: float  # best sell price (what you pay when buying)
    bid_volume: float = 0.0  # volume available at bid
    ask_volume: float = 0.0  # volume available at ask
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def mid_price(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_percent(self) -> float:
        if self.bid == 0:
            return 0
        return ((self.ask - self.bid) / self.bid) * 100


@dataclass
class OrderBookLevel:
    price: float
    quantity: float


@dataclass
class OrderBook:
    exchange: str
    pair: str
    bids: list[OrderBookLevel]  # sorted descending by price
    asks: list[OrderBookLevel]  # sorted ascending by price
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def best_bid(self) -> Optional[OrderBookLevel]:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[OrderBookLevel]:
        return self.asks[0] if self.asks else None

    def effective_sell_price(self, quantity: float) -> Optional[float]:
        """Price you'd get selling `quantity` ERG (walking the bid side)."""
        remaining = quantity
        total_value = 0.0
        for level in self.bids:
            fill = min(remaining, level.quantity)
            total_value += fill * level.price
            remaining -= fill
            if remaining <= 0:
                return total_value / quantity
        return None  # not enough liquidity

    def effective_buy_price(self, quantity: float) -> Optional[float]:
        """Price you'd pay buying `quantity` ERG (walking the ask side)."""
        remaining = quantity
        total_cost = 0.0
        for level in self.asks:
            fill = min(remaining, level.quantity)
            total_cost += fill * level.price
            remaining -= fill
            if remaining <= 0:
                return total_cost / quantity
        return None  # not enough liquidity


@dataclass
class PoolState:
    exchange: str
    pool_id: str
    token_x: str  # e.g. "ERG"
    token_y: str  # e.g. "SigUSD"
    reserve_x: float
    reserve_y: float
    fee_num: int = 997  # fee numerator (out of 1000)
    fee_denom: int = 1000
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def price_x_in_y(self) -> float:
        """Price of token X in terms of token Y."""
        if self.reserve_x == 0:
            return 0
        return self.reserve_y / self.reserve_x

    @property
    def price_y_in_x(self) -> float:
        """Price of token Y in terms of token X."""
        if self.reserve_y == 0:
            return 0
        return self.reserve_x / self.reserve_y

    def swap_output(self, input_amount: float, input_is_x: bool) -> float:
        """Calculate output amount for a swap using constant product formula with fees."""
        if input_is_x:
            reserve_in = self.reserve_x
            reserve_out = self.reserve_y
        else:
            reserve_in = self.reserve_y
            reserve_out = self.reserve_x

        input_with_fee = input_amount * self.fee_num
        numerator = input_with_fee * reserve_out
        denominator = (reserve_in * self.fee_denom) + input_with_fee

        if denominator == 0:
            return 0
        return numerator / denominator

    def price_impact(self, input_amount: float, input_is_x: bool) -> float:
        """Calculate price impact as a percentage."""
        spot_price = self.price_x_in_y if input_is_x else self.price_y_in_x
        if spot_price == 0:
            return 0
        output = self.swap_output(input_amount, input_is_x)
        effective_price = output / input_amount if input_amount > 0 else 0
        return abs(1 - (effective_price / spot_price)) * 100


class RateLimiter:
    """Simple async rate limiter using a sliding window."""

    def __init__(self, max_calls: int, period_seconds: float):
        self.max_calls = max_calls
        self.period = period_seconds
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()   # one caller at a time, so waiters can't all pass at once after waking

    async def acquire(self):
        """Wait if necessary to stay within rate limits."""
        async with self._lock:
            while True:
                now = time.monotonic()
                self._timestamps = [t for t in self._timestamps if now - t < self.period]
                if len(self._timestamps) < self.max_calls:
                    break
                await asyncio.sleep(self._timestamps[0] + self.period - now)
            self._timestamps.append(time.monotonic())


class ExchangeBase(ABC):
    @abstractmethod
    async def get_price(self, pair: str) -> Optional[PriceQuote]:
        pass

    @abstractmethod
    async def connect(self):
        pass

    @abstractmethod
    async def disconnect(self):
        pass


class CEXBase(ExchangeBase):
    @abstractmethod
    async def get_orderbook(self, pair: str, depth: int = 20) -> Optional[OrderBook]:
        pass

    @abstractmethod
    async def get_balance(self, asset: str) -> float:
        pass

    @abstractmethod
    async def get_withdraw_fee(self, asset: str) -> float:
        pass

    @abstractmethod
    async def get_trading_fee(self, pair: str) -> float:
        pass


class DEXBase(ExchangeBase):
    @abstractmethod
    async def get_pool_state(self, pair: str) -> Optional[PoolState]:
        pass
