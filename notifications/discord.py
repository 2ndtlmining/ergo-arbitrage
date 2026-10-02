import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from typing import Optional

import aiohttp

import config
from arbitrage.calculator import ArbitrageOpportunity
from notifications import embeds

logger = logging.getLogger("ergo_arb.discord")

QUEUE_MAX = 100
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
RETRY_CAP_S = 30.0


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
        self._jobs: deque = deque()          # ("post", embed, content, on_id) | ("edit", embed, get_id)
        self._worker: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._busy = False
        self._bucket_until = 0.0             # loop time before which the next request must wait
        self._sleep = asyncio.sleep

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
        await self.stop()
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

    # --- queued embeds: never awaited from the scan/poll path -------------------------------

    def post(self, embed: dict, content: str = "", on_id=None):
        """Queue a new message; on_id(message_id) is called once Discord returns it."""
        self._enqueue(("post", embed, content, on_id))

    def edit(self, get_id, embed: dict):
        """Queue an edit of the message whose id get_id() returns when the job runs."""
        self._enqueue(("edit", embed, get_id))

    def _enqueue(self, job):
        if not self.enabled:
            return
        if len(self._jobs) >= QUEUE_MAX:
            oldest_post = next((j for j in self._jobs if j[0] == "post"), None)
            self._jobs.remove(oldest_post if oldest_post is not None else self._jobs[0])
            logger.warning("Discord queue full: dropped the oldest message")
        self._jobs.append(job)
        self._start_worker()

    def _start_worker(self):
        if self._worker is None or self._worker.done():
            self._wake = asyncio.Event()
            self._worker = asyncio.get_running_loop().create_task(self._run_worker())
        self._wake.set()

    async def _run_worker(self):
        while True:
            if not self._jobs:
                self._wake.clear()
                await self._wake.wait()
                continue
            job = self._jobs.popleft()
            self._busy = True
            try:
                await self._do(job)
            except Exception as e:  # a notifier problem must never reach the scanner
                logger.error(f"Discord delivery error: {e}")
            finally:
                self._busy = False

    async def _do(self, job):
        if job[0] == "post":
            _, embed, content, on_id = job
            status, body = await self._request("POST", f"{self.webhook_url}?wait=true",
                                               {"content": content, "embeds": [embed]})
            if status == 200 and body and body.get("id") and on_id:
                on_id(str(body["id"]))
        else:
            _, embed, get_id = job
            message_id = get_id()
            if not message_id:
                logger.warning("Discord edit dropped: the original message was not posted")
                return
            await self._request("PATCH", f"{self.webhook_url}/messages/{message_id}", {"embeds": [embed]})

    async def _request(self, method: str, url: str, payload: dict) -> tuple[int, Optional[dict]]:
        """One HTTP call to the webhook; on 429 waits retry_after (<= 30 s) and retries once."""
        if self._session is None:
            await self.connect()
        loop = asyncio.get_running_loop()
        status, body = 0, None
        for attempt in (0, 1):
            wait = self._bucket_until - loop.time()
            if wait > 0:
                await self._sleep(wait)
            async with self._session.request(method, url, json=payload, timeout=REQUEST_TIMEOUT) as r:
                status = r.status
                headers = r.headers or {}
                if headers.get("X-RateLimit-Remaining") == "0":
                    reset = min(float(headers.get("X-RateLimit-Reset-After") or 1), RETRY_CAP_S)
                    self._bucket_until = loop.time() + reset
                if status == 429 and attempt == 0:
                    data = await r.json(content_type=None) or {}
                    retry = data.get("retry_after") or headers.get("Retry-After") or 5
                    await self._sleep(min(float(retry), RETRY_CAP_S))
                    continue
                body = await r.json(content_type=None) if status == 200 else None
                if status not in (200, 204):
                    logger.warning(f"Discord {method} returned HTTP {status}")
                return status, body
        return status, body

    async def stop(self, timeout: float = 10):
        """Deliver what is queued (up to `timeout` seconds), then stop the worker."""
        if self._worker is None:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (self._jobs or self._busy) and loop.time() < deadline:
            await asyncio.sleep(0.05)
        self._worker.cancel()
        try:
            await self._worker
        except (asyncio.CancelledError, Exception):
            pass
        self._worker = None

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

    async def send_wallet_analysis(self, wallet: dict, analysis: dict):
        """Wallet balances and the best options per asset, as one embed."""
        self.post(embeds.wallet_embed(wallet, analysis))

    async def send_startup_message(self, mode: str = "notify"):
        self.post(embeds.startup_embed(mode), content=self._ping())

    async def send_summary_message(self, stats: dict):
        self.post(embeds.shutdown_embed(stats))
