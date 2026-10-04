import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import config
from exchanges.sigmausd import BankState, affordable_mint_cents, can_mint_sigusd

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
    path: str  # e.g. "Bank mint->ErgoDEX sell [10 ERG]"
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
    sources: tuple = ()  # price sources this path depends on (staleness guard): spectrum, bank, kucoin, ...
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def path_key(self) -> str:
        """Path name without the trade size suffix, e.g. "Bank mint->ErgoDEX sell"."""
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

# Paths that end (or start) in SigUSD on one side and USDT on the other cannot be executed: there is
# no SigUSD<->USDT venue in this codebase. Their figures are shown for comparison only.
WATCH_ONLY_REASON = "watch-only: no SigUSD<->USDT venue"


def sigusd_redeem_value_usd() -> float:
    """USD one SigUSD is worth when redeemed at the bank (the exit the bot can actually execute)."""
    return (1 - config.SIGMAUSD_PROTOCOL_FEE) * (1 - config.SIGMAUSD_FRONTEND_FEE)


def _sigusd_assumption(sigusd_usd: float) -> str:
    return f"SigUSD valued at its bank redeem value (${sigusd_usd:.3f})"


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
        2. Swap SigUSD -> ERG on the ErgoDEX pool/Mew
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
            path="ERG -> SigUSD (Bank) -> ERG (DEX)",
            input_erg=input_erg,
            output_erg=output_erg,
            profit_erg=profit_erg,
            profit_percent=profit_percent,
            fees=fees,
            source_price=bank_erg_to_sigusd_rate,
            target_price=sigusd_received / erg_from_dex if erg_from_dex > 0 else 0,
            source_exchange="SigmaUSD Bank",
            target_exchange="ErgoDEX pool",
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
        1. Swap ERG -> SigUSD on the ErgoDEX pool/Mew
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
            path="ERG -> SigUSD (DEX) -> ERG (Bank)",
            input_erg=input_erg,
            output_erg=output_erg,
            profit_erg=profit_erg,
            profit_percent=profit_percent,
            fees=fees,
            source_price=sigusd_received / input_erg if input_erg > 0 else 0,
            target_price=1.0 / bank_sigusd_to_erg_rate if bank_sigusd_to_erg_rate > 0 else 0,
            source_exchange="ErgoDEX pool",
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
            path="Sell ERG -> USDT -> Buy ERG",
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
        cex_buy_price: float,           # USDT per ERG you pay on the CEX (ask side, for this size)
        cex_sell_price: float,          # USDT per ERG you get on the CEX (bid side, for this size)
        dex_erg_sigusd_price: float,    # SigUSD per ERG on the pool (spot)
        direction: str = "buy_cex_sell_dex",  # or "buy_dex_sell_cex"
        cex_trading_fee: float = config.NONKYC_TRADING_FEE,
        erg_withdraw_fee: float = config.NONKYC_ERG_WITHDRAW_FEE,
        dex_execution_fee: Optional[float] = None,   # None: the configured pool route's service fee
        slippage: float = 0.0,          # extra fraction of the DEX leg, when no pool reserves are known
        pool=None,                      # PoolState (x = ERG, y = SigUSD): exact AMM output when given
        sigusd_usd: Optional[float] = None,          # USD per SigUSD; default: its bank redeem value
    ) -> ArbitrageOpportunity:
        """CEX ERG/USDT against the ERG/SigUSD pool, as two legs in real units.

        buy_cex_sell_dex: USDT -> ERG on the CEX (ask, taker fee) -> withdraw (fee once) -> ERG -> SigUSD
        on the pool. buy_dex_sell_cex: SigUSD -> ERG on the pool -> deposit -> ERG -> USDT on the CEX
        (bid, taker fee). The SigUSD side is valued at `sigusd_usd`; profit is the USD difference,
        expressed in ERG at the CEX price. Watch-only: nothing converts SigUSD and USDT.
        """
        exec_fee = config.pool_service_fee() if dex_execution_fee is None else dex_execution_fee
        sigusd_usd = sigusd_redeem_value_usd() if sigusd_usd is None else sigusd_usd
        fees = FeeBreakdown(execution_fee_erg=exec_fee)

        if direction == "buy_cex_sell_dex":
            usd_in = input_erg * cex_buy_price
            fees.trading_fee = usd_in * cex_trading_fee
            erg_bought = input_erg * (1 - cex_trading_fee)
            fees.withdraw_fee_erg = erg_withdraw_fee
            fees.network_fee_erg = config.ERGO_TX_FEE
            erg_in = erg_bought - erg_withdraw_fee - exec_fee - config.ERGO_TX_FEE
            fees.slippage_cost = 0.0 if pool else erg_in * slippage
            erg_in -= fees.slippage_cost
            sigusd_out = (pool.swap_output(erg_in, True) if pool
                          else erg_in * dex_erg_sigusd_price * (1 - config.SPECTRUM_POOL_FEE))
            usd_out = max(sigusd_out, 0.0) * sigusd_usd
            ref_price = cex_buy_price
            path = f"Buy ERG (CEX ask ${cex_buy_price:.4f}) -> SigUSD (DEX {dex_erg_sigusd_price:.4f})"
        else:
            sigusd_in = input_erg * dex_erg_sigusd_price
            usd_in = sigusd_in * sigusd_usd
            erg = (pool.swap_output(sigusd_in, False) if pool
                   else sigusd_in / dex_erg_sigusd_price * (1 - config.SPECTRUM_POOL_FEE))
            fees.network_fee_erg = 2 * config.ERGO_TX_FEE           # the swap, then the deposit
            fees.slippage_cost = 0.0 if pool else erg * slippage
            erg -= exec_fee + fees.network_fee_erg + fees.slippage_cost
            usd_out = max(erg, 0.0) * cex_sell_price * (1 - cex_trading_fee)
            fees.trading_fee = max(erg, 0.0) * cex_sell_price * cex_trading_fee
            ref_price = cex_sell_price
            path = f"Buy ERG (DEX {dex_erg_sigusd_price:.4f}) -> Sell (CEX bid ${cex_sell_price:.4f})"

        profit_usd = usd_out - usd_in
        profit_erg = profit_usd / ref_price if ref_price else 0.0
        profit_percent = (profit_erg / input_erg) * 100 if input_erg > 0 else 0

        exec_time = EXECUTION_TIMES["cex_to_dex"]
        price_risk = exec_time * PRICE_RISK_PER_MINUTE

        return ArbitrageOpportunity(
            path=path,
            input_erg=input_erg,
            output_erg=input_erg + profit_erg,
            profit_erg=profit_erg,
            profit_percent=profit_percent,
            fees=fees,
            source_price=cex_buy_price if direction == "buy_cex_sell_dex" else dex_erg_sigusd_price,
            target_price=dex_erg_sigusd_price if direction == "buy_cex_sell_dex" else cex_sell_price,
            source_exchange="CEX" if direction == "buy_cex_sell_dex" else "ErgoDEX pool",
            target_exchange="ErgoDEX pool" if direction == "buy_cex_sell_dex" else "CEX",
            is_profitable=profit_percent > config.MIN_PROFIT_PERCENT,
            blocked=True,
            blocked_reason=WATCH_ONLY_REASON,
            assumption=_sigusd_assumption(sigusd_usd),
            requires_transfer=True,
            estimated_execution_minutes=exec_time,
            price_risk_percent=price_risk,
            profit_usd=profit_usd,
        )

    def calc_bank_to_cex(
        self,
        input_erg: float,
        bank_state: BankState,
        cex_buy_price: float,           # USDT per ERG you pay on the CEX (ask side, for this size)
        cex_trading_fee: float,
        erg_withdraw_fee: float,
        sigusd_usd: Optional[float] = None,          # USD per SigUSD when sold; default: bank redeem value
        execution_minutes: float = 60,
    ) -> ArbitrageOpportunity:
        """Mint SigUSD at the bank -> (sell SigUSD for USDT: no venue) -> buy ERG on the CEX -> withdraw.

        The mint is contract-exact (fees included). ERG out per ERG in is the bank's USD per ERG over the
        CEX ask, so it pays only when the bank values ERG above the CEX price. Watch-only.
        """
        sigusd_usd = sigusd_redeem_value_usd() if sigusd_usd is None else sigusd_usd
        budget = int(max(input_erg - config.ERGO_TX_FEE, 0) * 1e9)
        cents = affordable_mint_cents(bank_state, budget)
        usd = cents / 100 * sigusd_usd
        erg_bought = usd / cex_buy_price * (1 - cex_trading_fee) if cex_buy_price else 0.0
        output_erg = erg_bought - erg_withdraw_fee
        profit_erg = output_erg - input_erg
        profit_percent = (profit_erg / input_erg) * 100 if input_erg > 0 else 0
        reasons = [WATCH_ONLY_REASON]
        if not can_mint_sigusd(bank_state, max(cents, 1)):
            reasons.append(f"Bank mint blocked (RR={bank_state.reserve_ratio:.0f}%, post-mint RR must stay >=400%)")
        fees = FeeBreakdown(trading_fee=usd * cex_trading_fee, withdraw_fee_erg=erg_withdraw_fee,
                            network_fee_erg=config.ERGO_TX_FEE,
                            protocol_fee=input_erg * bank_state.oracle_usd_per_erg - cents / 100)
        return ArbitrageOpportunity(
            path="Bank mint -> CEX buy",
            input_erg=input_erg,
            output_erg=output_erg,
            profit_erg=profit_erg,
            profit_percent=profit_percent,
            fees=fees,
            source_price=bank_state.oracle_usd_per_erg,
            target_price=cex_buy_price,
            source_exchange="SigmaUSD Bank",
            target_exchange="CEX",
            is_profitable=profit_percent > config.MIN_PROFIT_PERCENT,
            blocked=True,
            blocked_reason="; ".join(reasons),
            assumption=_sigusd_assumption(sigusd_usd),
            requires_transfer=True,
            estimated_execution_minutes=execution_minutes,
            price_risk_percent=execution_minutes * PRICE_RISK_PER_MINUTE,
            profit_usd=profit_erg * cex_buy_price,
            details={"sigusd_cents": cents},
        )
