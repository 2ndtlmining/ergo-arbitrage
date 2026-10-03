import asyncio
import json
import re
import logging
import os
import signal
import sys
import time
from datetime import datetime
from typing import Optional

import aiohttp

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

import config
from exchanges.nonkyc import NonKYCExchange
from exchanges.kucoin import KucoinExchange
from exchanges.spectrum import SpectrumDEX
from exchanges.sigmausd import (
    SigmaUSDBank,
    BankState,
    affordable_mint_cents,
    can_mint_sigusd,
    quote_redeem_sigusd,
)
from exchanges.ergo_node import ErgoNodeClient
from exchanges.dexy import fetch_use_lp, mint_available, mint_box_state, quote_dexy_mint
from ergo.arb_runner import ArbResult, run_arb
from ergo.chain_state import ChainSnapshot, prices_from_snapshot, read_snapshot
from arbitrage.optimizer import maximize
from arbitrage.sizing import Market, SizeChoice, best_size
from arbitrage.dashboard_state import PATH_LABELS, DashboardState
from arbitrage.venues import VenueContext, describe_all
from arbitrage.calculator import (
    ArbitrageCalculator,
    ArbitrageOpportunity,
    FeeBreakdown,
    EXECUTION_TIMES,
    PRICE_RISK_PER_MINUTE,
)
from tracker.profit_tracker import ProfitTracker
from notifications.discord import DiscordNotifier
from exchanges.cex_public import CexQuote, FeeBook, best_spread, fetch_all_quotes
from notifications import embeds
from notifications.digest import build_digest, digest_due, mark_digest_sent
from notifications.episodes import EpisodeTracker
from notifications.health import HealthMonitor
from notifications.mint_gate import MintGateWatcher, mint_gate_text
from logging_config import console

MINT_MESSAGE_KEY = "mint_gate_message"   # tracker meta: the open mint message, for after a crash
CEX_FEE_REFRESH_SECONDS = 3600            # how often published CEX fees are re-read
CEX_STALE_SECONDS = 60                    # a CEX snapshot older than this is shown as stale, not used
CEX_FIRST_WAIT_SECONDS = 10               # the first scan waits this long for the first CEX snapshot
EPISODE_HEARTBEAT_S = 60                 # how often an open episode's last-seen time is saved
DIGEST_RETRY_S = 600                     # a digest not confirmed by Discord is queued again after this

logger = logging.getLogger("ergo_arb.scanner")

LIVE_PATH = "Spectrum buy->Bank redeem"
# Paths --live can execute (ergo/arb_runner.py), scanner path key -> runner path
LIVE_PATHS = {"Spectrum buy->Bank redeem": "redeem", "Bank mint->Spectrum sell": "mint"}

# Minimum balance per asset before the wallet analysis is worth showing
WALLET_MINIMUMS = {"erg": (2, "ERG", ".4f"), "sigusd": (0.5, "SigUSD", ".2f"), "use": (0.01, "USE", ".3f")}


class ArbitrageScanner:
    def __init__(self, mode: str = "monitor", db_path: str = "arbitrage_tracker.db",
                 enable_cex: Optional[bool] = None, enable_use: Optional[bool] = None,
                 cex_watch: Optional[bool] = None, view: str = "plain", show_wallet: bool = True):
        """
        mode: "monitor" (console only), "notify" (console + Discord), "live" (console + Discord + execute)
        """
        self.mode = mode
        self.view = view                    # plain | dashboard | json
        self.show_wallet = show_wallet
        self.out = console if view == "plain" else Console(quiet=True)  # all scan printing goes here
        self.state = DashboardState(mode)
        self.episodes = EpisodeTracker(config.DISCORD_CONFIRM_SECONDS, config.DISCORD_CLOSE_SECONDS,
                                       config.DISCORD_EDIT_SECONDS, config.DISCORD_MIN_PROFIT_PERCENT,
                                       config.DISCORD_MIN_PROFIT_ERG)
        self.health = HealthMonitor(config.DISCORD_HEALTH_CHAIN_SECONDS, config.DISCORD_HEALTH_VENUE_SECONDS,
                                    config.DISCORD_HEALTH_ORACLE_SECONDS, config.DISCORD_HEALTH_REPEAT_SECONDS,
                                    started_at=time.time())
        self._episode_touched: dict[int, float] = {}   # episode db id -> poll time of the last heartbeat
        self._digest_queued_at: Optional[float] = None
        self.mint_gate = MintGateWatcher(config.MINT_GATE_CONFIRM_POLLS, config.MINT_GATE_MIN_ROOM_ERG,
                                         config.MINT_GATE_PING_COOLDOWN_SECONDS, config.MINT_GATE_CLOSE_SECONDS)
        self._last_wallet: Optional[dict] = None
        self._last_prices: dict = {}
        self._now = 0.0
        # On-chain only by default: CEX paths are parked until issues #2/#3 are fixed
        self.enable_cex = config.ENABLE_CEX if enable_cex is None else enable_cex
        self.enable_use = config.ENABLE_USE if enable_use is None else enable_use
        # Watch-only CEX prices; redundant when CEX trading paths are enabled
        self.cex_watch = (config.CEX_WATCH if cex_watch is None else cex_watch) and not self.enable_cex
        self.nonkyc = NonKYCExchange()
        self.kucoin = KucoinExchange()
        self.spectrum = SpectrumDEX()
        self.sigmausd = SigmaUSDBank()
        self.ergo_node = ErgoNodeClient()
        self.calculator = ArbitrageCalculator()
        self.tracker = ProfitTracker(db_path)
        self.discord = DiscordNotifier()
        self._http: Optional[aiohttp.ClientSession] = None  # shared: Crux API + explorer fallback + CEX data
        self.cex_fees = FeeBook()                 # per-exchange fees: defaults, then what each exchange publishes
        self._cex_quotes: dict = {}               # latest CEX books, filled by the background feed
        self._cex_ready: Optional[asyncio.Event] = None
        self._cex_task: Optional[asyncio.Task] = None
        self._snapshot: Optional[ChainSnapshot] = None      # last good node snapshot
        self._chain_prices: Optional[dict] = None           # its price entries
        self._last_blocked: Optional[tuple] = None          # last printed live blocker lines
        self._last_blocked_logged: Optional[tuple] = None   # their shape, last written to the log
        self._health_blocker: Optional[str] = None          # node health from the last live gate check
        self._chain_error: Optional[str] = None             # set while chain reads fail
        self._last_full_scan = float("-inf")
        self._last_key: Optional[tuple] = None
        self.scan_count = 0
        self._last_snapshot_id = None
        self._trade_sizes = list(config.TRADE_SIZES)

        # Notification anti-spam state
        self._opportunity_streak: dict[str, int] = {}
        self._last_wallet_analysis_time: float = 0.0
        self._last_summary_time: float = 0.0
        self._price_timestamps: dict[str, float] = {}
        self._nonkyc_usdt_fee: float = 0.0
        self._stop = asyncio.Event()
        self.node_health: dict = {}
        self.last_optima: dict[str, ArbitrageOpportunity] = {}
        self.last_sizing: dict[str, SizeChoice] = {}  # contract-exact best size per LIVE_PATHS key
        # --live state
        self._live_streak: dict[str, int] = {}
        self._live_paused: Optional[str] = None
        self._last_trade_time = 0.0
        self._trades_today: tuple[str, int] = ("", 0)
        self._live_start_value: Optional[float] = None
        self._kucoin_usdt_fee: float = 1.0

    @property
    def discord_enabled(self) -> bool:
        return self.mode in ("notify", "live") and config.DISCORD_ENABLED

    @property
    def trading_enabled(self) -> bool:
        return self.mode == "live"

    async def connect_all(self):
        venues = [self.ergo_node.connect()]  # pool/bank/oracle come from the node (ergo/chain_state.py)
        if self.enable_cex or self.cex_watch:
            venues += [self.nonkyc.connect(), self.kucoin.connect()]
        await asyncio.gather(*venues)
        self._http = aiohttp.ClientSession()
        if self.enable_cex or self.cex_watch:
            self._start_cex_feed()

        self.node_health = await self.ergo_node.get_health()
        if not self.node_health["reachable"]:
            logger.warning("Ergo node not reachable - continuing without node features")
        elif not self.node_health["synced"]:
            logger.warning(
                f"Ergo node not synced (height {self.node_health['height']}, headers {self.node_health['headers']})"
            )
        if self.trading_enabled and not self.node_health["ok_to_trade"]:
            logger.warning("LIVE mode: trading stays disabled until the node is synced and the wallet is unlocked")

        if not self.enable_cex:
            logger.info("CEX venues disabled (on-chain only). Set ENABLE_CEX=true to enable.")
            return

        nonkyc_fee, kucoin_fee, nonkyc_usdt_fee, kucoin_usdt_fee = await asyncio.gather(
            self.nonkyc.get_withdraw_fee("ERG"),
            self.kucoin.get_withdraw_fee("ERG"),
            self.nonkyc.get_withdraw_fee("USDT"),
            self.kucoin.get_withdraw_fee("USDT"),
        )
        self._nonkyc_usdt_fee = nonkyc_usdt_fee
        self._kucoin_usdt_fee = kucoin_usdt_fee
        logger.info(f"NonKYC ERG withdrawal fee: {nonkyc_fee} ERG")
        logger.info(f"Kucoin ERG withdrawal fee: {kucoin_fee} ERG")
        logger.info(f"NonKYC USDT withdrawal fee: {nonkyc_usdt_fee} USDT")
        logger.info(f"Kucoin USDT withdrawal fee: {kucoin_usdt_fee} USDT")

    async def disconnect_all(self):
        await self._stop_cex_feed()
        if self._http:
            await self._http.close()
            self._http = None
        venues = []
        if self.enable_cex or self.cex_watch:
            venues += [self.nonkyc.disconnect(), self.kucoin.disconnect()]
        await asyncio.gather(
            *venues,
            self.ergo_node.disconnect(),
            self.discord.disconnect(),
        )
        self.tracker.close()

    async def _fetch_use_mint_status(self) -> Optional[dict]:
        """Check if USE free_mint or arb_mint is available."""
        if not self._http:
            return None
        async def one(mint_type: str):
            try:
                async with self._http.get(
                    f"{config.CRUX_API_URL}/dexy/mint_status/use?mint_type={mint_type}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 200:
                        return mint_type, await r.json()
            except Exception as e:
                logger.error(f"Crux {mint_type} status error: {e}")
            return mint_type, None

        pairs = await asyncio.gather(one("free_mint"), one("arb_mint"))
        results = {k: v for k, v in pairs if v is not None}
        return results if results else None

    async def _fetch_use_lp(self):
        return await fetch_use_lp(self._http) if self._http else None

    async def _read_chain(self) -> Optional[ChainSnapshot]:
        """Read pool/bank/oracle from the node. On failure keep the last good snapshot
        for display, record the error (which blocks trading) and log once per outage."""
        try:
            snap = await read_snapshot(self.ergo_node.session, self._http)
            prices = prices_from_snapshot(snap)  # a snapshot only counts if it can be priced
        except Exception as e:  # anything (malformed JSON too) fails closed; CancelledError still propagates
            message = f"{e.__class__.__name__}: {e}" if str(e) else e.__class__.__name__
            if isinstance(e, (RuntimeError, asyncio.TimeoutError)):
                message = str(e) or e.__class__.__name__
            if self._chain_error is None:
                logger.warning(f"Chain state unavailable: {message}")
            self._chain_error = message
            return None
        if self._chain_error is not None:
            logger.info("Chain state readable again")
        self._chain_error = None
        self._snapshot, self._chain_prices = snap, prices
        self._price_timestamps["spectrum"] = self._price_timestamps["bank"] = time.time()
        return snap

    async def fetch_all_prices(self) -> dict:
        """Fetch prices from all sources concurrently."""
        results = {}

        async def _none():
            return None

        snap, cex, use_lp, use_mint = await asyncio.gather(
            self._read_chain(),
            self._cex_data() if (self.enable_cex or self.cex_watch) else _none(),
            self._fetch_use_lp() if self.enable_use else _none(),
            self._fetch_use_mint_status() if self.enable_use else _none(),
            return_exceptions=True,
        )

        if isinstance(cex, Exception):
            logger.error(f"CEX data fetch failed: {cex}")
            cex = None
        cex = cex or {}
        live = {n: q for n, q in cex.items() if q.book is not None}

        def mid(name):
            q = live.get(name) if self.enable_cex else None
            return (q.bid + q.ask) / 2 if q else None

        nonkyc_price, kucoin_price = mid("NonKYC"), mid("Kucoin")
        if isinstance(snap, BaseException):  # _read_chain catches everything; fail closed regardless
            logger.error(f"Chain read failed: {snap}")
            self._chain_error = str(snap) or snap.__class__.__name__
        if isinstance(use_lp, Exception):
            logger.error(f"USE LP fetch failed: {use_lp}")
            use_lp = None
        if isinstance(use_mint, Exception):
            logger.error(f"Crux USE mint status fetch failed: {use_mint}")
            use_mint = None

        results["nonkyc_erg_usdt"] = nonkyc_price
        results["kucoin_erg_usdt"] = kucoin_price
        # Last good node snapshot (a failed read leaves it in place, marked stale by its age)
        results.update(self._chain_prices or {"spectrum_pool": None, "spectrum_erg_sigusd": None, "bank": {}})
        results["use_lp"] = use_lp
        results["use_mint"] = use_mint

        # Track price freshness for staleness guard
        now = time.time()
        if nonkyc_price is not None:
            self._price_timestamps["nonkyc"] = now
        if kucoin_price is not None:
            self._price_timestamps["kucoin"] = now
        if use_lp is not None:
            self._price_timestamps["use"] = now

        results["cex"] = cex                  # every exchange, with the error when it has no book
        if self.cex_watch:
            results["cex_watch"] = live       # quotes with bid/ask/timestamp, watch-only
        results["nonkyc_orderbook"] = live["NonKYC"].book if self.enable_cex and "NonKYC" in live else None
        results["kucoin_orderbook"] = live["Kucoin"].book if self.enable_cex and "Kucoin" in live else None

        self._last_prices = results
        return results

    async def _fetch_cex(self) -> dict:
        """Order books of every CEX in one go (public data, shared session); fees refreshed hourly."""
        if self._http is None:
            return {}
        if time.time() - self.cex_fees.refreshed_at > CEX_FEE_REFRESH_SECONDS:
            try:
                await self.cex_fees.refresh(self._http)
            except Exception as e:  # fees fall back to the configured defaults
                logger.warning(f"CEX fee refresh failed: {e}")
        return await fetch_all_quotes(self._http)

    def _start_cex_feed(self):
        """Refresh the CEX books in the background every SCAN_INTERVAL_SECONDS, so a slow or hanging
        exchange never holds up the 2 s chain poll or the live gate."""
        if self._cex_task is not None and not self._cex_task.done():
            return
        if self._cex_ready is None:
            self._cex_ready = asyncio.Event()
        self._cex_task = asyncio.get_running_loop().create_task(self._cex_feed_loop())

    async def _cex_feed_loop(self):
        while True:
            try:
                self._cex_quotes = await self._fetch_cex()
            except Exception as e:  # keep the last snapshot; it turns stale on its own
                logger.warning(f"CEX feed error: {e}")
            self._cex_ready.set()
            await asyncio.sleep(config.SCAN_INTERVAL_SECONDS)

    async def _stop_cex_feed(self):
        if self._cex_task is not None:
            self._cex_task.cancel()
            try:
                await self._cex_task
            except (asyncio.CancelledError, Exception):
                pass
            self._cex_task = None

    def _cex_snapshot(self) -> dict:
        """The latest CEX books; one older than CEX_STALE_SECONDS is reported as stale instead."""
        now, out = time.time(), {}
        for name, q in self._cex_quotes.items():
            age = now - q.timestamp
            if q.book is not None and age > CEX_STALE_SECONDS:
                q = CexQuote(name, None, error=f"stale ({age:.0f}s old)", timestamp=q.timestamp,
                             latency_ms=q.latency_ms)
            out[name] = q
        return out

    async def _cex_data(self) -> dict:
        """CEX books for this scan: the background feed's latest snapshot (waiting only for the very
        first one, at most CEX_FIRST_WAIT_SECONDS), or a direct fetch when no feed is running."""
        if self._cex_task is None:
            return await self._fetch_cex()
        if not self._cex_ready.is_set():
            try:
                await asyncio.wait_for(self._cex_ready.wait(), CEX_FIRST_WAIT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("CEX feed: no snapshot yet, scanning without CEX prices")
        return self._cex_snapshot()

    def _cex_spread(self, prices: dict):
        """Best buy-here-sell-there across the exchanges at the funded size, every fee counted."""
        return best_spread(prices.get("cex") or {}, self.cex_fees, self._funded_size(),
                           config.CEX_USDT_TRANSFER_FEE)

    def _analysis_sizes(self) -> list:
        """Grid sizes to price: those the wallet can fund, plus the funded size itself (TRADE_SIZES_UNFUNDED
        keeps the whole grid). Pricing 1000 ERG for a 20 ERG wallet only produced rows nobody can trade."""
        sizes = list(self._trade_sizes)
        if config.TRADE_SIZES_UNFUNDED:
            return sizes
        funded = self._funded_size()
        out = [s for s in sizes if s <= funded]
        if not out or funded > out[-1]:
            out.append(funded)
        return out

    def _funded_size(self) -> float:
        """The largest trade the wallet could fund now (MAX_TRADE_SIZE_ERG when the wallet is unknown)."""
        cap = config.MAX_TRADE_SIZE_ERG
        if self._last_wallet is not None and self._last_wallet.get("ok", True):
            cap = min(cap, self._last_wallet.get("erg", 0) - config.LIVE_ERG_RESERVE)
        return max(round(cap, 2), config.MIN_TRADE_SIZE_ERG)

    @staticmethod
    def _spread_text(s) -> str:
        return (f"buy on {s.buy} (ask ${s.buy_price:.4f}), sell on {s.sell} (bid ${s.sell_price:.4f}) at "
                f"{s.size_erg:g} ERG: {s.gross_percent:+.2f}% gross, {s.net_percent:+.2f}% "
                f"({s.net_usdt:+.2f} USDT) after fees")

    def _display_prices(self, prices: dict):
        """Display current prices in a rich table."""
        table = Table(title="Current Prices", show_header=True, header_style="bold cyan")
        table.add_column("Source", style="bold")
        table.add_column("Pair")
        table.add_column("Price", justify="right")
        table.add_column("Details", style="dim")

        nonkyc = prices.get("nonkyc_erg_usdt")
        if nonkyc:
            table.add_row("NonKYC", "ERG/USDT", f"${nonkyc:.4f}", "CEX mid price")

        ob = prices.get("nonkyc_orderbook")
        if ob and ob.best_bid and ob.best_ask:
            table.add_row("", "", f"${ob.best_bid.price:.4f} / ${ob.best_ask.price:.4f}", "bid / ask")

        kucoin = prices.get("kucoin_erg_usdt")
        if kucoin:
            table.add_row("Kucoin", "ERG/USDT", f"${kucoin:.4f}", "CEX mid price")

        kob = prices.get("kucoin_orderbook")
        if kob and kob.best_bid and kob.best_ask:
            table.add_row("", "", f"${kob.best_bid.price:.4f} / ${kob.best_ask.price:.4f}", "bid / ask")

        spectrum = prices.get("spectrum_erg_sigusd")
        if spectrum:
            pool = prices.get("spectrum_pool")
            depth = f"pool spot, {pool.reserve_x:,.0f} ERG / {pool.reserve_y:,.0f} SigUSD" if pool else "pool spot"
            table.add_row("ErgoDEX pool", "ERG/SigUSD", f"{spectrum:.4f} SigUSD", depth)

        bank = prices.get("bank", {})
        oracle = bank.get("oracle_erg_usd")
        if oracle:
            table.add_row("SigmaUSD Bank", "ERG/USD", f"${oracle:.4f}", "Oracle price")
            rr = bank.get("reserve_ratio")
            mint_ok = bank.get("can_mint_sigusd", False)
            redeem_ok = bank.get("can_redeem_sigusd", False)
            status = f"RR: {rr:.0f}%" if rr else "RR: N/A"
            status += f" | Mint: {'YES' if mint_ok else 'NO'} | Redeem: {'YES' if redeem_ok else 'NO'}"
            table.add_row("", "", "", status)

        # SigUSD peg check
        if spectrum and oracle and spectrum > 0:
            implied = oracle / spectrum
            depeg = (implied - 1.0) * 100
            peg_status = f"1 SigUSD = ${implied:.2f} ({depeg:+.1f}%)"
            table.add_row("", "SigUSD Peg", "", peg_status)

        # USE (DexyUSD)
        use_lp = prices.get("use_lp")
        if use_lp:
            table.add_row(
                "Dexy USE LP", "USE/ERG", f"{use_lp.price_y_in_x:.4f} ERG",
                f"LP spot, {use_lp.reserve_x:,.3f} ERG / {use_lp.reserve_y:,.3f} USE",
            )

        use_mint = prices.get("use_mint")
        if use_mint:
            fm_ok = (use_mint.get("free_mint") or {}).get("is_available", False)
            am_ok = (use_mint.get("arb_mint") or {}).get("is_available", False)
            table.add_row("", "USE Mint", "", f"FreeMint: {'YES' if fm_ok else 'NO'} | ArbMint: {'YES' if am_ok else 'NO'}")

        self.out.print(table)

    @staticmethod
    def _dex_erg_to_sigusd(prices: dict, erg: float) -> float:
        """SigUSD out of the AMM pool for `erg` ERG (real reserves, price impact included)."""
        pool = prices.get("spectrum_pool")
        if pool is not None:
            return pool.swap_output(erg, input_is_x=True)
        spot = prices.get("spectrum_erg_sigusd") or 0
        return erg * spot * (1 - config.SPECTRUM_POOL_FEE)

    @staticmethod
    def _dex_sigusd_to_erg(prices: dict, sigusd: float) -> float:
        """ERG out of the AMM pool for `sigusd` SigUSD (real reserves, price impact included)."""
        pool = prices.get("spectrum_pool")
        if pool is not None:
            return pool.swap_output(sigusd, input_is_x=False)
        spot = prices.get("spectrum_erg_sigusd") or 0
        return sigusd / spot * (1 - config.SPECTRUM_POOL_FEE) if spot > 0 else 0

    @staticmethod
    def _bank_mint(state: Optional[BankState], erg: float) -> tuple[int, bool]:
        """(SigUSD cents minted for `erg` ERG, mint allowed by post-mint RR)."""
        if state is None:
            return 0, False
        cents = affordable_mint_cents(state, int(erg * 1e9))
        return cents, can_mint_sigusd(state, cents)

    @staticmethod
    def _bank_redeem_erg(state: Optional[BankState], sigusd: float) -> tuple[int, float]:
        """(cents redeemed, ERG paid out by the bank) for `sigusd` SigUSD.

        Excludes the receipt box + miner fee (config.SIGMAUSD_REDEEM_EXTRA_ERG).
        """
        if state is None:
            return 0, 0.0
        cents = int(sigusd * 100)
        return cents, quote_redeem_sigusd(state, cents) / 1e9

    def _amm_buffer(self, prices: dict, trade_size: float) -> float:
        return config.EXECUTION_BUFFER if prices.get("spectrum_pool") else config.get_recommended_slippage(trade_size)

    def _path_bank_mint(self, prices: dict, trade_size: float) -> Optional[ArbitrageOpportunity]:
        """Path 1: ERG -> SigUSD (bank mint) -> ERG (pool sell). Always computed, marked blocked."""
        bank = prices.get("bank", {})
        bank_state: Optional[BankState] = bank.get("state")
        spectrum_price = prices.get("spectrum_erg_sigusd")
        if not (bank_state and spectrum_price and spectrum_price > 0):
            return None
        oracle_price = bank.get("oracle_erg_usd") or bank_state.oracle_usd_per_erg
        reserve_ratio = bank.get("reserve_ratio")
        rr_str = f"{reserve_ratio:.0f}%" if reserve_ratio else "N/A"

        mint_cents, mint_ok = self._bank_mint(bank_state, trade_size)
        sigusd_from_bank = mint_cents / 100
        bank_rate_after_fees = sigusd_from_bank / trade_size
        erg_from_dex_after_fee = self._dex_sigusd_to_erg(prices, sigusd_from_bank)

        opp = self.calculator.calc_bank_to_dex(
            input_erg=trade_size,
            bank_erg_to_sigusd_rate=bank_rate_after_fees,
            dex_sigusd_to_erg_output=erg_from_dex_after_fee,
            dex_execution_fee=config.pool_service_fee(),
            slippage=self._amm_buffer(prices, trade_size),
        )
        opp.path = f"Bank mint->Spectrum sell [{trade_size:g} ERG]"
        fee_pct = (config.SIGMAUSD_PROTOCOL_FEE + config.SIGMAUSD_FRONTEND_FEE) * 100
        opp.steps = [
            f"START: Have {trade_size:g} ERG in wallet",
            f"Send {trade_size:g} ERG to SigmaUSD Bank to mint SigUSD",
            f"Receive ~{sigusd_from_bank:.2f} SigUSD (oracle ${oracle_price:.4f}, -{fee_pct:.2f}% bank fees)",
            f"Swap {sigusd_from_bank:.2f} SigUSD -> ERG on Spectrum (-{config.SPECTRUM_POOL_FEE*100:.1f}% pool fee, {config.pool_fee_text()})",
            f"END RESULT: ~{opp.output_erg:.2f} ERG in wallet (net {opp.profit_erg:+.2f} ERG)",
        ]
        opp.details = {"sigusd_cents": mint_cents}
        if not mint_ok:
            opp.blocked = True
            opp.blocked_reason = f"Bank mint blocked (RR={rr_str}, post-mint RR would drop below 400%)"
            opp.is_profitable = False
        return opp

    def _path_pool_buy_redeem(self, prices: dict, trade_size: float) -> Optional[ArbitrageOpportunity]:
        """Path 2: ERG -> SigUSD (pool buy) -> ERG (bank redeem). Executable by execute_arb.py."""
        bank = prices.get("bank", {})
        bank_state: Optional[BankState] = bank.get("state")
        spectrum_price = prices.get("spectrum_erg_sigusd")
        if not (bank_state and spectrum_price and spectrum_price > 0):
            return None
        oracle_price = bank.get("oracle_erg_usd") or bank_state.oracle_usd_per_erg

        sigusd_from_dex = self._dex_erg_to_sigusd(prices, trade_size)
        redeem_cents, erg_from_bank = self._bank_redeem_erg(bank_state, sigusd_from_dex)
        bank_redeem_rate = erg_from_bank / sigusd_from_dex if sigusd_from_dex > 0 else 0

        opp = self.calculator.calc_dex_to_bank(
            input_erg=trade_size,
            dex_erg_to_sigusd_output=sigusd_from_dex,
            bank_sigusd_to_erg_rate=bank_redeem_rate,
            dex_execution_fee=config.pool_service_fee(),
            slippage=self._amm_buffer(prices, trade_size),
        )
        opp.path = f"Spectrum buy->Bank redeem [{trade_size:g} ERG]"
        fee_pct = (config.SIGMAUSD_PROTOCOL_FEE + config.SIGMAUSD_FRONTEND_FEE) * 100
        opp.details = {"sigusd_cents": redeem_cents, "bank_erg": erg_from_bank}
        opp.steps = [
            f"START: Have {trade_size:g} ERG in wallet",
            f"Swap {trade_size:g} ERG -> SigUSD on Spectrum (-{config.SPECTRUM_POOL_FEE*100:.1f}% pool fee, {config.pool_fee_text()})",
            f"Receive ~{sigusd_from_dex:.2f} SigUSD",
            f"Redeem {sigusd_from_dex:.2f} SigUSD at SigmaUSD Bank (oracle ${oracle_price:.4f}, -{fee_pct:.2f}% bank fees)",
            f"END RESULT: ~{opp.output_erg:.2f} ERG in wallet (net {opp.profit_erg:+.2f} ERG)",
        ]
        return opp

    def _optimize_sizes(self, prices: dict) -> dict[str, ArbitrageOpportunity]:
        """Best size per on-chain path within [MIN_TRADE_SIZE_ERG, MAX_TRADE_SIZE_ERG] (issue #10).

        With the pool's real reserves the size comes from arbitrage/sizing.py (contract-exact,
        the same numbers the runner's transactions produce) and is kept in last_sizing, which
        gates --live. Without them only an estimate is shown and nothing is tradable.
        """
        lo, hi = config.MIN_TRADE_SIZE_ERG, max(config.MAX_TRADE_SIZE_ERG, config.MIN_TRADE_SIZE_ERG)
        pool = prices.get("spectrum_pool")
        bank_state = (prices.get("bank") or {}).get("state")
        market = Market.from_pool_state(pool, bank_state) if pool is not None and bank_state else None
        self.last_sizing = {}
        optima = {}
        for path_fn, path in ((self._path_bank_mint, "mint"), (self._path_pool_buy_redeem, "redeem")):
            if path_fn(prices, lo) is None:
                continue
            if market is not None:
                choice = best_size(path, market, hi)
                size = choice.size_erg if choice.ok else (choice.max_profit_size_erg or lo)
            else:
                choice = None
                size, _ = maximize(lambda x: path_fn(prices, x).profit_erg, lo, hi)
                size = round(size, 2)
            opp = path_fn(prices, size)
            optima[opp.path_key] = opp
            if choice is not None:
                self.last_sizing[opp.path_key] = choice
        return optima

    @staticmethod
    def _cex_side_price(prices: dict, cex: str, erg: float, side: str) -> Optional[float]:
        """Average USDT per ERG for `erg` on that side of the CEX order book (buy = asks, sell = bids).
        None when there is no book or not enough depth; callers fall back to the mid price."""
        book = prices.get(f"{cex.lower()}_orderbook")
        if book is None or isinstance(book, Exception) or not hasattr(book, "effective_buy_price"):
            return None
        return book.effective_buy_price(erg) if side == "buy" else book.effective_sell_price(erg)

    def _fees_of(self, cex: str) -> tuple[float, float]:
        """(taker fee, ERG withdrawal fee) for the CEX paths; an unknown withdrawal fee counts as the default."""
        f = self.cex_fees.get(cex)
        return f.taker, f.erg_withdraw if f.erg_withdraw is not None else self.cex_fees.default(cex).erg_withdraw or 0.0

    def _find_opportunities(self, prices: dict) -> list[ArbitrageOpportunity]:
        """Analyze prices and find all arbitrage opportunities (grid sizes); best sizes go to last_optima."""
        opportunities = []
        self.last_optima = self._optimize_sizes(prices)

        nonkyc_price = prices.get("nonkyc_erg_usdt") if self.enable_cex else None
        kucoin_price = prices.get("kucoin_erg_usdt") if self.enable_cex else None
        spectrum_price = prices.get("spectrum_erg_sigusd")
        bank = prices.get("bank", {})
        bank_state: Optional[BankState] = bank.get("state")

        # USE (Dexy): LP read on-chain, mint status/state from Crux
        use_lp = prices.get("use_lp") if self.enable_use else None
        use_mint = prices.get("use_mint") if self.enable_use else None
        use_mint_ok = mint_available(use_mint)
        use_box_state = mint_box_state(use_mint)


        for trade_size in self._analysis_sizes():
            slippage = config.get_recommended_slippage(trade_size)  # legs without known depth (CEX)

            # ---- SigUSD Paths ----

            for path_fn in (self._path_bank_mint, self._path_pool_buy_redeem):
                opp = path_fn(prices, trade_size)
                if opp is not None:
                    opportunities.append(opp)

            # Paths 3-4: CEX vs the pool. Both directions are computed and the better one is kept.
            # Watch-only: nothing converts SigUSD and USDT (WATCH_ONLY_REASON).
            pool = prices.get("spectrum_pool")
            for cex, mid, trading_fee, withdraw_fee in (
                    ("NonKYC", nonkyc_price, *self._fees_of("NonKYC")),
                    ("Kucoin", kucoin_price, *self._fees_of("Kucoin"))):
                if not (mid and spectrum_price):
                    continue
                ask = self._cex_side_price(prices, cex, trade_size, "buy") or mid
                bid = self._cex_side_price(prices, cex, trade_size, "sell") or mid
                opp = max((self.calculator.calc_cex_vs_dex(
                    input_erg=trade_size, cex_buy_price=ask, cex_sell_price=bid,
                    dex_erg_sigusd_price=spectrum_price, direction=d, cex_trading_fee=trading_fee,
                    erg_withdraw_fee=withdraw_fee, slippage=slippage, pool=pool)
                    for d in ("buy_cex_sell_dex", "buy_dex_sell_cex")), key=lambda o: o.profit_erg)
                buy_on_cex = opp.source_exchange == "CEX"
                opp.path = f"{cex}<>Spectrum [{trade_size} ERG]"
                if buy_on_cex:
                    opp.steps = [
                        f"START: Have USDT on {cex}",
                        f"Buy ERG on {cex} at ask ${ask:.4f} USDT ({trading_fee*100:.1f}% fee)",
                        f"Withdraw ERG to wallet (-{withdraw_fee} ERG withdrawal fee)",
                        f"Swap ERG -> SigUSD on the pool ({spectrum_price:.4f} SigUSD/ERG, {config.pool_fee_text()})",
                        "END RESULT: SigUSD tokens in Ergo wallet",
                        f"WATCH-ONLY: {opp.assumption}; no SigUSD<->USDT venue to close the loop",
                    ]
                else:
                    opp.steps = [
                        "START: Have SigUSD in wallet",
                        f"Swap SigUSD -> ERG on the pool ({spectrum_price:.4f} SigUSD/ERG, {config.pool_fee_text()})",
                        f"Deposit ERG to {cex} (~{config.ERGO_TX_FEE} ERG network fee)",
                        f"Sell ERG on {cex} at bid ${bid:.4f} USDT ({trading_fee*100:.1f}% fee)",
                        f"END RESULT: USDT on {cex}",
                        f"WATCH-ONLY: {opp.assumption}; no SigUSD<->USDT venue to close the loop",
                    ]
                opportunities.append(opp)

            # Path 5: NonKYC vs Kucoin (CEX to CEX): buy at the ask on one, sell at the bid on the other
            if nonkyc_price and kucoin_price:
                quotes = {name: (self._cex_side_price(prices, name, trade_size, "buy") or mid,
                                 self._cex_side_price(prices, name, trade_size, "sell") or mid)
                          for name, mid in (("NonKYC", nonkyc_price), ("Kucoin", kucoin_price))}
                fees_by = {"NonKYC": self._fees_of("NonKYC"), "Kucoin": self._fees_of("Kucoin")}
                options = []
                for buy_on, sell_on in (("Kucoin", "NonKYC"), ("NonKYC", "Kucoin")):
                    o = self.calculator.calc_cex_to_cex(
                        input_erg=trade_size,
                        sell_price_usdt=quotes[sell_on][1],
                        buy_price_usdt=quotes[buy_on][0],
                        sell_trading_fee=fees_by[sell_on][0],
                        buy_trading_fee=fees_by[buy_on][0],
                        erg_withdraw_fee=fees_by[buy_on][1],
                    )
                    o.path = f"{buy_on}->{sell_on} [{trade_size} ERG]"
                    o.steps = [
                        f"START: Have USDT on {buy_on}",
                        f"Buy ERG on {buy_on} at ask ${quotes[buy_on][0]:.4f} USDT ({fees_by[buy_on][0]*100:.1f}% fee)",
                        f"Withdraw ERG to {sell_on} (-{fees_by[buy_on][1]} ERG withdrawal fee)",
                        f"Sell ERG on {sell_on} at bid ${quotes[sell_on][1]:.4f} USDT ({fees_by[sell_on][0]*100:.1f}% fee)",
                        f"END RESULT: ~${o.output_erg * quotes[sell_on][1]:.2f} USDT on {sell_on}",
                    ]
                    options.append(o)
                opportunities.append(max(options, key=lambda o: o.profit_erg))

            # Paths 6-7: bank mint -> (SigUSD -> USDT: no venue) -> buy ERG on a CEX. Watch-only.
            for cex, mid, trading_fee, withdraw_fee, minutes in (
                    ("Kucoin", kucoin_price, *self._fees_of("Kucoin"), 60),
                    ("NonKYC", nonkyc_price, *self._fees_of("NonKYC"), 40)):
                if not (mid and bank_state is not None):
                    continue
                ask = self._cex_side_price(prices, cex, trade_size, "buy") or mid
                opp = self.calculator.calc_bank_to_cex(trade_size, bank_state, ask, trading_fee, withdraw_fee,
                                                       execution_minutes=minutes)
                opp.path = f"{cex}<>Bank [{trade_size} ERG]"
                opp.target_exchange = cex
                sigusd = opp.details["sigusd_cents"] / 100
                opp.steps = [
                    f"START: Have {trade_size} ERG in wallet",
                    f"Mint SigUSD at the bank: {trade_size} ERG -> {sigusd:.2f} SigUSD (contract-exact, fees included)",
                    "Sell SigUSD for USDT (no venue in this bot)",
                    f"Buy ERG on {cex} at ask ${ask:.4f} USDT ({trading_fee*100:.1f}% fee)",
                    f"Withdraw ERG to wallet (-{withdraw_fee} ERG)",
                    f"WATCH-ONLY: {opp.assumption}",
                ]
                opportunities.append(opp)

            # ---- USE (DexyUSD) Paths ----

            # Path 8: ERG -> USE (Dexy bank mint via Crux) -> ERG (Dexy LP via Crux)
            if use_lp is not None and use_box_state:
                mint_budget = trade_size - config.CRUX_MINT_SERVICE_FEE - config.ERGO_TX_FEE
                use_raw = quote_dexy_mint(int(mint_budget * 1e9), use_box_state) if mint_budget > 0 else 0
                use_minted = use_raw / 10**config.USE_DECIMALS
                erg_from_lp = use_lp.swap_output(use_minted, input_is_x=False)
                buffer_cost = erg_from_lp * config.EXECUTION_BUFFER
                erg_out = erg_from_lp - config.SPECTRUM_EXECUTION_FEE - config.ERGO_TX_FEE - buffer_cost
                profit = erg_out - trade_size
                pct = (profit / trade_size) * 100 if trade_size > 0 else 0
                oracle_use_erg = use_box_state["oracle_rate"] / 1e9  # ERG per USE at the oracle

                opportunities.append(ArbitrageOpportunity(
                    path=f"Crux mint+sell USE [{trade_size} ERG]",
                    input_erg=trade_size,
                    output_erg=erg_out,
                    profit_erg=profit,
                    profit_percent=pct,
                    fees=FeeBreakdown(
                        execution_fee_erg=config.CRUX_MINT_SERVICE_FEE + config.SPECTRUM_EXECUTION_FEE,
                        network_fee_erg=config.ERGO_TX_FEE * 2,
                        slippage_cost=buffer_cost,
                    ),
                    source_price=oracle_use_erg,
                    target_price=use_lp.price_y_in_x,
                    source_exchange="Dexy bank (via Crux)",
                    target_exchange="Dexy USE LP (via Crux)",
                    is_profitable=pct > config.MIN_PROFIT_PERCENT and use_mint_ok,
                    blocked=not use_mint_ok,
                    blocked_reason="" if use_mint_ok else "USE mint not available (FreeMint/ArbMint closed)",
                    estimated_execution_minutes=EXECUTION_TIMES["bank_to_dex"],
                    price_risk_percent=EXECUTION_TIMES["bank_to_dex"] * PRICE_RISK_PER_MINUTE,
                    details={"use_raw": use_raw, "lp_erg": erg_from_lp},
                    steps=[
                        f"START: Have {trade_size} ERG in wallet",
                        f"Mint USE via Crux (/dexy/build_mint_tx, -{config.CRUX_MINT_SERVICE_FEE} ERG service fee): "
                        f"~{use_minted:.3f} USE at {oracle_use_erg:.4f} ERG/USE +0.5% bank fees",
                        f"Sell {use_minted:.3f} USE -> {erg_from_lp:.2f} ERG on the Dexy LP "
                        f"({use_lp.reserve_x:,.0f} ERG deep, -0.3% LP fee, -{config.SPECTRUM_EXECUTION_FEE} ERG service fee)",
                        f"END RESULT: ~{erg_out:.2f} ERG in wallet (net {profit:+.2f} ERG)",
                    ],
                ))

        return opportunities

    def _cex_watch_gaps(self, prices: dict) -> list[dict]:
        """Gap between each watched CEX and the on-chain pool price (1 SigUSD ~ $1)."""
        pool = prices.get("spectrum_erg_sigusd")
        gaps = []
        if not pool:
            return gaps
        for name, q in (prices.get("cex_watch") or {}).items():
            wd = self.cex_fees.get(name).erg_withdraw
            buy_cex = (pool - q.ask) / q.ask * 100 if q.ask else 0   # ERG cheaper on the CEX
            sell_cex = (q.bid - pool) / pool * 100 if pool else 0    # ERG dearer on the CEX
            if buy_cex >= sell_cex:
                gap, how = buy_cex, f"ERG cheaper on {name} (ask ${q.ask:.4f}) than the on-chain pool ({pool:.4f} SigUSD)"
            else:
                gap, how = sell_cex, f"ERG dearer on {name} (bid ${q.bid:.4f}) than the on-chain pool ({pool:.4f} SigUSD)"
            gaps.append({
                "exchange": name,
                "gap_percent": gap,
                "alert": gap >= config.CEX_WATCH_ALERT_PERCENT,
                "text": (f"{how}: {gap:+.2f}%. watch-only, assumes 1 SigUSD ~ $1, ignores the "
                         f"{'unknown' if wd is None else f'{wd:g}'} ERG withdrawal fee and trading fees"),
            })
        return gaps

    def _display_cex_watch(self, prices: dict):
        for g in self._cex_watch_gaps(prices):
            style = "bold yellow" if g["alert"] else "dim"
            hint = " -> worth connecting?" if g["alert"] else ""
            self.out.print(f"  [{style}]WATCH {g['exchange']}: {g['text']}{hint}[/{style}]")
        spread = self._cex_spread(prices)
        if spread is not None:
            style = "bold yellow" if spread.profitable else "dim"
            self.out.print(f"  [{style}]WATCH spread: {self._spread_text(spread)}[/{style}]")

    async def _notify_cex_watch(self, prices: dict):
        """One combined Discord watch message (at most once per CEX_WATCH_COOLDOWN_SECONDS), sent when an
        exchange is far from the pool or the cross-exchange spread pays after every fee."""
        gaps = self._cex_watch_gaps(prices)
        spread = self._cex_spread(prices)
        if not (any(g["alert"] for g in gaps) or (spread is not None and spread.profitable)):
            return
        lines = [f"{g['exchange']}: {g['text']}" for g in gaps]
        if spread is not None:
            lines.append(f"Best spread: {self._spread_text(spread)}")
        await self.discord.notify_watch("CEX", "\n".join(lines))

    @staticmethod
    def _sigusd_premium_percent(prices: dict) -> Optional[float]:
        """SigUSD price on the pool vs the oracle ($1), in percent."""
        oracle = (prices.get("bank") or {}).get("oracle_erg_usd")
        spot = prices.get("spectrum_erg_sigusd")
        if not oracle or not spot:
            return None
        return (oracle / spot - 1) * 100

    def _explain(self, opp: ArbitrageOpportunity, prices: dict) -> str:
        """One line on why a path is (not) profitable at its best size."""
        min_pct = config.MIN_PROFIT_PERCENT
        profit = opp.profit_percent
        short = min_pct - profit
        size = f"{opp.input_erg:g} ERG"
        premium = self._sigusd_premium_percent(prices)
        key = opp.path_key

        if premium is not None and key == "Bank mint->Spectrum sell":
            cost = premium - profit  # profit ~ premium - costs
            if profit > min_pct:
                text = f"clears: SigUSD at {premium:+.2f}% on the pool vs ~{cost:.2f}% costs -> {profit:+.2f}% at {size}"
            else:
                text = (f"SigUSD at {premium:+.2f}% vs oracle on the pool; mint+sell costs ~{cost:.2f}% "
                        f"(bank fees, pool fee + impact, buffer, miner), so it needs > {cost + min_pct:+.2f}%: "
                        f"short by {short:.2f}% at {size}")
        elif premium is not None and key == "Spectrum buy->Bank redeem":
            cost = -premium - profit  # profit ~ discount - costs
            if profit > min_pct:
                text = f"clears: SigUSD at {premium:+.2f}% on the pool vs ~{cost:.2f}% costs -> {profit:+.2f}% at {size}"
            else:
                text = (f"SigUSD at {premium:+.2f}% vs oracle on the pool; buy+redeem needs a discount of "
                        f"> {cost + min_pct:.2f}% (costs ~{cost:.2f}%): short by {short:.2f}% at {size}")
        elif profit > min_pct:
            text = f"clears: {profit:+.2f}% after fees at {size}"
        else:
            text = f"{profit:+.2f}% after fees at {size}, needs > {min_pct:.2f}%: short by {short:.2f}%"

        if opp.blocked and opp.blocked_reason:
            text += f" [{opp.blocked_reason}]"
        return text

    def _display_opportunities(self, opportunities: list[ArbitrageOpportunity], prices: Optional[dict] = None):
        """Display opportunities as a full grid: paths x trade sizes."""
        if not opportunities:
            self.out.print("[dim]No opportunities to display[/dim]")
            return

        # Group by path type
        path_groups: dict[str, list[ArbitrageOpportunity]] = {}
        for opp in opportunities:
            path_name = opp.path_key
            path_groups.setdefault(path_name, []).append(opp)

        # Collect all trade sizes used
        all_sizes = sorted(set(o.input_erg for o in opportunities))

        best_per_path: list[ArbitrageOpportunity] = []
        for path_name, opps in path_groups.items():
            best = max(opps, key=lambda x: x.profit_percent)
            best_per_path.append(best)

        profitable = [o for o in best_per_path if o.is_profitable and not o.blocked]
        has_profitable = len(profitable) > 0

        # --- Full Grid: paths x sizes ---
        title = "Arbitrage Grid (profit %)"
        if has_profitable:
            title = f"Arbitrage Grid | {len(profitable)} profitable path(s)"
        grid = Table(
            title=title,
            show_header=True,
            header_style="bold cyan",
            border_style="green" if has_profitable else "dim",
            padding=(0, 1),
        )
        grid.add_column("Path", style="bold", no_wrap=True)
        for size in all_sizes:
            star = "*" if size > config.MAX_TRADE_SIZE_ERG else ""
            grid.add_column(f"{size:.0f}{star}", justify="right", min_width=6)
        grid.add_column("Status", justify="center")

        # Sort paths: profitable first (by best %), then unprofitable, then blocked
        def path_sort_key(name):
            opps = path_groups[name]
            best = max(opps, key=lambda x: x.profit_percent)
            if best.blocked:
                return (2, -best.profit_percent)
            elif best.is_profitable:
                return (0, -best.profit_percent)
            else:
                return (1, -best.profit_percent)

        for path_name in sorted(path_groups.keys(), key=path_sort_key):
            opps = path_groups[path_name]
            best = max(opps, key=lambda x: x.profit_percent)

            # Build size->opp lookup
            size_map = {o.input_erg: o for o in opps}

            display_path = path_name
            if best.assumption:
                display_path = f"{path_name} *"

            # Status column
            if best.blocked:
                status = "[bold red]BLOCKED[/bold red]"
            elif best.is_profitable:
                risk_ok = best.risk_adjusted_profitable
                status = "[bold green]GO[/bold green]" if risk_ok else "[yellow]RISKY[/yellow]"
            else:
                status = "[dim]-[/dim]"

            # Build cells for each size
            cells = []
            for size in all_sizes:
                opp = size_map.get(size)
                if opp is None:
                    cells.append("[dim]-[/dim]")
                elif opp.blocked:
                    cells.append(f"[dim]{opp.profit_percent:+.1f}%[/dim]")
                elif opp.is_profitable:
                    cells.append(f"[bold green]{opp.profit_percent:+.1f}%[/bold green]")
                elif opp.profit_percent > -0.5:
                    cells.append(f"[yellow]{opp.profit_percent:+.1f}%[/yellow]")
                else:
                    cells.append(f"[dim]{opp.profit_percent:+.1f}%[/dim]")

            if best.blocked:
                grid.add_row(f"[dim]{display_path}[/dim]", *cells, status)
            else:
                grid.add_row(display_path, *cells, status)

        # Footnotes
        footnotes = set()
        for opps in path_groups.values():
            for o in opps:
                if o.assumption:
                    footnotes.add(o.assumption)
        if any(size > config.MAX_TRADE_SIZE_ERG for size in all_sizes):
            footnotes.add(f"sizes above MAX_TRADE_SIZE_ERG={config.MAX_TRADE_SIZE_ERG:g}: analysis only")
        if footnotes:
            grid.caption = " | ".join(f"* {a}" for a in sorted(footnotes))

        self.out.print(grid)

        # Best size per on-chain path (searched, capped by MAX_TRADE_SIZE_ERG)
        for key, opt in self.last_optima.items():
            choice = self.last_sizing.get(key)
            if choice is not None:
                style = "bold green" if choice.ok else "dim"
                self.out.print(f"  [{style}]BEST SIZE {key}: {choice.summary()}[/{style}]")
                continue
            if opt.profit_erg <= 0:
                self.out.print(f"  [dim]BEST SIZE {key}: no profitable size up to {config.MAX_TRADE_SIZE_ERG:g} ERG[/dim]")
                continue
            style = "bold green" if opt.is_profitable and not opt.blocked else "yellow"
            self.out.print(
                f"  [{style}]BEST SIZE {key}: {opt.input_erg:g} ERG -> {opt.profit_erg:+.4f} ERG "
                f"({opt.profit_percent:+.2f}%)[/{style}]"
            )

        # Why each path is (not) profitable, including blocked reasons
        for opp in sorted(best_per_path, key=lambda o: -o.profit_percent):
            style = "green" if opp.is_profitable and not opp.blocked else ("red" if opp.blocked else "dim")
            self.out.print(f"  [bold]WHY {opp.path_key}:[/bold] [{style}]{self._explain(opp, prices or {})}[/{style}]")

        # Show steps for GO/RISKY paths
        actionable = [o for o in best_per_path if o.is_profitable and not o.blocked]
        for opp in sorted(actionable, key=lambda x: x.profit_percent, reverse=True):
            path_name = opp.path_key
            risk_ok = opp.risk_adjusted_profitable
            status_tag = "[bold green]GO[/bold green]" if risk_ok else "[yellow]RISKY[/yellow]"
            self.out.print(f"\n>> {path_name} [{opp.input_erg:.0f} ERG] {status_tag} [bold green]{opp.profit_percent:+.1f}% ({opp.profit_erg:+.2f} ERG)[/bold green]")
            if opp.steps:
                for i, step in enumerate(opp.steps, 1):
                    if step.startswith("WARNING:"):
                        self.out.print(f"   [bold yellow]!! {step}[/bold yellow]")
                    else:
                        self.out.print(f"   [dim]{i}. {step}[/dim]")

        # --- Summary table for profitable paths only ---
        if profitable:
            summary = Table(
                title="Profitable Paths - Best Size",
                show_header=True,
                header_style="bold green",
                border_style="green",
            )
            summary.add_column("Path", style="bold", min_width=20)
            summary.add_column("Size", justify="right")
            summary.add_column("Profit", justify="right")
            summary.add_column("ERG", justify="right")
            summary.add_column("USD", justify="right")
            summary.add_column("Time", justify="right")
            summary.add_column("Status", justify="center")

            for opp in sorted(profitable, key=lambda x: x.profit_percent, reverse=True):
                path_name = opp.path_key
                risk_ok = opp.risk_adjusted_profitable
                summary.add_row(
                    path_name,
                    f"{opp.input_erg:.0f} ERG",
                    f"[bold green]{opp.profit_percent:+.2f}%[/bold green]",
                    f"[bold green]{opp.profit_erg:+.4f}[/bold green]",
                    f"[bold green]${opp.profit_usd:+.2f}[/bold green]",
                    f"{opp.estimated_execution_minutes:.0f}m",
                    "[bold green]GO[/bold green]" if risk_ok else "[yellow]RISKY[/yellow]",
                )
            self.out.print(summary)

            best = max(profitable, key=lambda x: x.profit_percent)
            self.out.print(Panel(
                f"[bold green]BEST: {best.path}\n"
                f"Input: {best.input_erg:.2f} ERG -> Output: {best.output_erg:.4f} ERG\n"
                f"Profit: {best.profit_erg:+.4f} ERG (${best.profit_usd:+.2f}) ({best.profit_percent:+.2f}%)\n"
                f"Fees: {best.fees.total_fee_erg:.4f} ERG + ${best.fees.total_fee_usd:.4f}\n"
                f"Time: ~{best.estimated_execution_minutes:.0f} min | Price risk: {best.price_risk_percent:.1f}% | "
                f"Risk-adjusted: {'YES' if best.risk_adjusted_profitable else 'NO'}[/bold green]",
                title="Best Opportunity",
                border_style="green",
            ))

    def _get_path_sources(self, path_key: str) -> list[str]:
        """Return which price sources a path depends on."""
        sources = []
        pk = path_key.lower()
        if "nonkyc" in pk:
            sources.append("nonkyc")
        if "kucoin" in pk:
            sources.append("kucoin")
        if "spectrum" in pk:
            sources.append("spectrum")
        if "bank" in pk:
            sources.append("bank")
        if "crux" in pk or "use" in pk:
            sources.append("use")
        return sources

    def _is_price_stale(self, path_key: str) -> bool:
        """Check if any price source used by this path is stale."""
        now = time.time()
        for source in self._get_path_sources(path_key):
            last = self._price_timestamps.get(source, 0)
            if (now - last) > config.PRICE_STALE_SECONDS:
                return True
        return False

    def _update_streaks(self, opportunities: list[ArbitrageOpportunity]):
        """Update consecutive scan streak counts for profitable paths."""
        profitable_keys = set()
        for opp in opportunities:
            if opp.is_profitable and not opp.blocked:
                path_key = opp.path_key
                profitable_keys.add(path_key)

        # Increment streaks for paths that are profitable this scan
        for key in profitable_keys:
            self._opportunity_streak[key] = self._opportunity_streak.get(key, 0) + 1

        # Reset streaks for paths that disappeared
        for key in list(self._opportunity_streak.keys()):
            if key not in profitable_keys:
                self._opportunity_streak[key] = 0

    def _episode_embed(self, ep, status: str) -> dict:
        s = self.state
        bank = (s.prices or {}).get("bank") or {}
        age = time.time() - self._price_timestamps["spectrum"] if self._price_timestamps.get("spectrum") else None
        return embeds.episode_embed(ep, status, height=s.height, data_age_s=age,
                                    oracle_usd=bank.get("oracle_erg_usd"))

    def _handle_episode(self, event):
        ep = event.episode
        if event.kind == "open":
            ep.db_id = self.tracker.open_chain_episode(ep)
            tier1 = ep.profit_percent >= config.DISCORD_TIER1_PROFIT_PERCENT
            self.discord.post(self._episode_embed(ep, "open"), content=self.discord._ping() if tier1 else "",
                              on_id=lambda mid, ep=ep: self._episode_posted(ep, mid))
            self.state.add_event("info", f"Discord: opened {ep.label} {ep.profit_percent:+.2f}%")
        elif event.kind == "update":
            self.tracker.update_chain_episode(ep.db_id, ep)
            self.discord.edit(lambda ep=ep: ep.message_id, self._episode_embed(ep, "open"))
        else:
            self.tracker.close_chain_episode(ep.db_id, ep)
            self.discord.edit(lambda ep=ep: ep.message_id, self._episode_embed(ep, "closed"))
            self.state.add_event("info", f"Discord: closed {ep.label} (peak {ep.peak_percent:+.2f}%)")

    def _handle_mint_gate(self, event):
        if event.kind == "opened":
            self.discord.post(embeds.mint_gate_embed(event), content=self.discord._ping() if event.ping else "",
                              on_id=lambda mid, op=event.opening: self._mint_message_posted(op, mid))
            self.state.add_event("good", f"Bank mint OPEN: RR {event.reserve_ratio:.0f}%, "
                                         f"room ~{event.room_erg:,.0f} ERG")
        else:
            self.discord.edit(lambda op=event.opening: op.message_id, embeds.mint_gate_embed(event))
            self.tracker.set_meta(MINT_MESSAGE_KEY, "")
            self.state.add_event("info", f"Bank mint closed again (RR {event.reserve_ratio:.0f}%)")

    def _mint_message_posted(self, opening, message_id: str):
        opening.message_id = message_id
        self.tracker.set_meta(MINT_MESSAGE_KEY, message_id)   # so the next start can grey it after a crash

    def _episode_posted(self, ep, message_id: str):
        ep.message_id = message_id
        self.tracker.set_chain_episode_message(ep.db_id, message_id)  # so a restart can close it

    def _close_stale_discord_messages(self):
        """Grey out the Discord messages of episodes a previous run left open (crash, closed window)."""
        for row in self.tracker.claim_stale_chain_episodes():
            if row.get("message_id"):
                self.discord.edit(lambda mid=row["message_id"]: mid, embeds.stale_episode_embed(row))
        mint_message = self.tracker.get_meta(MINT_MESSAGE_KEY)
        if mint_message:
            self.discord.edit(lambda: mint_message, embeds.mint_gate_stopped_embed())
            self.tracker.set_meta(MINT_MESSAGE_KEY, "")

    def _close_episodes_on_shutdown(self):
        try:
            for event in self.episodes.close_all(self._now, "bot stopped"):
                self._handle_episode(event)
            if self.mint_gate.is_open and self.mint_gate.opening is not None:
                self.discord.edit(lambda op=self.mint_gate.opening: op.message_id, embeds.mint_gate_stopped_embed())
                self.tracker.set_meta(MINT_MESSAGE_KEY, "")
        except Exception as e:  # shutdown must still send the summary and close connections
            logger.error(f"Closing Discord episodes failed: {e}", exc_info=True)

    def _maybe_send_digest(self):
        """Queue the daily digest when due; it counts as sent once Discord returns the message id.
        One that never comes back (Discord down) is queued again after DIGEST_RETRY_S."""
        if not self.discord_enabled:
            return
        in_flight = self._digest_queued_at is not None and time.time() - self._digest_queued_at < DIGEST_RETRY_S
        now = datetime.now()
        if in_flight or not digest_due(self.tracker, now, config.DISCORD_DIGEST_HOUR):
            return
        digest = build_digest(self.tracker, self.health, self._last_wallet, now, bank=self._bank_for_alerts())
        self._digest_queued_at = time.time()
        self.discord.post(embeds.digest_embed(digest), on_id=lambda mid, now=now: self._digest_delivered(now))

    def _digest_delivered(self, queued_at: datetime):
        mark_digest_sent(self.tracker, queued_at)
        self._digest_queued_at = None

    def _bank_for_alerts(self) -> Optional[dict]:
        """The latest bank state, or None while the chain is unreadable (never a stale reading)."""
        return None if self._chain_error else (self._chain_prices or {}).get("bank")

    def _tick_episodes(self, now: float):
        for event in self.episodes.update(now, self.state.paths):
            self._handle_episode(event)
        for ep in list(self.episodes.open.values()):
            if ep.db_id and now - self._episode_touched.get(ep.db_id, now) >= EPISODE_HEARTBEAT_S:
                self.tracker.touch_chain_episode(ep.db_id)
                self._episode_touched[ep.db_id] = now
            self._episode_touched.setdefault(ep.db_id, now)

    def _tick_health(self, now: float):
        for h in self.health.update(time.time(), self.state):
            self.discord.post(embeds.health_embed(h), content=self.discord._ping() if h.ping else "")
            self.state.add_event("warn" if h.kind == "alert" else "info", f"Discord: {h.text}")

    def _tick_mint_gate(self, now: float):
        gate = self.mint_gate.update(now, self._bank_for_alerts())
        if gate:
            self._handle_mint_gate(gate)

    def _discord_tick(self, now: float):
        """Episodes, health alerts and the mint gate from the refreshed state. Never awaits Discord;
        each part has its own error handler, so one failing never skips the others."""
        if not self.discord_enabled:
            return
        for part in (self._tick_episodes, self._tick_health, self._tick_mint_gate):
            try:
                part(now)
            except Exception as e:
                logger.error(f"Discord tick error ({part.__name__}): {e}", exc_info=True)

    async def _notify_discord(self, opportunities: list[ArbitrageOpportunity]):
        """Send Discord notifications for confirmed, non-stale opportunities."""
        if not self.discord_enabled:
            return

        self._update_streaks(opportunities)

        # Filter to confirmed opportunities (streak >= CONFIRM_SCANS)
        confirmed = []
        for opp in opportunities:
            if not opp.is_profitable or opp.blocked:
                continue
            if opp.path_key in LIVE_PATHS:
                continue  # on-chain paths: one edited message per episode (_discord_tick)
            path_key = opp.path_key
            streak = self._opportunity_streak.get(path_key, 0)
            if streak < config.DISCORD_CONFIRM_SCANS:
                logger.debug(f"Streak {streak}/{config.DISCORD_CONFIRM_SCANS}: {path_key}")
                continue
            if self._is_price_stale(path_key):
                logger.debug(f"Stale price, skipping notification: {path_key}")
                continue
            confirmed.append(opp)

        if confirmed:
            sent = await self.discord.notify_opportunities(
                confirmed, scan_number=self.scan_count
            )
            if sent > 0:
                logger.info(f"Sent {sent} Discord notification(s) (confirmed)")
                self.state.add_event("info", f"Discord alert sent ({sent} path(s))")

        # Log streak status for visibility
        active_streaks = {k: v for k, v in self._opportunity_streak.items() if v > 0}
        if active_streaks:
            streak_info = ", ".join(f"{k}: {v}" for k, v in active_streaks.items())
            logger.info(f"Streaks: {streak_info}")

    def _update_live_streak(self):
        """Consecutive scans in which each executable path is profitable at its best size."""
        for key in LIVE_PATHS:
            choice = self.last_sizing.get(key)
            ok = bool(choice and choice.ok and self._chain_error is None)
            self._live_streak[key] = self._live_streak.get(key, 0) + 1 if ok else 0

    @staticmethod
    def _wallet_value_erg(wallet: dict, prices: dict) -> float:
        oracle = (prices.get("bank") or {}).get("oracle_erg_usd")
        sigusd_erg = wallet.get("sigusd", 0) / oracle if oracle else 0.0
        return wallet.get("erg", 0) + sigusd_erg

    @staticmethod
    def _live_cap(wallet: dict) -> float:
        """ERG a live trade may use; the runner sizes the trade within this on fresh boxes."""
        return max(0.0, min(config.MAX_TRADE_SIZE_ERG, wallet.get("erg", 0) - config.LIVE_ERG_RESERVE))

    def _trades_today_count(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        return self._trades_today[1] if self._trades_today[0] == today else 0

    def _path_blockers(self, key: str, wallet: dict) -> list[str]:
        """Reasons this particular path would not trade (profit, confirmations, size)."""
        blockers = []
        choice = self.last_sizing.get(key)
        if choice is None:
            blockers.append("no profit data (pool reserves or bank state missing)")
        elif not choice.ok:
            blockers.append(f"no profit >= {config.MIN_PROFIT_PERCENT}% ({choice.reason})")
        streak = self._live_streak.get(key, 0)
        if streak < config.LIVE_CONFIRM_POLLS:
            blockers.append(f"profitable {streak}/{config.LIVE_CONFIRM_POLLS} polls in a row")
        if self._live_cap(wallet) < config.MIN_TRADE_SIZE_ERG:
            blockers.append(f"wallet too small: {wallet.get('erg', 0):.2f} ERG minus {config.LIVE_ERG_RESERVE:g} reserve")
        return blockers

    def _sync_global_blockers(self, wallet: Optional[dict], prices: dict) -> list[str]:
        """Global blockers that need no node call (chain, oracle, STOP, pause, drawdown, cooldown, daily max)."""
        blockers = []
        if self._chain_error is not None:
            blockers.append(f"chain state unavailable ({self._chain_error})")
        elif self._snapshot is not None and "oracle" in self._snapshot.pending:
            blockers.append("oracle update pending (waiting for it to confirm)")
        if os.path.exists(config.LIVE_STOP_FILE):
            blockers.append(f"STOP file present ({config.LIVE_STOP_FILE})")
        if self._live_paused:
            blockers.append(f"paused after: {self._live_paused} (restart to resume)")
        if wallet is not None and self._live_start_value is not None:
            drop = self._live_start_value - self._wallet_value_erg(wallet, prices)
            if drop > config.LIVE_MAX_DRAWDOWN_ERG:
                blockers.append(f"drawdown {drop:.2f} ERG > LIVE_MAX_DRAWDOWN_ERG {config.LIVE_MAX_DRAWDOWN_ERG:g}")
        wait = config.LIVE_TRADE_COOLDOWN_SECONDS - (time.time() - self._last_trade_time)
        if self._last_trade_time and wait > 0:
            blockers.append(f"cooldown {wait:.0f}s")
        if self._trades_today_count() >= config.LIVE_MAX_TRADES_PER_DAY:
            blockers.append(f"max {config.LIVE_MAX_TRADES_PER_DAY} trades per day reached")
        return blockers

    async def _global_blockers(self, wallet: dict, prices: dict) -> list[str]:
        """Reasons no path would trade right now (kill switch, pause, limits, node)."""
        blockers = []
        if not wallet.get("ok", True):
            blockers.append("wallet balance unreadable")
        elif self._live_start_value is None:
            oracle = (prices.get("bank") or {}).get("oracle_erg_usd")
            if oracle or not wallet.get("sigusd"):  # held SigUSD needs a price to be valued
                self._live_start_value = self._wallet_value_erg(wallet, prices)
        if self._live_start_value is None:
            blockers.append("no drawdown baseline yet (wallet or oracle price unreadable)")
        blockers += self._sync_global_blockers(wallet, prices)
        health = await self.ergo_node.get_health()
        self._health_blocker = None
        if not health.get("ok_to_trade"):
            self._health_blocker = (f"node not ready (synced={health.get('synced')}, "
                                    f"unlocked={health.get('unlocked')})")
            blockers.append(self._health_blocker)
        return blockers

    async def _live_blockers(self, wallet: dict, prices: dict, key: str = LIVE_PATH) -> list[str]:
        """Every reason `key` would not execute right now (empty list = ready)."""
        return self._path_blockers(key, wallet) + await self._global_blockers(wallet, prices)

    def _trade_log(self, message: str):
        """Runner progress: always in the log file, printed in plain view, an event otherwise.

        Runs between the two legs of a live trade, so it must never raise: node error text can
        contain things that look like rich markup (e.g. "[/detail]").
        """
        logger.info(f"LIVE {message.strip()}")
        try:
            self.out.print(message)
        except Exception:
            self.out.print(message, markup=False)
        try:
            text = Text.from_markup(message).plain.strip()
        except Exception:
            text = message.strip()
        if text:
            self.state.add_event("trade", text)

    async def _execute_trades(self, wallet: dict, prices: dict, quiet: bool = False):
        """--live: run the most profitable executable path at its best size when every check passes.

        quiet (fast polls): blocker lines are printed only when they differ from the last ones shown.
        """
        if not self.trading_enabled:
            return
        global_blockers = await self._global_blockers(wallet, prices)
        ready = [k for k in LIVE_PATHS if not self._path_blockers(k, wallet)]
        if global_blockers or not ready:
            lines = tuple(f"LIVE {LIVE_PATHS[key]}: not trading - "
                          f"{'; '.join(self._path_blockers(key, wallet) + global_blockers)}" for key in LIVE_PATHS)
            if not (quiet and lines == self._last_blocked):
                for line in lines:
                    self.out.print(f"  [dim]{line}[/dim]")
            self._last_blocked = lines
            shape = tuple(re.sub(r"\d+(\.\d+)?", "#", line) for line in lines)  # ignore ticking counters
            if self.view != "plain" and shape != self._last_blocked_logged:
                for line in lines:
                    logger.info(line)
            self._last_blocked_logged = shape
            return
        self._last_blocked = None

        key = max(ready, key=lambda k: self.last_sizing[k].profit_erg)
        path = LIVE_PATHS[key]
        choice = self.last_sizing[key]
        cap = self._live_cap(wallet)
        self.out.print(Panel(f"[bold red]LIVE: executing {key}, scan says {choice.summary()}; "
                            f"re-sizing on fresh boxes (cap {cap:g} ERG)[/bold red]", border_style="red"))
        trade_id = self.tracker.start_trade(None, choice.cost_erg, choice.cost_erg + choice.profit_erg,
                                            choice.profit_erg)
        try:
            result = await run_arb(self.ergo_node.session, path, None, max_erg_in=int(round(cap * 1e9)),
                                   execute=True, log=self._trade_log)
        except Exception as e:  # fail closed: leg 1 may already be on chain
            logger.error(f"LIVE runner error: {e}", exc_info=True)
            result = ArbResult("error", f"{e.__class__.__name__}: {e}", path=path)
        size = result.erg_in / 1e9
        profit = result.profit_nanoerg / 1e9
        # The guards first: cooldown, daily count and pause must hold even if the bookkeeping below fails
        self._last_trade_time = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        self._trades_today = (today, self._trades_today_count() + 1)
        pause = {"leg2_failed": f"leg 2 failed ({result.message})",
                 "error": f"runner error ({result.message})",
                 "refused": f"TX guard refused ({result.message})"}.get(result.status)
        if pause:
            self._live_paused = pause
        self._record_trade(trade_id, result, path, size, profit)

        if result.status == "executed":
            msg = (f"executed {key} with {size:g} ERG, expected {profit:+.4f} ERG "
                   f"({result.profit_percent:+.2f}%). Leg 1 {result.tx1}, leg 2 {result.tx2}")
        elif result.status == "leg2_failed":
            msg = (f"LEG 2 FAILED after leg 1 ({result.tx1}): {result.message}. Holding "
                   f"{result.sigusd_cents / 100:.2f} SigUSD. Live trading paused. Finish with: "
                   f"{result.recover_command}")
        elif result.status == "leg1_dropped":
            msg = f"did not complete {key}: {result.message}. Nothing was spent."
        elif result.status == "error":
            msg = (f"UNEXPECTED ERROR while executing {key}: {result.message}. Leg 1 may already be on chain: "
                   f"check `python arb.py balance` (redeem any SigUSD with `python arb.py redeem --sigusd all "
                   f"--execute`). Live trading paused.")
        else:
            msg = f"did not execute {key} ({result.status}): {result.message or 'see console'}. Nothing was spent."
        self.episodes.note_trade(key, f"{result.status}: {profit:+.4f} ERG" if result.status == "executed"
                                 else f"{result.status}: {result.message or ''}")
        logger.warning(f"LIVE {msg}")
        self.state.add_event("good" if result.status == "executed" else "warn", f"LIVE {msg}")
        if self.discord_enabled:
            await self.discord.notify_live(msg, ping=result.status != "not_profitable")

    def _record_trade(self, trade_id: int, result, path: str, size: float, profit: float):
        """Tracker rows for a finished trade attempt. Never raises: a locked database must not hide
        the result from the live gate (the cooldown, count and pause are already set)."""
        try:
            if result.erg_in:
                self.tracker.set_trade_input(trade_id, size, size + profit, profit)
            if result.status == "executed":
                self.tracker.complete_trade(trade_id, actual_output=size + profit, fee_paid_erg=0.0022,
                                            tx_ids=[result.tx1, result.tx2],
                                            notes=f"{path}; expected (pre-confirmation)")
            elif result.status == "leg2_failed":
                self.tracker.fail_trade(trade_id, result.message, notes=f"{path}; leg 1 {result.tx1}; holding SigUSD")
            elif result.status == "leg1_dropped":
                self.tracker.fail_trade(trade_id, result.message, notes=f"{path}; leg 1 dropped, nothing spent")
            elif result.status == "error":
                self.tracker.fail_trade(trade_id, result.message,
                                        notes=f"{path}; unexpected error, leg 1 may be on chain")
            else:
                self.tracker.fail_trade(trade_id, f"{result.status}: {result.message}", notes=f"{path}; nothing spent")
        except Exception as e:
            logger.error(f"LIVE trade bookkeeping failed (the trade result stands): {e}", exc_info=True)

    async def _fetch_wallet_balances(self) -> dict:
        """Fetch current wallet balances."""
        balances = await self.ergo_node.get_wallet_balances()
        if not balances:
            return {"erg": 0, "sigusd": 0, "use": 0, "ok": False}  # unreadable: never a drawdown baseline
        tokens = balances.get("tokens", {})
        sigusd_raw = tokens.get(config.SIGUSD_TOKEN_ID, {}).get("amount", 0)
        use_raw = tokens.get(config.USE_TOKEN_ID, {}).get("amount", 0)
        return {
            "erg": balances.get("erg", 0),
            "sigusd": sigusd_raw / 100,  # 2 decimals
            "use": use_raw / 1000,  # 3 decimals
            "ok": True,
        }

    def _build_wallet_analysis(self, wallet: dict, prices: dict):
        """Analyze wallet by asset: what paths exist, profit for each.

        Each option is a dict:
            name: str - short path name
            steps: list[str] - step-by-step flow with amounts
            profit_pct: float - % profit vs baseline
            profit_desc: str - human-readable profit summary
            result: str - what you end up with
            blocked: bool
            blocked_reason: str
        """
        erg = wallet.get("erg", 0)
        sigusd = wallet.get("sigusd", 0)
        use = wallet.get("use", 0)

        oracle_price = prices.get("bank", {}).get("oracle_erg_usd")
        spectrum_price = prices.get("spectrum_erg_sigusd")
        bank_state: Optional[BankState] = prices.get("bank", {}).get("state")
        can_redeem = bank_state is not None  # SigUSD redeem has no RR restriction
        nonkyc_price = prices.get("nonkyc_erg_usdt") if self.enable_cex else None
        kucoin_price = prices.get("kucoin_erg_usdt") if self.enable_cex else None
        fee_factor = (1 - config.SIGMAUSD_PROTOCOL_FEE) * (1 - config.SIGMAUSD_FRONTEND_FEE)
        bank_fee_pct = (1 - fee_factor) * 100  # ~2.225%

        nonkyc_usdt_fee = self._nonkyc_usdt_fee
        kucoin_usdt_fee = self._kucoin_usdt_fee

        erg_options = []
        sigusd_options = []
        use_options = []

        # ===================== ERG OPTIONS =====================
        if erg > 2:
            # 1. Bank mint -> Spectrum sell
            if bank_state and spectrum_price:
                mint_cents, mint_ok = self._bank_mint(bank_state, erg)
                if mint_ok:
                    sigusd_out = mint_cents / 100
                    erg_before_fees = self._dex_sigusd_to_erg(prices, sigusd_out)
                    total_fees = config.pool_service_fee() + config.ERGO_TX_FEE * 2
                    erg_back = erg_before_fees - total_fees
                    net = erg_back - erg
                    pct = (net / erg) * 100
                    erg_options.append({
                        "name": "Bank mint -> Spectrum sell",
                        "steps": [
                            f"Mint SigUSD at Bank: {erg:.2f} ERG -> {sigusd_out:.2f} SigUSD (oracle ${oracle_price:.4f}, -{bank_fee_pct:.2f}% bank fee)",
                            f"Swap SigUSD -> ERG on Spectrum: {sigusd_out:.2f} SigUSD -> {erg_before_fees:.2f} ERG (-0.5% pool fee, {config.pool_fee_text()})",
                            f"Network fees: -{config.ERGO_TX_FEE * 2} ERG (2 txns)",
                        ],
                        "profit_pct": pct,
                        "profit_desc": f"{net:+.2f} ERG ({pct:+.1f}%)",
                        "result": f"{erg_back:.2f} ERG in wallet",
                        "blocked": False, "blocked_reason": "",
                    })
                else:
                    rr = prices.get('bank', {}).get('reserve_ratio', 0)
                    erg_options.append({"name": "Bank mint -> Spectrum sell", "steps": [], "profit_pct": 0,
                        "profit_desc": "", "result": "", "blocked": True,
                        "blocked_reason": f"Bank mint BLOCKED (RR={rr:.0f}%, post-mint RR would drop below 400%)"})

            # 2. Spectrum buy SigUSD -> Bank redeem
            if bank_state and spectrum_price:
                if can_redeem:
                    sigusd_out = self._dex_erg_to_sigusd(prices, erg)
                    _, erg_from_bank = self._bank_redeem_erg(bank_state, sigusd_out)
                    total_fees = config.pool_service_fee() + config.ERGO_TX_FEE + config.SIGMAUSD_REDEEM_EXTRA_ERG
                    erg_back = erg_from_bank - total_fees
                    net = erg_back - erg
                    pct = (net / erg) * 100
                    erg_options.append({
                        "name": "Spectrum buy -> Bank redeem",
                        "steps": [
                            f"Swap ERG -> SigUSD on Spectrum: {erg:.2f} ERG -> {sigusd_out:.2f} SigUSD (-0.5% pool fee, {config.pool_fee_text()})",
                            f"Redeem SigUSD at Bank: {sigusd_out:.2f} SigUSD -> {erg_from_bank:.2f} ERG (oracle ${oracle_price:.4f}, -{bank_fee_pct:.2f}% bank fee)",
                            f"Extra fees: -{config.SIGMAUSD_REDEEM_EXTRA_ERG} ERG (receipt + miner), -{config.ERGO_TX_FEE} ERG network",
                        ],
                        "profit_pct": pct,
                        "profit_desc": f"{net:+.2f} ERG ({pct:+.1f}%)",
                        "result": f"{erg_back:.2f} ERG in wallet",
                        "blocked": False, "blocked_reason": "",
                    })
                else:
                    erg_options.append({"name": "Spectrum buy -> Bank redeem", "steps": [], "profit_pct": 0,
                        "profit_desc": "", "result": "", "blocked": True, "blocked_reason": "Bank redeem BLOCKED"})

            # 3. Sell ERG on NonKYC
            if nonkyc_price:
                usdt_out = erg * nonkyc_price * (1 - config.NONKYC_TRADING_FEE)
                erg_options.append({
                    "name": "Sell on NonKYC",
                    "steps": [
                        f"Deposit {erg:.2f} ERG to NonKYC (free)",
                        f"Sell on NonKYC: {erg:.2f} ERG -> ${usdt_out:.2f} USDT (${nonkyc_price:.4f}/ERG, -{config.NONKYC_TRADING_FEE*100:.1f}% fee)",
                    ],
                    "profit_pct": 0,
                    "profit_desc": f"${usdt_out:.2f} USDT (withdraw fee: {nonkyc_usdt_fee} USDT)",
                    "result": f"${usdt_out:.2f} USDT on NonKYC",
                    "blocked": False, "blocked_reason": "exit to USDT",
                })

            # 4. Sell ERG on Kucoin
            if kucoin_price:
                usdt_out = erg * kucoin_price * (1 - config.KUCOIN_TRADING_FEE)
                erg_options.append({
                    "name": "Sell on Kucoin",
                    "steps": [
                        f"Deposit {erg:.2f} ERG to Kucoin (free)",
                        f"Sell on Kucoin: {erg:.2f} ERG -> ${usdt_out:.2f} USDT (${kucoin_price:.4f}/ERG, -{config.KUCOIN_TRADING_FEE*100:.1f}% fee)",
                    ],
                    "profit_pct": 0,
                    "profit_desc": f"${usdt_out:.2f} USDT (withdraw fee: {kucoin_usdt_fee} USDT)",
                    "result": f"${usdt_out:.2f} USDT on Kucoin",
                    "blocked": False, "blocked_reason": "exit to USDT",
                })

        # ===================== SIGUSD OPTIONS =====================
        # Baseline: SigUSD at oracle rate (no fees) = sigusd / oracle_price ERG
        if sigusd > 0.5 and oracle_price and oracle_price > 0:
            baseline_erg = sigusd / oracle_price

            # 1. Bank redeem (SigUSD -> ERG)
            if can_redeem:
                erg_out = self._bank_redeem_erg(bank_state, sigusd)[1] - config.SIGMAUSD_REDEEM_EXTRA_ERG
                pct = ((erg_out - baseline_erg) / baseline_erg) * 100
                sigusd_options.append({
                    "name": "Bank redeem",
                    "steps": [
                        f"Redeem at Bank: {sigusd:.2f} SigUSD -> {erg_out:.2f} ERG (oracle ${oracle_price:.4f}, -{bank_fee_pct:.2f}% bank fee, -{config.SIGMAUSD_REDEEM_EXTRA_ERG} ERG extra)",
                    ],
                    "profit_pct": pct,
                    "profit_desc": f"{erg_out:.2f} ERG ({pct:+.1f}% vs oracle baseline {baseline_erg:.2f} ERG)",
                    "result": f"{erg_out:.2f} ERG in wallet",
                    "blocked": False, "blocked_reason": "",
                })
            else:
                sigusd_options.append({"name": "Bank redeem", "steps": [], "profit_pct": 0,
                    "profit_desc": "", "result": "", "blocked": True, "blocked_reason": "Bank redeem BLOCKED"})

            # 2. Spectrum swap (SigUSD -> ERG)
            if spectrum_price:
                erg_before_fees = self._dex_sigusd_to_erg(prices, sigusd)
                erg_out = erg_before_fees - config.pool_service_fee() - config.ERGO_TX_FEE
                pct = ((erg_out - baseline_erg) / baseline_erg) * 100
                sigusd_options.append({
                    "name": "Spectrum swap",
                    "steps": [
                        f"Swap on Spectrum: {sigusd:.2f} SigUSD -> {erg_out:.2f} ERG (-0.5% pool fee, {config.pool_fee_text()}, -{config.ERGO_TX_FEE} ERG network)",
                    ],
                    "profit_pct": pct,
                    "profit_desc": f"{erg_out:.2f} ERG ({pct:+.1f}% vs oracle baseline {baseline_erg:.2f} ERG)",
                    "result": f"{erg_out:.2f} ERG in wallet",
                    "blocked": False, "blocked_reason": "",
                })

            # 3. Spectrum -> Kucoin (SigUSD -> ERG -> USDT)
            if spectrum_price and kucoin_price:
                erg_before_fees = self._dex_sigusd_to_erg(prices, sigusd)
                erg_from_dex = erg_before_fees - config.pool_service_fee() - config.ERGO_TX_FEE
                if erg_from_dex > 0:
                    usdt_out = erg_from_dex * kucoin_price * (1 - config.KUCOIN_TRADING_FEE)
                    # Profit: compare USDT out to what SigUSD "should be worth" ($1 per SigUSD if pegged)
                    baseline_usdt = sigusd  # 1 SigUSD should = $1
                    pct_usdt = ((usdt_out - baseline_usdt) / baseline_usdt) * 100
                    sigusd_options.append({
                        "name": "Spectrum -> Kucoin",
                        "steps": [
                            f"Swap on Spectrum: {sigusd:.2f} SigUSD -> {erg_from_dex:.2f} ERG (-0.5% pool fee, {config.pool_fee_text()}, -{config.ERGO_TX_FEE} ERG network)",
                            f"Deposit {erg_from_dex:.2f} ERG to Kucoin (free)",
                            f"Sell on Kucoin: {erg_from_dex:.2f} ERG -> ${usdt_out:.2f} USDT (${kucoin_price:.4f}/ERG, -{config.KUCOIN_TRADING_FEE*100:.1f}% fee)",
                        ],
                        "profit_pct": pct_usdt,
                        "profit_desc": f"${usdt_out:.2f} USDT from {sigusd:.2f} SigUSD ({pct_usdt:+.1f}% vs $1 peg, withdraw fee: {kucoin_usdt_fee} USDT)",
                        "result": f"${usdt_out:.2f} USDT on Kucoin",
                        "blocked": False, "blocked_reason": "",
                    })

            # 4. Spectrum -> NonKYC (SigUSD -> ERG -> USDT)
            if spectrum_price and nonkyc_price:
                erg_before_fees = self._dex_sigusd_to_erg(prices, sigusd)
                erg_from_dex = erg_before_fees - config.pool_service_fee() - config.ERGO_TX_FEE
                if erg_from_dex > 0:
                    usdt_out = erg_from_dex * nonkyc_price * (1 - config.NONKYC_TRADING_FEE)
                    baseline_usdt = sigusd
                    pct_usdt = ((usdt_out - baseline_usdt) / baseline_usdt) * 100
                    sigusd_options.append({
                        "name": "Spectrum -> NonKYC",
                        "steps": [
                            f"Swap on Spectrum: {sigusd:.2f} SigUSD -> {erg_from_dex:.2f} ERG (-0.5% pool fee, {config.pool_fee_text()}, -{config.ERGO_TX_FEE} ERG network)",
                            f"Deposit {erg_from_dex:.2f} ERG to NonKYC (free)",
                            f"Sell on NonKYC: {erg_from_dex:.2f} ERG -> ${usdt_out:.2f} USDT (${nonkyc_price:.4f}/ERG, -{config.NONKYC_TRADING_FEE*100:.1f}% fee)",
                        ],
                        "profit_pct": pct_usdt,
                        "profit_desc": f"${usdt_out:.2f} USDT from {sigusd:.2f} SigUSD ({pct_usdt:+.1f}% vs $1 peg, withdraw fee: {nonkyc_usdt_fee} USDT)",
                        "result": f"${usdt_out:.2f} USDT on NonKYC",
                        "blocked": False, "blocked_reason": "",
                    })

            # 5. Bank redeem -> Kucoin (SigUSD -> ERG via bank -> USDT)
            if can_redeem and kucoin_price:
                erg_out = self._bank_redeem_erg(bank_state, sigusd)[1] - config.SIGMAUSD_REDEEM_EXTRA_ERG
                if erg_out > 0:
                    usdt_out = erg_out * kucoin_price * (1 - config.KUCOIN_TRADING_FEE)
                    baseline_usdt = sigusd
                    pct_usdt = ((usdt_out - baseline_usdt) / baseline_usdt) * 100
                    sigusd_options.append({
                        "name": "Bank redeem -> Kucoin",
                        "steps": [
                            f"Redeem at Bank: {sigusd:.2f} SigUSD -> {erg_out:.2f} ERG (oracle ${oracle_price:.4f}, -{bank_fee_pct:.2f}% bank fee)",
                            f"Deposit {erg_out:.2f} ERG to Kucoin (free)",
                            f"Sell on Kucoin: {erg_out:.2f} ERG -> ${usdt_out:.2f} USDT (${kucoin_price:.4f}/ERG, -{config.KUCOIN_TRADING_FEE*100:.1f}% fee)",
                        ],
                        "profit_pct": pct_usdt,
                        "profit_desc": f"${usdt_out:.2f} USDT from {sigusd:.2f} SigUSD ({pct_usdt:+.1f}% vs $1 peg, withdraw fee: {kucoin_usdt_fee} USDT)",
                        "result": f"${usdt_out:.2f} USDT on Kucoin",
                        "blocked": False, "blocked_reason": "",
                    })

            # 6. Bank redeem -> NonKYC (SigUSD -> ERG via bank -> USDT)
            if can_redeem and nonkyc_price:
                erg_out = self._bank_redeem_erg(bank_state, sigusd)[1] - config.SIGMAUSD_REDEEM_EXTRA_ERG
                if erg_out > 0:
                    usdt_out = erg_out * nonkyc_price * (1 - config.NONKYC_TRADING_FEE)
                    baseline_usdt = sigusd
                    pct_usdt = ((usdt_out - baseline_usdt) / baseline_usdt) * 100
                    sigusd_options.append({
                        "name": "Bank redeem -> NonKYC",
                        "steps": [
                            f"Redeem at Bank: {sigusd:.2f} SigUSD -> {erg_out:.2f} ERG (oracle ${oracle_price:.4f}, -{bank_fee_pct:.2f}% bank fee)",
                            f"Deposit {erg_out:.2f} ERG to NonKYC (free)",
                            f"Sell on NonKYC: {erg_out:.2f} ERG -> ${usdt_out:.2f} USDT (${nonkyc_price:.4f}/ERG, -{config.NONKYC_TRADING_FEE*100:.1f}% fee)",
                        ],
                        "profit_pct": pct_usdt,
                        "profit_desc": f"${usdt_out:.2f} USDT from {sigusd:.2f} SigUSD ({pct_usdt:+.1f}% vs $1 peg, withdraw fee: {nonkyc_usdt_fee} USDT)",
                        "result": f"${usdt_out:.2f} USDT on NonKYC",
                        "blocked": False, "blocked_reason": "",
                    })

        # ===================== USE OPTIONS =====================
        # USE -> ERG is the first hop, then ERG unlocks more paths
        use_lp = prices.get("use_lp") if self.enable_use else None
        if use > 0.01:
            if oracle_price and oracle_price > 0 and use_lp is not None:
                baseline_usdt = use  # 1 USE ~= $1
                oracle_erg = use / oracle_price  # value at the oracle rate
                erg_before_fees = use_lp.swap_output(use, input_is_x=False)
                erg_from_crux = erg_before_fees - config.SPECTRUM_EXECUTION_FEE - config.ERGO_TX_FEE
                crux_step = (
                    f"Swap via Crux on the Dexy LP: {use:.3f} USE -> {erg_from_crux:.2f} ERG "
                    f"(-0.3% LP fee, -{config.SPECTRUM_EXECUTION_FEE} ERG service, -{config.ERGO_TX_FEE} ERG network)"
                )

                if erg_from_crux > 0:
                    # 1. USE -> ERG (keep)
                    pct = ((erg_from_crux - oracle_erg) / oracle_erg) * 100
                    use_options.append({
                        "name": "Crux -> keep ERG",
                        "steps": [crux_step],
                        "profit_pct": pct,
                        "profit_desc": f"{erg_from_crux:.2f} ERG ({pct:+.1f}% vs oracle value {oracle_erg:.2f} ERG)",
                        "result": f"{erg_from_crux:.2f} ERG in wallet",
                        "blocked": False, "blocked_reason": "",
                    })

                    # 2. USE -> ERG -> sell on Kucoin -> USDT
                    if kucoin_price:
                        usdt_out = erg_from_crux * kucoin_price * (1 - config.KUCOIN_TRADING_FEE)
                        pct_usdt = ((usdt_out - baseline_usdt) / baseline_usdt) * 100
                        use_options.append({
                            "name": "Crux -> Kucoin",
                            "steps": [
                                crux_step,
                                f"Deposit {erg_from_crux:.2f} ERG to Kucoin (free)",
                                f"Sell on Kucoin: {erg_from_crux:.2f} ERG -> ${usdt_out:.2f} USDT (${kucoin_price:.4f}/ERG, -{config.KUCOIN_TRADING_FEE*100:.1f}% fee)",
                            ],
                            "profit_pct": pct_usdt,
                            "profit_desc": f"${usdt_out:.2f} USDT from {use:.3f} USE ({pct_usdt:+.1f}% vs $1 peg, withdraw fee: {kucoin_usdt_fee} USDT)",
                            "result": f"${usdt_out:.2f} USDT on Kucoin",
                            "blocked": False, "blocked_reason": "",
                        })

                    # 3. USE -> ERG -> sell on NonKYC -> USDT
                    if nonkyc_price:
                        usdt_out = erg_from_crux * nonkyc_price * (1 - config.NONKYC_TRADING_FEE)
                        pct_usdt = ((usdt_out - baseline_usdt) / baseline_usdt) * 100
                        use_options.append({
                            "name": "Crux -> NonKYC",
                            "steps": [
                                crux_step,
                                f"Deposit {erg_from_crux:.2f} ERG to NonKYC (free)",
                                f"Sell on NonKYC: {erg_from_crux:.2f} ERG -> ${usdt_out:.2f} USDT (${nonkyc_price:.4f}/ERG, -{config.NONKYC_TRADING_FEE*100:.1f}% fee)",
                            ],
                            "profit_pct": pct_usdt,
                            "profit_desc": f"${usdt_out:.2f} USDT from {use:.3f} USE ({pct_usdt:+.1f}% vs $1 peg, withdraw fee: {nonkyc_usdt_fee} USDT)",
                            "result": f"${usdt_out:.2f} USDT on NonKYC",
                            "blocked": False, "blocked_reason": "",
                        })

                    # 4. USE -> ERG -> Spectrum buy SigUSD -> Bank redeem -> ERG (arb loop)
                    if spectrum_price and can_redeem:
                        sigusd_from_spectrum = self._dex_erg_to_sigusd(prices, erg_from_crux)
                        erg_hop2 = self._bank_redeem_erg(bank_state, sigusd_from_spectrum)[1] - config.SIGMAUSD_REDEEM_EXTRA_ERG
                        total_fees_hop2 = config.pool_service_fee() + config.ERGO_TX_FEE
                        erg_final = erg_hop2 - total_fees_hop2
                        net = erg_final - erg_from_crux
                        pct_arb = (net / erg_from_crux) * 100
                        use_options.append({
                            "name": "Crux -> Spectrum -> Bank redeem",
                            "steps": [
                                crux_step,
                                f"Swap ERG -> SigUSD on Spectrum: {erg_from_crux:.2f} ERG -> {sigusd_from_spectrum:.2f} SigUSD (-0.5% pool fee, {config.pool_fee_text()})",
                                f"Redeem at Bank: {sigusd_from_spectrum:.2f} SigUSD -> {erg_hop2:.2f} ERG (oracle ${oracle_price:.4f}, -{bank_fee_pct:.2f}% bank fee)",
                                f"Extra fees: -{config.SIGMAUSD_REDEEM_EXTRA_ERG} ERG (receipt + miner), -{config.ERGO_TX_FEE} ERG network",
                            ],
                            "profit_pct": pct_arb,
                            "profit_desc": f"{erg_final:.2f} ERG from {erg_from_crux:.2f} ERG ({pct_arb:+.1f}%)",
                            "result": f"{erg_final:.2f} ERG in wallet",
                            "blocked": False, "blocked_reason": "",
                        })

                    # 5. USE -> ERG -> Bank mint SigUSD -> Spectrum sell -> ERG (if mint available)
                    use_mint_cents, use_mint_ok = self._bank_mint(bank_state, erg_from_crux)
                    if spectrum_price and use_mint_ok:
                        sigusd_from_bank = use_mint_cents / 100
                        erg_from_spectrum = self._dex_sigusd_to_erg(prices, sigusd_from_bank)
                        total_fees_hop2 = config.pool_service_fee() + config.ERGO_TX_FEE * 2
                        erg_final = erg_from_spectrum - total_fees_hop2
                        net = erg_final - erg_from_crux
                        pct_arb = (net / erg_from_crux) * 100
                        use_options.append({
                            "name": "Crux -> Bank mint -> Spectrum sell",
                            "steps": [
                                crux_step,
                                f"Mint SigUSD at Bank: {erg_from_crux:.2f} ERG -> {sigusd_from_bank:.2f} SigUSD (oracle ${oracle_price:.4f}, -{bank_fee_pct:.2f}% bank fee)",
                                f"Swap SigUSD -> ERG on Spectrum: {sigusd_from_bank:.2f} SigUSD -> {erg_from_spectrum:.2f} ERG (-0.5% pool fee, {config.pool_fee_text()})",
                                f"Network fees: -{config.ERGO_TX_FEE * 2} ERG (2 txns)",
                            ],
                            "profit_pct": pct_arb,
                            "profit_desc": f"{erg_final:.2f} ERG from {erg_from_crux:.2f} ERG ({pct_arb:+.1f}%)",
                            "result": f"{erg_final:.2f} ERG in wallet",
                            "blocked": False, "blocked_reason": "",
                        })
                    elif spectrum_price and bank_state:
                        rr = prices.get('bank', {}).get('reserve_ratio', 0)
                        use_options.append({"name": "Crux -> Bank mint -> Spectrum sell", "steps": [], "profit_pct": 0,
                            "profit_desc": "", "result": "", "blocked": True,
                            "blocked_reason": f"Bank mint BLOCKED (RR={rr:.0f}%)"})
                else:
                    use_options.append({
                        "name": "Crux LP swap",
                        "steps": [f"Not worth it: {config.SPECTRUM_EXECUTION_FEE} ERG service fee > {erg_before_fees:.2f} ERG value"],
                        "profit_pct": -100,
                        "profit_desc": f"Balance too small (0.785 ERG fee > {erg_before_fees:.2f} ERG value)",
                        "result": "",
                        "blocked": False, "blocked_reason": "too small",
                    })

        return {
            "erg": {"balance": erg, "options": erg_options},
            "sigusd": {"balance": sigusd, "options": sigusd_options},
            "use": {"balance": use, "options": use_options},
        }

    def _display_wallet_opportunities(self, wallet: dict, opportunities: list, prices: dict):
        """Show what's possible with current wallet holdings."""
        erg = wallet.get("erg", 0)
        sigusd = wallet.get("sigusd", 0)
        use = wallet.get("use", 0)

        self.out.print()
        self.out.print(Panel(
            f"[bold]ERG:[/bold] {erg:.4f}  |  [bold]SigUSD:[/bold] {sigusd:.2f}  |  [bold]USE:[/bold] {use:.3f}",
            title="Wallet Balances",
            border_style="cyan",
        ))

        analysis = self._build_wallet_analysis(wallet, prices)

        for asset_key, label in [("erg", "ERG"), ("sigusd", "SigUSD"), ("use", "USE")]:
            info = analysis[asset_key]
            balance = info["balance"]
            options = info["options"]

            minimum, unit, fmt = WALLET_MINIMUMS[asset_key]
            if balance <= 0:
                self.out.print(f"  [dim]{label}: No balance[/dim]")
                continue
            if balance < minimum:
                self.out.print(f"  [dim]{label}: {balance:{fmt}} (below {minimum:g} {unit} minimum for path analysis)[/dim]")
                continue

            if not options:
                self.out.print(f"  [dim]{label}: No paths available[/dim]")
                continue

            available = [o for o in options if not o["blocked"] and o.get("blocked_reason") != "exit to USDT"]
            usdt_exits = [o for o in options if not o["blocked"] and o.get("blocked_reason") == "exit to USDT"]
            blocked = [o for o in options if o["blocked"]]

            profitable = [o for o in available if o["profit_pct"] > 0.5]

            # Header line
            if profitable:
                best = max(profitable, key=lambda x: x["profit_pct"])
                self.out.print(f"\n  [bold green]{label}: {len(profitable)} profitable option(s). Best: {best['name']} ({best['profit_pct']:+.1f}%)[/bold green]")
            elif available:
                best = max(available, key=lambda x: x["profit_pct"])
                if best.get("blocked_reason") == "too small":
                    self.out.print(f"\n  [yellow]{label}: {best['profit_desc']}[/yellow]")
                else:
                    self.out.print(f"\n  [yellow]{label}: No profitable options. Best: {best['name']} ({best['profit_pct']:+.1f}%)[/yellow]")
            else:
                reasons = ", ".join(o["blocked_reason"] for o in blocked if o["blocked_reason"])
                self.out.print(f"\n  [dim]{label}: All paths blocked ({reasons})[/dim]")

            # Show each option with steps
            all_available = sorted(available, key=lambda x: x["profit_pct"], reverse=True)
            for o in all_available:
                pct = o["profit_pct"]
                tag = "[bold green]" if pct > 0.5 else "[yellow]" if pct > -1 else "[dim]"
                end_tag = "[/bold green]" if pct > 0.5 else "[/yellow]" if pct > -1 else "[/dim]"

                self.out.print(f"    {tag}{o['name']} ({pct:+.1f}%):{end_tag}")
                for step in o["steps"]:
                    self.out.print(f"      {tag}{step}{end_tag}")
                self.out.print(f"      {tag}-> {o['profit_desc']}{end_tag}")

            # Show USDT exit options (ERG only)
            for o in usdt_exits:
                self.out.print(f"    [dim]{o['name']}: {o['profit_desc']}[/dim]")

            # Show blocked
            for o in blocked:
                self.out.print(f"    [dim]-- {o['name']}: {o['blocked_reason']}[/dim]")

    def _should_send_wallet_analysis(self, opportunities: list[ArbitrageOpportunity]) -> bool:
        """Check if wallet analysis should be sent to Discord."""
        now = time.time()
        elapsed = now - self._last_wallet_analysis_time

        # Periodic: every WALLET_COOLDOWN_SECONDS
        if elapsed >= config.DISCORD_WALLET_COOLDOWN_SECONDS:
            return True

        # Triggered: when a NEW opportunity just hit confirmation threshold
        for opp in opportunities:
            if not opp.is_profitable or opp.blocked:
                continue
            path_key = opp.path_key
            streak = self._opportunity_streak.get(path_key, 0)
            if streak == config.DISCORD_CONFIRM_SCANS:
                return True

        return False

    async def scan_once(self):
        """Run a single scan cycle."""
        self.scan_count += 1
        self.out.rule(f"[header]Scan #{self.scan_count} - {datetime.now().strftime('%H:%M:%S')}[/header]")

        prices = await self.fetch_all_prices()
        outage = self._chain_error is not None
        if outage:  # prices below are the last good state: show them, but do not log or alert on them
            self.out.print(f"[bold yellow]Chain state unavailable ({self._chain_error}): showing the last good "
                          f"state; nothing is logged, alerted or traded until the node can be read[/bold yellow]")
        else:
            self._last_snapshot_id = self.tracker.log_price_snapshot(prices)
        self._display_prices(prices)

        opportunities = self._find_opportunities(prices)
        self._update_live_streak()
        if not outage:
            self.tracker.log_scan_results(opportunities, self.scan_count, self._last_snapshot_id)
            self.tracker.record_scan(opportunities, self.scan_count, self._last_snapshot_id)
        self._display_opportunities(opportunities, prices)
        if self.cex_watch:
            self._display_cex_watch(prices)

        # Wallet-based analysis
        wallet = await self._fetch_wallet_balances()
        self._last_wallet = wallet
        if self.show_wallet:
            self._display_wallet_opportunities(wallet, opportunities, prices)

        if not outage:
            await self._notify_discord(opportunities)
        if self.discord_enabled and self.cex_watch:
            await self._notify_cex_watch(prices)

        # Send wallet analysis to Discord (rate limited)
        if self.discord_enabled and self._should_send_wallet_analysis(opportunities):
            analysis = self._build_wallet_analysis(wallet, prices)
            await self.discord.send_wallet_analysis(wallet, analysis)
            self._last_wallet_analysis_time = time.time()
            logger.info("Wallet analysis sent to Discord")

        # Periodic summary heartbeat
        if self.discord_enabled:
            now = time.time()
            if (now - self._last_summary_time) >= config.DISCORD_SUMMARY_INTERVAL_SECONDS:
                await self.discord.send_scan_summary(opportunities, scan_number=self.scan_count)
                self._last_summary_time = now
                logger.info("Periodic summary sent to Discord")
        try:
            self._maybe_send_digest()
        except Exception as e:  # the digest must never block a scan or a trade
            logger.error(f"Daily digest error: {e}", exc_info=True)

        await self._execute_trades(wallet, prices)

        profitable_count = sum(1 for o in opportunities if o.is_profitable and not o.blocked)
        self.tracker.update_daily_summary(profitable_count)
        logger.info(
            f"Scan #{self.scan_count}: {len(opportunities)} paths analyzed, "
            f"{profitable_count} profitable"
        )

    def _note_change(self):
        """One console line when the pool, bank or oracle box changes."""
        snap = self._snapshot
        if snap is None or self._chain_error is not None or snap.key == self._last_key:
            return
        names = ("pool", "bank", "oracle")
        changed = [n for n, a, b in zip(names, snap.key, self._last_key or (None,) * 3) if a != b]
        pending = f" pending: {', '.join(sorted(snap.pending))}" if snap.pending else ""
        best = " | ".join(f"{LIVE_PATHS[k]}: {c.summary()}" for k, c in self.last_sizing.items())
        self.out.print(f"[dim]{datetime.now().strftime('%H:%M:%S')} CHAIN h{snap.height} "
                      f"changed: {', '.join(changed)}{pending} ({snap.read_ms:.0f} ms) | {best}[/dim]")
        self.state.add_event("info", f"CHAIN h{snap.height} changed: {', '.join(changed)}{pending}")
        self._last_key = snap.key

    async def poll_once(self, now: float):
        """One tick: full scan when due, otherwise a fast node read, exact sizing and the live gate.
        The dashboard state is refreshed exactly once per tick (also when the tick fails)."""
        self._now = now
        full = now - self._last_full_scan >= config.SCAN_INTERVAL_SECONDS
        try:
            await self._poll(now)
        finally:
            self._refresh_state(now)
            self._discord_tick(now)
        if full and self.view == "json":
            sys.stdout.write(json.dumps(self.state.to_json(), default=str) + "\n")
            sys.stdout.flush()

    def _refresh_state(self, now: float):
        """Fill self.state (what the dashboard and --json show) from the scanner's current view."""
        s = self.state
        snap = self._snapshot
        prices = self._chain_prices or {}
        s.scan_count, s.chain_error = self.scan_count, self._chain_error
        s.node_ok = self._chain_error is None and snap is not None
        s.height = snap.height if snap else s.height
        s.read_ms = snap.read_ms if snap else None
        s.next_full_scan_in = max(0.0, config.SCAN_INTERVAL_SECONDS - (now - self._last_full_scan))
        s.prices = prices
        s.mint_text = mint_gate_text(prices.get("bank"), config.MINT_GATE_MIN_ROOM_ERG)
        s.update_venues(describe_all(VenueContext(
            prices={**self._last_prices, **prices}, timestamps=self._price_timestamps, now=time.time(),
            chain_error=self._chain_error, pending=snap.pending if snap else frozenset(), read_ms=s.read_ms,
            enable_cex=self.enable_cex, enable_use=self.enable_use, cex_watch=self.cex_watch)))
        steps = {k: self.last_optima[k].steps for k in PATH_LABELS if k in self.last_optima}
        s.update_paths(self.last_sizing, self._live_streak, self._chain_error, steps)
        wallet = self._last_wallet
        s.wallet_ok = wallet is not None
        if wallet is not None and self.show_wallet:
            value = self._wallet_value_erg(wallet, prices) if prices else None
            oracle = (prices.get("bank") or {}).get("oracle_erg_usd")
            s.wallet = dict(wallet, value_erg=value, value_usd=value * oracle if value and oracle else None)
        s.trades_today = self._trades_today_count()
        if self._live_start_value is not None and wallet is not None and wallet.get("ok", True) and prices:
            s.drawdown = max(0.0, self._live_start_value - self._wallet_value_erg(wallet, prices))
        if not self.trading_enabled:
            s.set_live("off", [f"{self.mode} mode: no trading"])
        elif self._live_paused:
            s.set_live("paused", [self._live_paused, "restart to resume"])
        else:
            # Global reasons first (they block every path), then the closest path's own reasons.
            reasons = self._sync_global_blockers(wallet, prices)
            if self._health_blocker:
                reasons.append(self._health_blocker)
            ready = [k for k in LIVE_PATHS if not self._path_blockers(k, wallet or {})]
            if not ready:
                best = max(LIVE_PATHS, key=lambda k: self._live_streak.get(k, 0))
                reasons = reasons + self._path_blockers(best, wallet or {})
            s.set_live("blocked" if reasons else "armed", reasons)

    async def _poll(self, now: float):
        if now - self._last_full_scan >= config.SCAN_INTERVAL_SECONDS:
            self._last_full_scan = now
            await self.scan_once()
            self._note_change()
            return
        snap = await self._read_chain()
        if snap is None:
            self._update_live_streak()  # resets streaks while the chain is unreadable
            return
        prices = self._chain_prices
        self.last_optima = self._optimize_sizes(prices)
        self._update_live_streak()
        self._note_change()
        if self.trading_enabled and any(self._live_streak.get(k, 0) >= config.LIVE_CONFIRM_POLLS
                                        for k in LIVE_PATHS):
            wallet = await self._fetch_wallet_balances()
            self._last_wallet = wallet
            await self._execute_trades(wallet, prices, quiet=True)

    def request_stop(self):
        """Ask the main loop to finish the current scan and shut down cleanly."""
        self._stop.set()

    def _install_signal_handlers(self) -> list:
        """Route SIGINT/SIGTERM to request_stop. Returns (signal, previous handler) pairs to restore."""
        loop = asyncio.get_running_loop()
        restore = []
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, self.request_stop)
                restore.append((sig, None))
            except (NotImplementedError, RuntimeError, ValueError):
                # Windows: no loop signal handlers; use signal.signal from the main thread
                try:
                    previous = signal.signal(sig, lambda *_: loop.call_soon_threadsafe(self.request_stop))
                    restore.append((sig, previous))
                except ValueError:
                    pass  # not the main thread
        return restore

    @staticmethod
    def _restore_signal_handlers(restore: list):
        loop = asyncio.get_running_loop()
        for sig, previous in restore:
            if previous is None:
                loop.remove_signal_handler(sig)
            else:
                signal.signal(sig, previous)

    async def run(self, once: bool = False):
        """Main scan loop: fixed-rate scans until request_stop() or SIGINT/SIGTERM."""
        mode_display = {
            "monitor": "MONITOR ONLY (console output)",
            "notify": "NOTIFICATION (console + Discord alerts)",
            "live": "LIVE TRADING (console + Discord + auto-execute)",
        }.get(self.mode, self.mode.upper())

        discord_line = ""
        if self.discord_enabled:
            discord_line = (
                f"Discord: ENABLED (min {config.DISCORD_MIN_PROFIT_PERCENT}%/{config.DISCORD_MIN_PROFIT_ERG} ERG, "
                f"cooldown {config.DISCORD_COOLDOWN_SECONDS}s, confirm {config.DISCORD_CONFIRM_SCANS} scans)\n"
                f"Tier 1 ping: >={config.DISCORD_TIER1_PROFIT_PERCENT}% + no SigUSD=USDT assumption\n"
                f"Wallet analysis: every {config.DISCORD_WALLET_COOLDOWN_SECONDS}s | Summary: every {config.DISCORD_SUMMARY_INTERVAL_SECONDS}s"
            )
            if config.DISCORD_USER_ID:
                discord_line += f"\nPinging user: <@{config.DISCORD_USER_ID}>"
        elif self.mode in ("notify", "live"):
            discord_line = "Discord: NOT CONFIGURED (set DISCORD_WEBHOOK_URL in .env)"
        else:
            discord_line = "Discord: DISABLED (use --notify or --live to enable)"

        self.out.print(Panel(
            "[bold]Ergo Arbitrage Scanner[/bold]\n"
            f"Mode: {mode_display}\n"
            f"Min profit: {config.MIN_PROFIT_PERCENT}%\n"
            f"Max trade size: {config.MAX_TRADE_SIZE_ERG} ERG\n"
            f"Chain poll: {config.CHAIN_POLL_SECONDS:g}s (node, mempool-aware) | full scan: {config.SCAN_INTERVAL_SECONDS}s\n"
            f"Trade sizes: {self._trade_sizes}"
            f"{' (all priced)' if config.TRADE_SIZES_UNFUNDED else ', priced up to what the wallet can fund'}\n"
            f"Venues: {'on-chain + CEX' if self.enable_cex else 'on-chain only (ENABLE_CEX=false)'}"
            f"{'' if self.enable_use else ', USE disabled (ENABLE_USE=false)'}"
            f"{', CEX watch-only' if self.cex_watch else ''}\n"
            f"{discord_line}",
            title="Starting Up",
            border_style="red" if self.mode == "live" else "magenta",
        ))

        for warning in config.deprecated_settings():
            logger.warning(warning)
        restore = self._install_signal_handlers()
        await self.connect_all()

        pruned = self.tracker.prune_scan_results(config.SCAN_RESULTS_RETENTION_DAYS)
        if pruned:
            logger.info(f"Pruned {pruned} non-profitable scan rows older than {config.SCAN_RESULTS_RETENTION_DAYS} days")

        if self.discord_enabled:
            await self.discord.send_startup_message(mode=self.mode)
            if not once:  # a one-off run must leave a running bot's open messages alone
                self._close_stale_discord_messages()

        try:
            loop = asyncio.get_running_loop()
            next_tick = loop.time()
            while not self._stop.is_set():
                try:
                    await self.poll_once(loop.time())
                except Exception as e:
                    logger.error(f"Poll error: {e}", exc_info=True)
                if once:
                    break
                next_tick = max(next_tick + config.CHAIN_POLL_SECONDS, loop.time())
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=next_tick - loop.time())
                except asyncio.TimeoutError:
                    pass
            self.out.print("\n[bold yellow]Shutting down...[/bold yellow]")
        finally:
            self._restore_signal_handlers(restore)
            if self.view == "plain":
                self.tracker.print_summary()
            if self.discord_enabled:
                self._close_episodes_on_shutdown()
            if self.discord_enabled:
                await self.discord.send_summary_message(self.tracker.get_session_stats())
            await self.disconnect_all()
