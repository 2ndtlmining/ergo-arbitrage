import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import config

logger = logging.getLogger("ergo_arb.calculator")


@dataclass
class FeeBreakdown:
    trading_fee: float = 0.0      # CEX trading fee or DEX swap fee (in USD/SigUSD)
    withdraw_fee_erg: float = 0.0  # CEX withdrawal fee in ERG
    network_fee_erg: float = 0.0   # Ergo tx fee in ERG
    protocol_fee: float = 0.0     # SigmaUSD bank fee (in USD)
    execution_fee_erg: float = 0.0  # DEX batcher fee in ERG
    slippage_cost: float = 0.0    # Estimated slippage cost (in ERG)

    @property
    def total_fee_erg(self) -> float:
        return self.withdraw_fee_erg + self.network_fee_erg + self.execution_fee_erg + self.slippage_cost

    @property
    def total_fee_usd(self) -> float:
        return self.trading_fee + self.protocol_fee


@dataclass
class ArbitrageOpportunity:
    path: str  # e.g. "ERG -> SigUSD (Bank) -> ERG (Spectrum)"
    input_erg: float
    output_erg: float
    profit_erg: float
    profit_percent: float
    fees: FeeBreakdown
    source_price: float  # price at source (ERG/USD or ERG/SigUSD)
    target_price: float  # price at target
    source_exchange: str
    target_exchange: str
    is_profitable: bool
    blocked: bool = False  # True if a step in the path is blocked
    blocked_reason: str = ""  # e.g. "Bank mint blocked (RR=261%, need >400%)"
    assumption: str = ""  # e.g. "Assumes SigUSD = USDT"
    requires_transfer: bool = False  # True if funds must move between venues
    steps: list = field(default_factory=list)  # Step-by-step execution instructions
    estimated_execution_minutes: float = 0  # How long the full path takes
    price_risk_percent: float = 0  # Estimated price risk during execution
    profit_usd: float = 0  # Profit in USD terms
    details: dict = field(default_factory=dict)  # Exact leg amounts (e.g. sigusd_cents, bank_erg)
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def path_key(self) -> str:
        """Path name without the trade size suffix, e.g. "Bank mint->Spectrum sell"."""
        return self.path.rsplit(" [", 1)[0] if " [" in self.path else self.path

    @property
    def net_profit_erg(self) -> float:
        return self.output_erg - self.input_erg

    @property
    def risk_adjusted_profitable(self) -> bool:
        """Is it profitable even accounting for price movement risk?"""
        return self.profit_percent > (config.MIN_PROFIT_PERCENT + self.price_risk_percent)


# Estimated execution times for different paths (minutes)
EXECUTION_TIMES = {
    "dex_only": 5,           # On-chain swap via batcher
    "bank_only": 10,         # SigmaUSD bank transaction
    "bank_to_dex": 15,       # Bank mint + DEX swap
    "dex_to_bank": 15,       # DEX swap + bank redeem
    "cex_withdraw": 40,      # NonKYC: 20 confirms * ~2min
    "kucoin_withdraw": 60,   # Kucoin: 30 confirms * ~2min
    "cex_to_cex": 90,        # Sell + transfer USDT + buy + withdraw ERG
    "cex_to_dex": 50,        # CEX withdraw ERG + DEX swap
}

# Price risk per minute (estimated % price can move)
PRICE_RISK_PER_MINUTE = 0.02  # 0.02% per minute (~1.2% per hour)


class ArbitrageCalculator:
    def calc_bank_to_dex(
        self,
        input_erg: float,
        bank_erg_to_sigusd_rate: float,  # SigUSD per ERG from bank (after bank fees)
        dex_sigusd_to_erg_output: float,  # ERG output from DEX for the SigUSD amount
        dex_execution_fee: float = config.SPECTRUM_EXECUTION_FEE,
        slippage: float = 0.0,
    ) -> ArbitrageOpportunity:
        """
        Path: ERG -> SigUSD (Bank mint) -> ERG (DEX swap)
        1. Send ERG to SigmaUSD bank, receive SigUSD (minus 2.1% fee)
        2. Swap SigUSD -> ERG on Spectrum/Mew
        """
        fees = FeeBreakdown()

        # Step 1: Mint SigUSD at bank
        sigusd_received = input_erg * bank_erg_to_sigusd_rate
        # Bank fees already included in bank_erg_to_sigusd_rate
        combined_fee_factor = (1 - config.SIGMAUSD_PROTOCOL_FEE) * (1 - config.SIGMAUSD_FRONTEND_FEE)
        fees.protocol_fee = input_erg * bank_erg_to_sigusd_rate / combined_fee_factor * (1 - combined_fee_factor)
        fees.network_fee_erg = config.ERGO_TX_FEE  # tx to mint

        # Step 2: Swap SigUSD -> ERG on DEX
        erg_from_dex = dex_sigusd_to_erg_output
        fees.trading_fee = sigusd_received * config.SPECTRUM_POOL_FEE  # DEX fee in SigUSD terms
        fees.execution_fee_erg = dex_execution_fee
        fees.network_fee_erg += config.ERGO_TX_FEE  # tx for swap
        fees.slippage_cost = erg_from_dex * slippage

        output_erg = erg_from_dex - fees.execution_fee_erg - fees.network_fee_erg - fees.slippage_cost
        profit_erg = output_erg - input_erg
        profit_percent = (profit_erg / input_erg) * 100 if input_erg > 0 else 0

        exec_time = EXECUTION_TIMES["bank_to_dex"]
        price_risk = exec_time * PRICE_RISK_PER_MINUTE

        return ArbitrageOpportunity(
            path=f"ERG -> SigUSD (Bank) -> ERG (DEX)",
            input_erg=input_erg,
            output_erg=output_erg,
            profit_erg=profit_erg,
            profit_percent=profit_percent,
            fees=fees,
            source_price=bank_erg_to_sigusd_rate,
            target_price=sigusd_received / erg_from_dex if erg_from_dex > 0 else 0,
            source_exchange="SigmaUSD Bank",
            target_exchange="Spectrum DEX",
            is_profitable=profit_percent > config.MIN_PROFIT_PERCENT,
            requires_transfer=False,  # all on-chain
            estimated_execution_minutes=exec_time,
            price_risk_percent=price_risk,
            profit_usd=profit_erg * bank_erg_to_sigusd_rate / combined_fee_factor if bank_erg_to_sigusd_rate > 0 else 0,
        )

    def calc_dex_to_bank(
        self,
        input_erg: float,
        dex_erg_to_sigusd_output: float,  # SigUSD received from DEX
        bank_sigusd_to_erg_rate: float,  # ERG per SigUSD from bank (after bank fees)
        dex_execution_fee: float = config.SPECTRUM_EXECUTION_FEE,
        slippage: float = 0.0,
    ) -> ArbitrageOpportunity:
        """
        Path: ERG -> SigUSD (DEX swap) -> ERG (Bank redeem)
        1. Swap ERG -> SigUSD on Spectrum/Mew
        2. Redeem SigUSD at SigmaUSD bank for ERG
        """
        fees = FeeBreakdown()

        # Step 1: Swap ERG -> SigUSD on DEX
        sigusd_received = dex_erg_to_sigusd_output
        fees.trading_fee = input_erg * config.SPECTRUM_POOL_FEE  # in ERG terms
        fees.execution_fee_erg = dex_execution_fee
        fees.network_fee_erg = config.ERGO_TX_FEE  # tx for swap

        # Step 2: Redeem SigUSD at bank
        erg_from_bank = sigusd_received * bank_sigusd_to_erg_rate
        fees.protocol_fee = sigusd_received * (1 - (1 - config.SIGMAUSD_PROTOCOL_FEE) * (1 - config.SIGMAUSD_FRONTEND_FEE))
        fees.network_fee_erg += config.SIGMAUSD_REDEEM_EXTRA_ERG  # receipt box + miner fee
        # Bank leg is oracle-priced; the buffer covers the DEX leg moving before inclusion
        fees.slippage_cost = erg_from_bank * slippage

        output_erg = erg_from_bank - fees.execution_fee_erg - fees.network_fee_erg - fees.slippage_cost
        profit_erg = output_erg - input_erg
        profit_percent = (profit_erg / input_erg) * 100 if input_erg > 0 else 0

        exec_time = EXECUTION_TIMES["dex_to_bank"]
        price_risk = exec_time * PRICE_RISK_PER_MINUTE

        return ArbitrageOpportunity(
            path=f"ERG -> SigUSD (DEX) -> ERG (Bank)",
            input_erg=input_erg,
            output_erg=output_erg,
            profit_erg=profit_erg,
            profit_percent=profit_percent,
            fees=fees,
            source_price=sigusd_received / input_erg if input_erg > 0 else 0,
            target_price=1.0 / bank_sigusd_to_erg_rate if bank_sigusd_to_erg_rate > 0 else 0,
            source_exchange="Spectrum DEX",
            target_exchange="SigmaUSD Bank",
            is_profitable=profit_percent > config.MIN_PROFIT_PERCENT,
            estimated_execution_minutes=exec_time,
            price_risk_percent=price_risk,
            profit_usd=profit_erg * (sigusd_received / input_erg) if input_erg > 0 else 0,
        )

    def calc_cex_to_cex(
        self,
        input_erg: float,
        sell_price_usdt: float,  # ERG/USDT price on sell exchange
        buy_price_usdt: float,  # ERG/USDT price on buy exchange
        sell_trading_fee: float = config.NONKYC_TRADING_FEE,
        buy_trading_fee: float = config.NONKYC_TRADING_FEE,
        usdt_transfer_fee: float = 0.0,  # USDT network fee to move between exchanges
        erg_withdraw_fee: float = config.NONKYC_ERG_WITHDRAW_FEE,
    ) -> ArbitrageOpportunity:
        """
        Path: Sell ERG on Exchange A -> Transfer USDT -> Buy ERG on Exchange B -> Withdraw ERG
        """
        fees = FeeBreakdown()

        # Step 1: Sell ERG for USDT
        usdt_gross = input_erg * sell_price_usdt
        sell_fee = usdt_gross * sell_trading_fee
        usdt_after_sell = usdt_gross - sell_fee
        fees.trading_fee = sell_fee

        # Step 2: Transfer USDT (if cross-exchange)
        usdt_after_transfer = usdt_after_sell - usdt_transfer_fee

        # Step 3: Buy ERG with USDT
        erg_gross = usdt_after_transfer / buy_price_usdt
        buy_fee_usdt = usdt_after_transfer * buy_trading_fee
        erg_bought = (usdt_after_transfer - buy_fee_usdt) / buy_price_usdt
        fees.trading_fee += buy_fee_usdt

        # Step 4: Withdraw ERG
        erg_received = erg_bought - erg_withdraw_fee
        fees.withdraw_fee_erg = erg_withdraw_fee

        profit_erg = erg_received - input_erg
        profit_percent = (profit_erg / input_erg) * 100 if input_erg > 0 else 0

        exec_time = EXECUTION_TIMES["cex_to_cex"]
        price_risk = exec_time * PRICE_RISK_PER_MINUTE

        return ArbitrageOpportunity(
            path=f"Sell ERG -> USDT -> Buy ERG",
            input_erg=input_erg,
            output_erg=erg_received,
            profit_erg=profit_erg,
            profit_percent=profit_percent,
            fees=fees,
            source_price=sell_price_usdt,
            target_price=buy_price_usdt,
            source_exchange="CEX (sell)",
            target_exchange="CEX (buy)",
            is_profitable=profit_percent > config.MIN_PROFIT_PERCENT,
            requires_transfer=True,
            estimated_execution_minutes=exec_time,
            price_risk_percent=price_risk,
            profit_usd=profit_erg * sell_price_usdt,
        )

    def calc_cex_vs_dex(
        self,
        input_erg: float,
        cex_erg_usdt_price: float,  # ERG/USDT on CEX
        dex_erg_sigusd_price: float,  # ERG/SigUSD on DEX (assuming SigUSD ≈ USDT)
        direction: str = "buy_cex_sell_dex",  # or "buy_dex_sell_cex"
        cex_trading_fee: float = config.NONKYC_TRADING_FEE,
        erg_withdraw_fee: float = config.NONKYC_ERG_WITHDRAW_FEE,
        dex_execution_fee: float = config.SPECTRUM_EXECUTION_FEE,
        slippage: float = 0.0,
    ) -> ArbitrageOpportunity:
        """
        Compare CEX ERG/USDT vs DEX ERG/SigUSD prices.
        Assumes SigUSD ≈ 1 USDT for comparison purposes.
        """
        fees = FeeBreakdown()

        if direction == "buy_cex_sell_dex":
            # Buy cheap ERG on CEX, sell expensive on DEX
            # This means CEX price < DEX price
            usdt_spent = input_erg * cex_erg_usdt_price
            fees.trading_fee = usdt_spent * cex_trading_fee
            fees.withdraw_fee_erg = erg_withdraw_fee

            erg_on_node = input_erg - erg_withdraw_fee  # after CEX withdrawal
            # Swap ERG -> SigUSD on DEX
            sigusd_received = erg_on_node * dex_erg_sigusd_price * (1 - config.SPECTRUM_POOL_FEE)
            fees.execution_fee_erg = dex_execution_fee
            fees.network_fee_erg = config.ERGO_TX_FEE
            fees.slippage_cost = erg_on_node * slippage

            # We end up with SigUSD, need to convert back to ERG value for comparison
            erg_equivalent = sigusd_received / cex_erg_usdt_price  # value in ERG at CEX rate
            output_erg = erg_equivalent
            path = f"Buy ERG (NonKYC ${cex_erg_usdt_price:.4f}) -> SigUSD (DEX ${dex_erg_sigusd_price:.4f})"
        else:
            # Buy ERG cheap on DEX (swap SigUSD->ERG), sell expensive on CEX
            # This path requires having SigUSD already or buying it first
            path = f"Buy ERG (DEX ${dex_erg_sigusd_price:.4f}) -> Sell (NonKYC ${cex_erg_usdt_price:.4f})"
            # For now, just compare the price differential
            price_diff = cex_erg_usdt_price - dex_erg_sigusd_price
            output_erg = input_erg * (1 + price_diff / dex_erg_sigusd_price)
            fees.trading_fee = input_erg * cex_erg_usdt_price * cex_trading_fee
            fees.execution_fee_erg = dex_execution_fee
            fees.network_fee_erg = config.ERGO_TX_FEE

        profit_erg = output_erg - input_erg - fees.total_fee_erg
        profit_percent = (profit_erg / input_erg) * 100 if input_erg > 0 else 0

        exec_time = EXECUTION_TIMES["cex_to_dex"]
        price_risk = exec_time * PRICE_RISK_PER_MINUTE

        return ArbitrageOpportunity(
            path=path,
            input_erg=input_erg,
            output_erg=output_erg,
            profit_erg=profit_erg,
            profit_percent=profit_percent,
            fees=fees,
            source_price=cex_erg_usdt_price if direction == "buy_cex_sell_dex" else dex_erg_sigusd_price,
            target_price=dex_erg_sigusd_price if direction == "buy_cex_sell_dex" else cex_erg_usdt_price,
            source_exchange="CEX" if direction == "buy_cex_sell_dex" else "Spectrum DEX",
            target_exchange="Spectrum DEX" if direction == "buy_cex_sell_dex" else "CEX",
            is_profitable=profit_percent > config.MIN_PROFIT_PERCENT,
            requires_transfer=True,
            estimated_execution_minutes=exec_time,
            price_risk_percent=price_risk,
            profit_usd=profit_erg * cex_erg_usdt_price,
        )
