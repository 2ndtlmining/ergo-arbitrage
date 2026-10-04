"""`python arb.py status`: is the bot running and is everything OK? Read from the bot's database, its lock
file and .env only: no node needed, nothing written. `arb.py doctor` checks the node itself."""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from instance_lock import holder

STALE_SCAN_S = 300          # no full scan for this long while the bot should be running: say so


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _span(seconds: float) -> str:
    return _ago(seconds).replace(" ago", "")


def _next_digest(now: datetime) -> str:
    hours = config.DISCORD_DIGEST_HOURS
    if not hours:
        return "digest off (DISCORD_DIGEST_HOURS)"
    later = [h for h in hours if h > now.hour]
    return f"next digest {later[0]:02d}:00" if later else f"next digest {hours[0]:02d}:00 tomorrow"


def _one(conn, sql: str, args=()) -> Optional[sqlite3.Row]:
    try:
        return conn.execute(sql, args).fetchone()
    except sqlite3.Error:
        return None


def _all(conn, sql: str, args=()) -> list:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def status_lines(db_path, now: Optional[datetime] = None) -> list[str]:
    now = now or datetime.now()
    lines = []
    running = holder(db_path)
    if running is None:
        lines.append("Bot       not running (start it: python main.py --notify)")
    else:
        lines.append(f"Bot       running: mode {running.get('mode', '?')}, pid {running.get('pid', '?')}, "
                     f"since {running.get('since', '?')}")
    if not Path(db_path).exists():
        lines.append(f"Database  {db_path} does not exist yet")
        return lines

    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        scan = _one(conn, "SELECT * FROM price_snapshots ORDER BY id DESC LIMIT 1")
        if scan is None:
            lines.append("Last scan no scan recorded yet")
        else:
            age = (now - datetime.fromisoformat(scan["timestamp"])).total_seconds()
            prices = []
            if scan["spectrum_erg_sigusd"]:
                prices.append(f"pool {scan['spectrum_erg_sigusd']:.4f} SigUSD/ERG")
            if scan["oracle_erg_usd"]:
                prices.append(f"oracle ${scan['oracle_erg_usd']:.4f}")
            if scan["bank_reserve_ratio"]:
                prices.append(f"RR {scan['bank_reserve_ratio']:.0f}%")
            stale = " (stale: is the bot stuck or the node down? arb.py doctor)" \
                if running is not None and age > STALE_SCAN_S else ""
            lines.append(f"Last scan {_ago(age)}{': ' + ', '.join(prices) if prices else ''}{stale}")

        episodes = _all(conn, "SELECT * FROM chain_episodes WHERE closed_at IS NULL ORDER BY opened_at")
        if episodes:
            for e in episodes:
                open_for = (now - datetime.fromisoformat(e["opened_at"])).total_seconds()
                lines.append(f"Open      {e['path']}: peak {e['peak_profit_percent']:+.2f}% "
                             f"({e['peak_profit_erg']:+.4f} ERG at {e['peak_size_erg']:g} ERG), open {_span(open_for)}")
        else:
            lines.append("Open      no opportunity open")

        today = now.strftime("%Y-%m-%d")
        trades = _all(conn, "SELECT status, actual_profit_erg FROM trades WHERE substr(started_at, 1, 10) = ?", (today,))
        net = sum(t["actual_profit_erg"] or 0.0 for t in trades if t["status"] == "completed")
        failed = sum(1 for t in trades if t["status"] == "failed")
        lines.append(f"Trades    {len(trades)} today (max {config.LIVE_MAX_TRADES_PER_DAY}), net {net:+.4f} ERG"
                     f"{f', {failed} failed' if failed else ''}")

        paused = _one(conn, "SELECT value FROM meta WHERE key = 'live_paused'")
        if paused is not None:
            lines.append(f"Live      PAUSED after {paused['value']} (check arb.py balance, then arb.py resume and restart)")
    finally:
        conn.close()

    stop = config.repo_path(config.LIVE_STOP_FILE)
    if stop.exists():
        lines.append(f"Kill sw.  STOP file present ({stop}): live mode does not trade")
    lines.append(f"Discord   {'on' if config.DISCORD_ENABLED else 'off (no DISCORD_WEBHOOK_URL)'}, {_next_digest(now)}")
    return lines
