"""Discord digest at DISCORD_DIGEST_HOURS (default 8, 14, 20): episodes, trades, outages and the wallet
since the previous digest. The only scheduled message; opportunities and alerts are posted when they happen."""
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import config
from notifications.mint_gate import mint_gate_text

DIGEST_KEY = "digest_slot"   # the last slot sent, e.g. "2026-10-05T14"


@dataclass
class Digest:
    hours: int
    paths: dict = field(default_factory=dict)       # label -> {count, longest_s, best_peak_percent}
    potential_erg: float = 0.0
    trades: dict = field(default_factory=dict)      # {count, net_erg, failed}
    outages: list = field(default_factory=list)     # (subject, seconds, ongoing)
    outage_since: str = ""
    wallet: Optional[dict] = None
    mint: Optional[str] = None                      # bank mint gate status text
    sigusd: Optional[str] = None                    # best exit for the wallet's SigUSD (usd_routes summary)


def build_digest(tracker, health, wallet: Optional[dict], now: datetime, hours: int = 24,
                 bank: Optional[dict] = None, sigusd: Optional[str] = None) -> Digest:
    since = now - timedelta(hours=hours)
    d = Digest(hours=hours, wallet=wallet, sigusd=sigusd)
    for row in tracker.chain_episodes_since(since.isoformat()):
        opened = datetime.fromisoformat(row["opened_at"])
        closed = datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else now
        p = d.paths.setdefault(row["path"], {"count": 0, "longest_s": 0.0, "best_peak_percent": float("-inf")})
        p["count"] += 1
        p["longest_s"] = max(p["longest_s"], (closed - opened).total_seconds())
        p["best_peak_percent"] = max(p["best_peak_percent"], row["peak_profit_percent"])
        d.potential_erg += row["peak_profit_erg"]
    trades = tracker.trades_since(since.isoformat())
    d.trades = {"count": len(trades),
                "net_erg": sum((t.get("actual_profit_erg") or 0.0) for t in trades if t.get("status") == "completed"),
                "failed": sum(1 for t in trades if t.get("status") == "failed")}
    if health is not None:
        wall_now = time.time()
        since_wall = max(wall_now - hours * 3600, health.started_at)
        d.outages = health.outages(now=wall_now, since=since_wall)
        d.outage_since = f"since {datetime.fromtimestamp(since_wall):%Y-%m-%d %H:%M}"
    d.mint = mint_gate_text(bank, config.MINT_GATE_MIN_ROOM_ERG)
    return d


def _hours(hours) -> list[int]:
    if isinstance(hours, int):
        return [hours] if 0 <= hours <= 23 else []
    return sorted(set(hours))


def digest_slot(now: datetime, hours) -> Optional[datetime]:
    """The latest scheduled digest time at or before `now` (today's, else yesterday's last), or None."""
    hours = _hours(hours)
    if not hours:
        return None
    today = [h for h in hours if h <= now.hour]
    day = now.replace(minute=0, second=0, microsecond=0)
    return day.replace(hour=today[-1]) if today else (day - timedelta(days=1)).replace(hour=hours[-1])


def digest_period_hours(slot: datetime, hours) -> int:
    """Hours since the previous scheduled digest (24 with one digest a day)."""
    hours = _hours(hours)
    earlier = [h for h in hours if h < slot.hour]
    previous = earlier[-1] if earlier else hours[-1] - 24
    return slot.hour - previous


def digest_due(tracker, now: datetime, hours) -> Optional[datetime]:
    """The slot to send now, or None (sent already, or no digest configured). The very first run only
    starts the schedule (no digest at once); a slot missed while the bot was down is sent on its return."""
    slot = digest_slot(now, hours)
    if slot is None:
        return None
    last = tracker.get_meta(DIGEST_KEY)
    if last is None:
        mark_digest_sent(tracker, slot)
        return None
    return None if last == slot.strftime("%Y-%m-%dT%H") else slot


def mark_digest_sent(tracker, slot: datetime):
    tracker.set_meta(DIGEST_KEY, slot.strftime("%Y-%m-%dT%H"))
