"""Daily Discord digest: episodes, trades and outages over the last 24 h (once per day)."""
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import config
from notifications.mint_gate import mint_gate_text

DIGEST_KEY = "digest_date"


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


def build_digest(tracker, health, wallet: Optional[dict], now: datetime, hours: int = 24,
                 bank: Optional[dict] = None) -> Digest:
    since = now - timedelta(hours=hours)
    d = Digest(hours=hours, wallet=wallet)
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


def digest_due(tracker, now: datetime, hour: int) -> bool:
    return hour >= 0 and now.hour >= hour and tracker.get_meta(DIGEST_KEY) != now.date().isoformat()


def mark_digest_sent(tracker, now: datetime):
    tracker.set_meta(DIGEST_KEY, now.date().isoformat())
