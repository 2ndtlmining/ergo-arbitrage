"""Unit tests for arbitrage profit calculator."""
import pytest
from arbitrage.calculator import ArbitrageCalculator, FeeBreakdown
import config


class TestFeeBreakdown:
    def test_total_fee_erg(self):
        fees = FeeBreakdown(
            withdraw_fee_erg=3.3,
            network_fee_erg=0.0022,
            execution_fee_erg=0.002,
            slippage_cost=0.05,
        )
        assert abs(fees.total_fee_erg - 3.3542) < 0.0001

    def test_total_fee_usd(self):
        fees = FeeBreakdown(trading_fee=0.062, protocol_fee=0.066)
        assert abs(fees.total_fee_usd - 0.128) < 0.0001

    def test_zero_fees(self):
        fees = FeeBreakdown()
        assert fees.total_fee_erg == 0
        assert fees.total_fee_usd == 0


class TestBankToDex:
    def setup_method(self):
        self.calc = ArbitrageCalculator()

    def test_profitable_when_dex_cheaper(self):
        """Bank values ERG at $0.32, DEX at $0.29 -> profit by minting then swapping."""
        # Bank rate after fees: $0.32 * 0.98 * 0.99771 = ~$0.3127/ERG
        bank_rate = 0.32 * (1 - config.SIGMAUSD_PROTOCOL_FEE) * (1 - config.SIGMAUSD_FRONTEND_FEE)
        input_erg = 100  # Need larger trade to absorb ~0.785 ERG service fee
        sigusd_from_bank = input_erg * bank_rate
        # DEX: SigUSD / $0.29 * (1-0.5%) = ERG output
        erg_from_dex = (sigusd_from_bank / 0.29) * (1 - config.SPECTRUM_POOL_FEE)

        opp = self.calc.calc_bank_to_dex(
            input_erg=input_erg,
            bank_erg_to_sigusd_rate=bank_rate,
            dex_sigusd_to_erg_output=erg_from_dex,
            slippage=0.005,
        )
        assert opp.profit_erg > 0
        assert opp.is_profitable

    def test_unprofitable_when_prices_close(self):
        """When bank and DEX prices are similar, fees eat the profit."""
        bank_rate = 0.30 * (1 - config.SIGMAUSD_PROTOCOL_FEE) * (1 - config.SIGMAUSD_FRONTEND_FEE)
        input_erg = 10
        sigusd = input_erg * bank_rate
        erg_from_dex = (sigusd / 0.30) * (1 - config.SPECTRUM_POOL_FEE)

        opp = self.calc.calc_bank_to_dex(
            input_erg=input_erg,
            bank_erg_to_sigusd_rate=bank_rate,
            dex_sigusd_to_erg_output=erg_from_dex,
            slippage=0.01,
        )
        # Round trip through bank (2.225%) + DEX (0.5%) + slippage + service fee should be a loss
        assert opp.profit_erg < 0
        assert not opp.is_profitable

    def test_fees_are_populated(self):
        bank_rate = 0.32 * (1 - config.SIGMAUSD_PROTOCOL_FEE) * (1 - config.SIGMAUSD_FRONTEND_FEE)
        opp = self.calc.calc_bank_to_dex(
            input_erg=10,
            bank_erg_to_sigusd_rate=bank_rate,
            dex_sigusd_to_erg_output=10.5,
            slippage=0.01,
        )
        assert opp.fees.network_fee_erg > 0
        assert opp.fees.execution_fee_erg > 0


class TestDexToBank:
    def setup_method(self):
        self.calc = ArbitrageCalculator()

    def test_profitable_when_dex_higher(self):
        """DEX prices ERG at $0.35, bank at $0.30 -> profit by swapping then redeeming."""
        input_erg = 10
        sigusd_from_dex = input_erg * 0.35 * (1 - config.SPECTRUM_POOL_FEE)
        bank_redeem_rate = (1.0 / 0.30) * (1 - config.SIGMAUSD_PROTOCOL_FEE) * (1 - config.SIGMAUSD_FRONTEND_FEE)

        opp = self.calc.calc_dex_to_bank(
            input_erg=input_erg,
            dex_erg_to_sigusd_output=sigusd_from_dex,
            bank_sigusd_to_erg_rate=bank_redeem_rate,
            slippage=0.005,
        )
        assert opp.profit_erg > 0

    def test_round_trip_bank_always_loses(self):
        """Minting and redeeming at bank at same price should always lose ~4.4%."""
        oracle_price = 0.30  # USD per ERG
        # Apply 2% protocol fee then 0.229% UI fee (compounded, not summed)
        fee_multiplier = (1 - config.SIGMAUSD_PROTOCOL_FEE) * (1 - config.SIGMAUSD_FRONTEND_FEE)
        bank_mint_rate = oracle_price * fee_multiplier
        input_erg = 100
        sigusd = input_erg * bank_mint_rate
        bank_redeem_rate = (1.0 / oracle_price) * fee_multiplier
        erg_back = sigusd * bank_redeem_rate

        # Pure bank round trip: ~4.4% loss (2.225% each way, compounded)
        loss_pct = (1 - erg_back / input_erg) * 100
        assert 4.0 < loss_pct < 5.0


class TestCexToCex:
    def setup_method(self):
        self.calc = ArbitrageCalculator()

    def test_profitable_large_spread(self):
        """10% price difference between exchanges should be profitable."""
        opp = self.calc.calc_cex_to_cex(
            input_erg=100,
            sell_price_usdt=0.33,
            buy_price_usdt=0.30,
            erg_withdraw_fee=3.3,
        )
        assert opp.profit_erg > 0

    def test_withdrawal_fee_kills_small_trades(self):
        """3.3 ERG withdrawal fee makes small trades unprofitable."""
        opp = self.calc.calc_cex_to_cex(
            input_erg=10,
            sell_price_usdt=0.33,
            buy_price_usdt=0.30,
            erg_withdraw_fee=3.3,
        )
        # 10% spread on 10 ERG = 1 ERG, but 3.3 ERG withdraw fee
        assert opp.profit_erg < 0

    def test_same_price_always_loses(self):
        """Same price on both exchanges should lose due to fees."""
        opp = self.calc.calc_cex_to_cex(
            input_erg=100,
            sell_price_usdt=0.30,
            buy_price_usdt=0.30,
            erg_withdraw_fee=3.3,
        )
        assert opp.profit_erg < 0


class TestCexVsDex:
    def setup_method(self):
        self.calc = ArbitrageCalculator()

    def test_large_spread_detected(self):
        opp = self.calc.calc_cex_vs_dex(
            input_erg=100,
            cex_erg_usdt_price=0.31,
            dex_erg_sigusd_price=0.29,
            direction="buy_dex_sell_cex",
            slippage=0.01,
        )
        # Should detect the ~7% spread
        assert opp.profit_percent > 0

    def test_tiny_spread_not_profitable(self):
        """0.3% spread should not be profitable after fees."""
        opp = self.calc.calc_cex_vs_dex(
            input_erg=10,
            cex_erg_usdt_price=0.301,
            dex_erg_sigusd_price=0.300,
            direction="buy_dex_sell_cex",
            slippage=0.01,
        )
        assert not opp.is_profitable
