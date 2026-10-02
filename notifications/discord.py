import asyncio
import logging
import time
from datetime import datetime
from typing import Optional

import aiohttp

import config
from arbitrage.calculator import ArbitrageOpportunity

logger = logging.getLogger("ergo_arb.discord")


class DiscordNotifier:
    """Send arbitrage opportunity notifications to a Discord webhook."""

    def __init__(self):
        self.webhook_url: str = config.DISCORD_WEBHOOK_URL
        self.user_id: str = config.DISCORD_USER_ID
        self.enabled: bool = config.DISCORD_ENABLED
        self.min_profit_percent: float = config.DISCORD_MIN_PROFIT_PERCENT
        self.cooldown_seconds: int = config.DISCORD_COOLDOWN_SECONDS
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_notified: dict[str, float] = {}

    def _ping(self) -> str:
        if self.user_id:
            return f"<@{self.user_id}>"
        return ""

    def _classify_tier(self, opp: ArbitrageOpportunity) -> int:
        """Classify opportunity into notification tier.

        Tier 1 (ping): >= TIER1_PROFIT_PERCENT AND >= MIN_PROFIT_ERG AND no SigUSD=USDT assumption.
        Tier 2 (silent): Passes filters but below Tier 1 thresholds.
        Returns 0 if should not notify at all.
        """
        if opp.profit_percent < self.min_profit_percent:
            return 0
        if opp.profit_erg < config.DISCORD_MIN_PROFIT_ERG:
            return 0

        has_assumption = opp.assumption and "SigUSD" in opp.assumption
        if (opp.profit_percent >= config.DISCORD_TIER1_PROFIT_PERCENT
                and opp.profit_erg >= config.DISCORD_MIN_PROFIT_ERG
                and not has_assumption):
            return 1
        return 2

    async def connect(self):
        if self.enabled and self._session is None:
            self._session = aiohttp.ClientSession()
            logger.info("Discord notifier connected")

    async def disconnect(self):
        if self._session:
            await self._session.close()
            self._session = None

    def _get_path_key(self, opp: ArbitrageOpportunity) -> str:
        path = opp.path
        if " [" in path:
            path = path.rsplit(" [", 1)[0]
        return path

    def _is_on_cooldown(self, path_key: str) -> bool:
        last_time = self._last_notified.get(path_key, 0)
        return (time.time() - last_time) < self.cooldown_seconds

    def _record_notification(self, path_key: str):
        self._last_notified[path_key] = time.time()

    async def _send(self, content: str) -> bool:
        """Send a plain text message to the webhook."""
        try:
            if self._session is None:
                await self.connect()

            async with self._session.post(
                self.webhook_url,
                json={"content": content},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 204:
                    return True
                elif resp.status == 429:
                    body = await resp.json()
                    retry_after = body.get("retry_after", 5)
                    logger.warning(f"Discord rate limited, retry after {retry_after}s")
                    return False
                else:
                    text = await resp.text()
                    logger.error(f"Discord webhook failed (HTTP {resp.status}): {text[:200]}")
                    return False
        except asyncio.TimeoutError:
            logger.error("Discord webhook timed out")
            return False
        except Exception as e:
            logger.error(f"Discord webhook error: {e}")
            return False

    async def notify_live(self, text: str, ping: bool = True) -> bool:
        """Live-trading event (trade executed, failure, pause)."""
        prefix = f"{self._ping()} " if ping and self._ping() else ""
        return await self._send(f"{prefix}**LIVE**: {text}")

    async def notify_watch(self, exchange: str, text: str) -> bool:
        """Watch-only CEX gap alert, at most once per CEX_WATCH_COOLDOWN_SECONDS per exchange."""
        key = f"watch:{exchange}"
        if time.time() - self._last_notified.get(key, 0) < config.CEX_WATCH_COOLDOWN_SECONDS:
            return False
        content = f"**CEX watch-only: {exchange}**\n{text}\nNot executable: {exchange} is not connected (no API keys)."
        sent = await self._send(content)
        if sent:
            self._last_notified[key] = time.time()
        return sent

    def _format_opportunity(self, opp: ArbitrageOpportunity, scan_number: int = 0, tier: int = 1) -> str:
        """Format an opportunity as a clear action-oriented message."""
        path_key = self._get_path_key(opp)
        status = "GO" if opp.risk_adjusted_profitable else "RISKY"
        fees = opp.fees
        now = datetime.now().strftime("%H:%M:%S")

        lines = []
        if tier == 1:
            ping = self._ping()
            if ping:
                lines.append(f"{ping} **Arbitrage Opportunity Found**")
            else:
                lines.append("**Arbitrage Opportunity Found**")
        else:
            lines.append("**Arbitrage Opportunity (silent)**")

        lines.append("```")
        lines.append(f"Path:    {path_key}")
        lines.append(f"Status:  {status}")
        lines.append(f"Scan:    #{scan_number}  |  Time: {now}")
        lines.append("")

        # Trade flow - step by step
        if opp.steps:
            lines.append("Trade Flow:")
            step_num = 0
            for step in opp.steps:
                if step.startswith("WARNING:"):
                    continue  # shown separately below
                elif step.startswith("START:"):
                    lines.append(f"  {step}")
                elif step.startswith("END RESULT:"):
                    lines.append(f"  {step}")
                else:
                    step_num += 1
                    lines.append(f"  {step_num}. {step}")
            lines.append("")

        # Profit summary
        lines.append(f"{'Metric':<14s} {'Value'}")
        lines.append(f"{'-'*14} {'-'*20}")
        lines.append(f"{'Trade Size':<14s} {opp.input_erg:.0f} ERG")
        lines.append(f"{'Output':<14s} {opp.output_erg:.2f} ERG")
        lines.append(f"{'Profit ERG':<14s} {opp.profit_erg:+.2f} ERG")
        lines.append(f"{'Profit %':<14s} {opp.profit_percent:+.2f}%")
        lines.append(f"{'Profit USD':<14s} ${opp.profit_usd:+.2f}")
        lines.append("")

        # Fees
        total = f"{fees.total_fee_erg:.4f} ERG"
        if fees.total_fee_usd > 0:
            total += f" + ${fees.total_fee_usd:.2f}"
        lines.append(f"{'Total Fee:':<14s} {total}")
        lines.append(f"{'Exec Time:':<14s} ~{opp.estimated_execution_minutes:.0f} min")
        lines.append(f"{'Price Risk:':<14s} {opp.price_risk_percent:.1f}%")

        # SigUSD=USDT warning
        if opp.assumption and "SigUSD" in opp.assumption:
            lines.append("")
            lines.append("!! Assumes 1 SigUSD = 1 USDT (depegged!)")
            lines.append("   Real profit may differ significantly")

        lines.append("```")

        return "\n".join(lines)

    def _format_scan_summary(self, opportunities: list[ArbitrageOpportunity], scan_number: int = 0) -> str:
        """Format all paths as a compact summary table (like the console grid)."""
        now = datetime.now().strftime("%H:%M:%S")

        # Group by path, pick best per path
        best_per_path: dict[str, ArbitrageOpportunity] = {}
        for opp in opportunities:
            path_key = self._get_path_key(opp)
            existing = best_per_path.get(path_key)
            if existing is None or opp.profit_percent > existing.profit_percent:
                best_per_path[path_key] = opp

        sorted_paths = sorted(best_per_path.values(), key=lambda x: x.profit_percent, reverse=True)
        profitable_count = sum(1 for o in sorted_paths if o.is_profitable)

        lines = []
        lines.append(f"**Scan #{scan_number}** - {now}  |  {profitable_count} profitable paths")
        lines.append("```")
        lines.append(f"  {'Path':<28s} {'Size':>5s} {'Profit':>8s} {'ERG':>9s} {'USD':>7s} {'St':>5s}")
        lines.append(f"  {'-'*28} {'-'*5} {'-'*8} {'-'*9} {'-'*7} {'-'*5}")

        for opp in sorted_paths:
            path_key = self._get_path_key(opp)
            pname = path_key[:28]
            size = f"{opp.input_erg:.0f}"
            pct = f"{opp.profit_percent:+.2f}%"
            erg = f"{opp.profit_erg:+.4f}"
            usd = f"${opp.profit_usd:.2f}"
            if opp.is_profitable and opp.risk_adjusted_profitable:
                st = "GO"
            elif opp.is_profitable:
                st = "RISKY"
            else:
                st = "-"
            lines.append(f"  {pname:<28s} {size:>5s} {pct:>8s} {erg:>9s} {usd:>7s} {st:>5s}")

        lines.append("```")
        return "\n".join(lines)

    async def notify_opportunity(self, opp: ArbitrageOpportunity, scan_number: int = 0) -> bool:
        """Send a notification for a single profitable opportunity."""
        if not self.enabled:
            return False

        tier = self._classify_tier(opp)
        if tier == 0:
            return False

        path_key = self._get_path_key(opp)
        if self._is_on_cooldown(path_key):
            logger.debug(f"Discord notification skipped (cooldown): {path_key}")
            return False

        msg = self._format_opportunity(opp, scan_number, tier=tier)
        success = await self._send(msg)
        if success:
            self._record_notification(path_key)
            tier_label = "Tier 1 (ping)" if tier == 1 else "Tier 2 (silent)"
            logger.info(f"Discord notification sent [{tier_label}]: {path_key} +{opp.profit_percent:.2f}% ({opp.profit_erg:+.2f} ERG)")
        return success

    async def notify_opportunities(
        self,
        opportunities: list[ArbitrageOpportunity],
        scan_number: int = 0,
    ) -> int:
        """Send notifications for profitable opportunities (best per path)."""
        if not self.enabled or not opportunities:
            return 0

        best_per_path: dict[str, ArbitrageOpportunity] = {}
        for opp in opportunities:
            if not opp.is_profitable:
                continue
            path_key = self._get_path_key(opp)
            existing = best_per_path.get(path_key)
            if existing is None or opp.profit_percent > existing.profit_percent:
                best_per_path[path_key] = opp

        sent = 0
        for opp in best_per_path.values():
            result = await self.notify_opportunity(opp, scan_number)
            if result:
                sent += 1
                await asyncio.sleep(0.5)
        return sent

    async def send_scan_summary(
        self,
        opportunities: list[ArbitrageOpportunity],
        scan_number: int = 0,
    ):
        """Send a compact scan summary table (all paths, one row each)."""
        if not self.enabled or not opportunities:
            return
        msg = self._format_scan_summary(opportunities, scan_number)
        await self._send(msg)

    async def send_startup_message(self, mode: str = "notify"):
        """Send a message when the scanner starts up."""
        if not self.enabled:
            return

        mode_display = {
            "notify": "NOTIFICATION ONLY",
            "live": "LIVE TRADING",
            "monitor": "MONITOR ONLY",
        }.get(mode, mode.upper())

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ping = self._ping()
        header = f"{ping} " if ping else ""

        msg = f"{header}**Ergo Arbitrage Monitor Started**\n"
        msg += "```\n"
        msg += f"  Mode:          {mode_display}\n"
        msg += f"  Started:       {now}\n"
        msg += f"  Min Profit:    {config.DISCORD_MIN_PROFIT_PERCENT}% AND {config.DISCORD_MIN_PROFIT_ERG} ERG\n"
        msg += f"  Tier 1 Ping:   >= {config.DISCORD_TIER1_PROFIT_PERCENT}% (no SigUSD=USDT)\n"
        msg += f"  Confirm:       {config.DISCORD_CONFIRM_SCANS} scans ({config.DISCORD_CONFIRM_SCANS * config.SCAN_INTERVAL_SECONDS}s)\n"
        msg += f"  Max Trade:     {config.MAX_TRADE_SIZE_ERG} ERG\n"
        msg += f"  Scan Interval: {config.SCAN_INTERVAL_SECONDS}s\n"
        msg += f"  Cooldown:      {self.cooldown_seconds}s\n"
        msg += f"  Wallet:        every {config.DISCORD_WALLET_COOLDOWN_SECONDS}s\n"
        msg += f"  Summary:       every {config.DISCORD_SUMMARY_INTERVAL_SECONDS}s\n"
        msg += "```"

        success = await self._send(msg)
        if success:
            logger.info("Discord startup message sent")

    async def send_wallet_analysis(self, wallet: dict, analysis: dict):
        """Send wallet balance and per-asset analysis to Discord.

        Sends one message per asset to stay under Discord's 2000 char limit.
        """
        if not self.enabled:
            return

        erg = wallet.get("erg", 0)
        sigusd = wallet.get("sigusd", 0)
        use = wallet.get("use", 0)

        # Header message with balances
        header = f"**Wallet Analysis**\n```\n  ERG: {erg:.4f}  |  SigUSD: {sigusd:.2f}  |  USE: {use:.3f}\n```"
        await self._send(header)

        for asset_key, label in [("erg", "ERG"), ("sigusd", "SigUSD"), ("use", "USE")]:
            info = analysis[asset_key]
            balance = info["balance"]
            options = info["options"]

            if (asset_key == "erg" and balance < 2) or (asset_key == "sigusd" and balance < 0.5) or (asset_key == "use" and balance < 0.01):
                continue  # skip empty assets

            if not options:
                continue

            available = [o for o in options if not o["blocked"] and o.get("blocked_reason") != "exit to USDT"]
            blocked = [o for o in options if o["blocked"]]
            profitable = [o for o in available if o["profit_pct"] > 0.5]

            lines = []
            if profitable:
                best = max(profitable, key=lambda x: x["profit_pct"])
                lines.append(f"**{label}: {len(profitable)} profitable. Best: {best['name']} ({best['profit_pct']:+.1f}%)**")
            elif available:
                best = max(available, key=lambda x: x["profit_pct"])
                lines.append(f"**{label}: Not profitable. Best: {best['name']} ({best['profit_pct']:+.1f}%)**")
            else:
                reasons = ", ".join(o["blocked_reason"] for o in blocked if o["blocked_reason"])
                lines.append(f"**{label}: All blocked ({reasons})**")

            lines.append("```")

            # Show top 3 options with steps
            for o in sorted(available, key=lambda x: x["profit_pct"], reverse=True)[:3]:
                marker = ">>" if o["profit_pct"] > 0.5 else "--"
                lines.append(f"  {marker} {o['name']} ({o['profit_pct']:+.1f}%):")
                for step in o["steps"]:
                    lines.append(f"     {step}")
                lines.append(f"     -> {o['profit_desc']}")
                lines.append("")

            for o in blocked:
                lines.append(f"  -- {o['name']}: {o['blocked_reason']}")

            lines.append("```")

            msg = "\n".join(lines)
            # Safety: truncate if still too long
            if len(msg) > 1950:
                msg = msg[:1940] + "\n...\n```"
            await self._send(msg)
            await asyncio.sleep(0.5)  # avoid rate limit between messages

    async def send_summary_message(self, stats: dict):
        """Send a session summary when the scanner shuts down."""
        if not self.enabled:
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        msg = "**Ergo Arbitrage Monitor Stopped**\n"
        msg += "```\n"
        msg += f"  Stopped:       {now}\n"
        msg += f"  Duration:      {stats.get('session_duration', 'N/A')}\n"
        msg += f"  Opportunities: {stats.get('opportunities_seen', 0)}\n"
        msg += f"  Potential:     {stats.get('total_potential_profit_erg', 0):.4f} ERG\n"
        msg += "```"

        await self._send(msg)
