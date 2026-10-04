from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass

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
