"""Unit tests for AMM pool math (constant product formula)."""
from exchanges.base import PoolState, OrderBook, OrderBookLevel


class TestPoolState:
    def setup_method(self):
        """Pool with 10,000 ERG and 5,000 SigUSD (price ~$0.50/ERG)."""
        self.pool = PoolState(
            exchange="test",
            pool_id="test_pool",
            token_x="ERG",
            token_y="SigUSD",
            reserve_x=10_000,
            reserve_y=5_000,
            fee_num=997,
            fee_denom=1000,
        )

    def test_spot_price(self):
        assert self.pool.price_x_in_y == 0.5  # 5000/10000
        assert self.pool.price_y_in_x == 2.0  # 10000/5000

    def test_swap_small_amount(self):
        """1 ERG swap should give close to spot price minus fee."""
        output = self.pool.swap_output(1, input_is_x=True)
        # Should be close to 0.5 SigUSD but slightly less due to fee + impact
        assert 0.49 < output < 0.50
        # Fee is 0.3%, so output should be > 0.497 * spot
        assert output > 0.497 * 0.5

    def test_swap_large_amount_has_more_impact(self):
        """Larger swaps should have more price impact."""
        out_1 = self.pool.swap_output(1, input_is_x=True)
        out_100 = self.pool.swap_output(100, input_is_x=True)
        # Effective price per ERG should be worse for 100 ERG
        eff_1 = out_1 / 1
        eff_100 = out_100 / 100
        assert eff_100 < eff_1

    def test_swap_reverse_direction(self):
        """Swapping SigUSD -> ERG should work correctly."""
        output = self.pool.swap_output(1, input_is_x=False)
        # 1 SigUSD should give close to 2 ERG
        assert 1.9 < output < 2.0

    def test_swap_zero_input(self):
        assert self.pool.swap_output(0, input_is_x=True) == 0

    def test_swap_preserves_constant_product(self):
        """After a swap, k = reserve_x * reserve_y should increase (fees)."""
        k_before = self.pool.reserve_x * self.pool.reserve_y
        input_amt = 100
        output = self.pool.swap_output(input_amt, input_is_x=True)
        new_x = self.pool.reserve_x + input_amt
        new_y = self.pool.reserve_y - output
        k_after = new_x * new_y
        # k should increase because fees stay in the pool
        assert k_after > k_before

    def test_price_impact_increases_with_size(self):
        impact_1 = self.pool.price_impact(1, input_is_x=True)
        impact_10 = self.pool.price_impact(10, input_is_x=True)
        impact_100 = self.pool.price_impact(100, input_is_x=True)
        assert impact_1 < impact_10 < impact_100

    def test_price_impact_small_trade_is_low(self):
        """1 ERG in a 10,000 ERG pool should have < 1% impact."""
        impact = self.pool.price_impact(1, input_is_x=True)
        assert impact < 1.0

    def test_price_impact_large_trade(self):
        """500 ERG (5% of pool) should have significant impact."""
        impact = self.pool.price_impact(500, input_is_x=True)
        assert impact > 3.0

    def test_cannot_drain_pool(self):
        """Swapping a huge amount should not produce more than pool reserves."""
        output = self.pool.swap_output(1_000_000, input_is_x=True)
        assert output < self.pool.reserve_y

    def test_no_fee_pool(self):
        """Pool with 0% fee (fee_num=1000)."""
        pool = PoolState(
            exchange="test", pool_id="no_fee", token_x="A", token_y="B",
            reserve_x=1000, reserve_y=1000, fee_num=1000, fee_denom=1000,
        )
        # With no fee, output should be exactly the AMM formula
        output = pool.swap_output(10, input_is_x=True)
        expected = (10 * 1000) / (1000 + 10)  # xy=k formula
        assert abs(output - expected) < 0.0001


class TestOrderBook:
    def setup_method(self):
        self.ob = OrderBook(
            exchange="test",
            pair="ERG/USDT",
            bids=[
                OrderBookLevel(price=0.31, quantity=50),
                OrderBookLevel(price=0.30, quantity=100),
                OrderBookLevel(price=0.29, quantity=200),
            ],
            asks=[
                OrderBookLevel(price=0.32, quantity=20),
                OrderBookLevel(price=0.33, quantity=80),
                OrderBookLevel(price=0.34, quantity=150),
            ],
        )

    def test_best_bid_ask(self):
        assert self.ob.best_bid.price == 0.31
        assert self.ob.best_ask.price == 0.32

    def test_effective_sell_small(self):
        """Selling 10 ERG should fill at best bid."""
        price = self.ob.effective_sell_price(10)
        assert price == 0.31  # all fills at top bid

    def test_effective_sell_walks_book(self):
        """Selling 60 ERG should walk through multiple bid levels."""
        price = self.ob.effective_sell_price(60)
        # 50 @ 0.31 + 10 @ 0.30 = 15.5 + 3.0 = 18.5 / 60 = 0.30833
        expected = (50 * 0.31 + 10 * 0.30) / 60
        assert abs(price - expected) < 0.0001

    def test_effective_buy_small(self):
        price = self.ob.effective_buy_price(10)
        assert price == 0.32  # all fills at best ask

    def test_effective_buy_walks_book(self):
        """Buying 30 ERG should walk through ask levels."""
        price = self.ob.effective_buy_price(30)
        # 20 @ 0.32 + 10 @ 0.33 = 6.4 + 3.3 = 9.7 / 30
        expected = (20 * 0.32 + 10 * 0.33) / 30
        assert abs(price - expected) < 0.0001

    def test_insufficient_liquidity(self):
        """Requesting more than available should return None."""
        assert self.ob.effective_sell_price(1000) is None
        assert self.ob.effective_buy_price(1000) is None

    def test_empty_orderbook(self):
        empty = OrderBook(exchange="test", pair="X/Y", bids=[], asks=[])
        assert empty.best_bid is None
        assert empty.best_ask is None
        assert empty.effective_sell_price(1) is None
