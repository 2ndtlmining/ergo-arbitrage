# Discord Episodes, Health Alerts and Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- Each on-chain opportunity becomes one Discord embed, edited while open and on close.
- Health problems are alerted, with recoveries.
- Wallet analysis is one embed, and a daily digest is posted.
- Discord never blocks the poll loop.

**Architecture:** pure modules (`embeds`, `episodes`, `health`, `digest`) fed from `DashboardState` every chain poll. The `DiscordNotifier` gets a bounded job queue, a background worker, post/edit with `?wait=true`, and one retry on 429. The tracker stores `chain_episodes` and a `meta` key/value table (schema version 2).

**Tech Stack:** Python 3.13, asyncio, aiohttp, sqlite3, pytest.

**Spec:** `docs/superpowers/specs/2026-10-02-discord-episodes-design.md`

## Global Constraints

- **Settings** (defaults):
  - `DISCORD_CONFIRM_SECONDS` 10, `DISCORD_CLOSE_SECONDS` 10, `DISCORD_EDIT_SECONDS` 30 (edit only when the change is > 0.1 pp)
  - `DISCORD_HEALTH_CHAIN_SECONDS` 120, `DISCORD_HEALTH_VENUE_SECONDS` 300, `DISCORD_HEALTH_ORACLE_SECONDS` 600, `DISCORD_HEALTH_REPEAT_SECONDS` 1800
  - `DISCORD_DIGEST_HOUR` 8 (−1 = off)
- **Pings** (`<@DISCORD_USER_ID>`) go only on:
  - tier-1 opens (`profit_percent >= DISCORD_TIER1_PROFIT_PERCENT`)
  - `chain` and `live` health alerts
  - the existing `notify_live`
- **Queue:** at most 100 jobs; when full, drop the oldest *post* (never an edit).
- **429:** wait `min(retry_after, 30)` s and retry once. When `X-RateLimit-Remaining == "0"`, wait `X-RateLimit-Reset-After` (≤ 30 s) before the next request.
- **Embed limits:** title 256, field value 1024, description 4096, 25 fields.
- **Explorer links:** `https://explorer.ergoplatform.com/en/token/{NFT}`.
- **CEX/USE paths** keep the existing `notify_opportunities` flow. The `LIVE_PATHS` keys move to episodes.
- **Nothing in the poll path awaits Discord.**

## Review Focus

1. **Discord unreachable or timing out for minutes:** polls keep their 2 s cadence, the queue stays bounded, and nothing raises into the scanner. Test in Task 4.
2. **Restart while an episode is open:** at startup the stale open `chain_episodes` row is closed. Test in Task 5.
3. **Profit flickering around the threshold every poll:** no open/close storm. Confirm and close hysteresis applies. Test in Task 2.
4. **Wallet analysis with missing keys** (`analysis` lacks an asset, or an option lacks `steps`): one embed, no `KeyError`. Test in Task 1.
5. **The digest on a fresh database** (no episodes, no trades, no outages): a sensible "nothing happened" embed. Test in Task 5.

---

### Task 1: `notifications/embeds.py` (pure embed builders)

**Files:**
- Create: `notifications/embeds.py`
- Test: `tests/test_embeds.py`

**Interfaces:**
- Produces:
  - colours `GREEN, GREY, RED, YELLOW, BLUE`
  - `cut(text, limit) -> str`, `duration(seconds) -> str`
  - `embed(title, color, fields=(), description=None, footer=None) -> dict`, `field(name, value, inline=True) -> dict`
  - `episode_embed(ep, status, height=0, data_age_s=None, oracle_usd=None) -> dict`. `ep` has the Task 2 `Episode` attributes: `label, size_erg, profit_erg, profit_percent, break_even_erg, peak_erg, peak_percent, peak_size_erg, opened_at, last_seen_at, closed_at, close_reason, trade, steps`.
  - `health_embed(event) -> dict`, with `event.kind`, `event.text`, `event.ping`
  - `wallet_embed(wallet, analysis) -> dict`
  - `digest_embed(d) -> dict`, with the Task 5 `Digest`
  - `startup_embed(mode) -> dict`, `shutdown_embed(stats) -> dict`

- [ ] **Step 1: Write the failing tests** (`tests/test_embeds.py`)

```python
"""Discord embed builders: pure, within Discord's limits (spec: discord episodes)."""
from types import SimpleNamespace

from notifications import embeds


def ep(**kw):
    base = dict(label="pool→redeem", size_erg=42.67, profit_erg=1.43, profit_percent=3.36, break_even_erg=0.06,
                peak_erg=1.51, peak_percent=3.5, peak_size_erg=45.0, opened_at=1000.0, last_seen_at=1380.0,
                closed_at=None, close_reason=None, trade=None, steps=["Swap 42.67 ERG -> SigUSD", "Redeem"])
    base.update(kw)
    return SimpleNamespace(**base)


def values(e):
    return " ".join([e["title"]] + [f["name"] + " " + f["value"] for f in e.get("fields", [])] +
                    [e.get("description", ""), e.get("footer", {}).get("text", "")])


def test_cut_and_duration():
    assert embeds.cut("abcdef", 4) == "abc…" and embeds.cut("abc", 4) == "abc"
    assert embeds.duration(45) == "45s" and embeds.duration(380) == "6m 20s" and embeds.duration(7300) == "2h 1m"


def test_open_episode_embed():
    e = embeds.episode_embed(ep(), "open", height=1885700, data_age_s=1.2, oracle_usd=0.3238)
    text = values(e)
    assert e["color"] == embeds.GREEN and e["title"].startswith("OPEN · pool→redeem +3.36%")
    for needle in ("42.67 ERG", "+1.4300 ERG", "+3.36%", "$+0.46", "+3.50%", "0.06 ERG", "6m 20s",
                   "1. Swap 42.67 ERG -> SigUSD", "explorer.ergoplatform.com/en/token/", "h1885700", "data 1s"):
        assert needle in text, needle


def test_closed_episode_embed():
    e = embeds.episode_embed(ep(closed_at=1380.0, profit_percent=0.3, trade="executed +0.42 ERG"), "closed")
    assert e["color"] == embeds.GREY
    assert e["title"] == "Closed · pool→redeem after 6m 20s · peak +3.50% · last +0.30%"
    assert "executed +0.42 ERG" in values(e) and "Steps" not in values(e)


def test_closed_by_shutdown_says_so():
    e = embeds.episode_embed(ep(closed_at=1100.0, close_reason="bot stopped"), "closed")
    assert "bot stopped" in values(e)


def test_limits_are_respected():
    long = "x" * 5000
    e = embeds.episode_embed(ep(label=long, steps=[long] * 5, trade=long), "open")
    assert len(e["title"]) <= 256 and all(len(f["value"]) <= 1024 for f in e["fields"])
    assert len(e["fields"]) <= 25


def test_health_embed_colours():
    alert = embeds.health_embed(SimpleNamespace(kind="alert", text="Chain state unreadable", ping=True))
    quiet = embeds.health_embed(SimpleNamespace(kind="alert", text="Kucoin down", ping=False))
    ok = embeds.health_embed(SimpleNamespace(kind="recovered", text="Kucoin recovered after 6m", ping=False))
    assert (alert["color"], quiet["color"], ok["color"]) == (embeds.RED, embeds.YELLOW, embeds.GREEN)
    assert ok["title"].startswith("✅") and alert["title"].startswith("⚠️")


def test_wallet_embed_survives_missing_keys():
    wallet = {"erg": 20.7119, "sigusd": 0.0}
    analysis = {"erg": {"balance": 20.7, "options": [{"name": "Spectrum buy -> Bank redeem", "profit_pct": 2.9,
                                                      "blocked": False}]},
                "sigusd": {"balance": 0.0}}
    e = embeds.wallet_embed(wallet, analysis)
    text = values(e)
    assert "20.7119 ERG" in text and "Spectrum buy -> Bank redeem" in text and "+2.9%" in text
    assert embeds.wallet_embed({}, {})["title"] == "Wallet"


def test_digest_embed_with_nothing_happening():
    d = SimpleNamespace(hours=24, paths={}, potential_erg=0.0, trades={"count": 0, "net_erg": 0.0, "failed": 0},
                        outages=[], outage_since="since 08:00", wallet={"erg": 20.7, "sigusd": 0.0})
    text = values(embeds.digest_embed(d))
    assert "No opportunities" in text and "No trades" in text and "No outages" in text and "20.7000 ERG" in text


def test_digest_embed_with_data():
    d = SimpleNamespace(hours=24, paths={"pool→redeem": {"count": 3, "longest_s": 400, "best_peak_percent": 3.5}},
                        potential_erg=2.75, trades={"count": 1, "net_erg": 0.42, "failed": 0},
                        outages=[("chain", 250.0, False)], outage_since="since 08:00", wallet=None)
    text = values(embeds.digest_embed(d))
    for needle in ("pool→redeem", "3 episodes", "6m 40s", "+3.50%", "+2.7500 ERG", "1 trade", "+0.4200 ERG",
                   "chain", "4m 10s"):
        assert needle in text, needle


def test_startup_and_shutdown():
    assert "LIVE" in embeds.startup_embed("live")["title"]
    s = embeds.shutdown_embed({"session_duration": "1:02:03", "opportunities_seen": 4})
    assert "1:02:03" in values(s)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_embeds.py -q`
Expected: `ImportError: cannot import name 'embeds' from 'notifications'`.

- [ ] **Step 3: Implement `notifications/embeds.py`**

```python
"""Discord embed payloads. Pure functions: data in, dict out, always within Discord's limits."""
from datetime import datetime, timezone
from typing import Optional

import config

GREEN, GREY, RED, YELLOW, BLUE = 0x2ECC71, 0x95A5A6, 0xE74C3C, 0xF1C40F, 0x3498DB
TITLE_MAX, FIELD_MAX, DESC_MAX, FOOTER_MAX, FIELDS_MAX = 256, 1024, 4096, 2048, 25
EXPLORER_TOKEN = "https://explorer.ergoplatform.com/en/token/{}"


def cut(text, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def duration(seconds: float) -> str:
    s = max(int(seconds), 0)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s" if m else f"{s}s"


def field(name: str, value, inline: bool = True) -> dict:
    return {"name": cut(name, TITLE_MAX), "value": cut(value if value not in (None, "") else "—", FIELD_MAX),
            "inline": inline}


def embed(title: str, color: int, fields=(), description: Optional[str] = None,
          footer: Optional[str] = None) -> dict:
    e = {"title": cut(title, TITLE_MAX), "color": color, "timestamp": datetime.now(timezone.utc).isoformat()}
    if description:
        e["description"] = cut(description, DESC_MAX)
    if fields:
        e["fields"] = list(fields)[:FIELDS_MAX]
    if footer:
        e["footer"] = {"text": cut(footer, FOOTER_MAX)}
    return e


def _boxes_field() -> dict:
    pool = EXPLORER_TOKEN.format(config.SPECTRUM_SIGUSD_POOL_NFT)
    bank = EXPLORER_TOKEN.format(config.SIGMAUSD_BANK_NFT)
    return field("Boxes", f"[pool]({pool}) · [bank]({bank})", inline=False)


def episode_embed(ep, status: str, height: int = 0, data_age_s: Optional[float] = None,
                  oracle_usd: Optional[float] = None) -> dict:
    is_open = status == "open"
    end = ep.closed_at if ep.closed_at is not None else ep.last_seen_at
    usd = f" / ${ep.profit_erg * oracle_usd:+.2f}" if oracle_usd else ""
    fields = [
        field("Size", f"{ep.size_erg:.2f} ERG"),
        field("Profit", f"{ep.profit_erg:+.4f} ERG ({ep.profit_percent:+.2f}%){usd}"),
        field("Peak", f"{ep.peak_percent:+.2f}% ({ep.peak_erg:+.4f} ERG at {ep.peak_size_erg:.2f})"),
        field("Break-even", f"{ep.break_even_erg:.2f} ERG" if ep.break_even_erg else None),
        field("Duration", duration(end - ep.opened_at)),
    ]
    if ep.trade:
        fields.append(field("Live trade", ep.trade, inline=False))
    if is_open and ep.steps:
        fields.append(field("Steps", "\n".join(f"{i}. {s}" for i, s in enumerate(ep.steps, 1)), inline=False))
    fields.append(_boxes_field())
    if is_open:
        title = f"OPEN · {ep.label} {ep.profit_percent:+.2f}%"
    else:
        title = (f"Closed · {ep.label} after {duration(end - ep.opened_at)} · "
                 f"peak {ep.peak_percent:+.2f}% · last {ep.profit_percent:+.2f}%")
    footer = f"h{height}" + (f" · data {data_age_s:.0f}s" if data_age_s is not None else "")
    description = f"Closed: {ep.close_reason}" if not is_open and ep.close_reason else None
    return embed(title, GREEN if is_open else GREY, fields, description=description, footer=footer)


def health_embed(event) -> dict:
    if event.kind == "recovered":
        return embed(f"✅ {event.text}", GREEN)
    return embed(f"⚠️ {event.text}", RED if event.ping else YELLOW)


def wallet_embed(wallet: dict, analysis: dict) -> dict:
    wallet, analysis = wallet or {}, analysis or {}
    description = (f"{wallet.get('erg', 0):.4f} ERG · {wallet.get('sigusd', 0):.2f} SigUSD · "
                   f"{wallet.get('use', 0):.3f} USE")
    fields = []
    for key, label in (("erg", "ERG"), ("sigusd", "SigUSD"), ("use", "USE")):
        info = analysis.get(key) or {}
        options = [o for o in (info.get("options") or []) if not o.get("blocked")]
        if not options:
            continue
        lines = []
        for o in sorted(options, key=lambda o: o.get("profit_pct", 0), reverse=True)[:3]:
            lines.append(f"{'>>' if o.get('profit_pct', 0) > 0.5 else '--'} {o.get('name', '?')} "
                         f"({o.get('profit_pct', 0):+.1f}%)")
        fields.append(field(f"{label} ({info.get('balance', 0):g})", "\n".join(lines), inline=False))
    return embed("Wallet", BLUE, fields, description=description)


def digest_embed(d) -> dict:
    if d.paths:
        lines = [f"{label}: {p['count']} episode{'s' if p['count'] != 1 else ''}, longest "
                 f"{duration(p['longest_s'])}, best peak {p['best_peak_percent']:+.2f}%"
                 for label, p in d.paths.items()]
        opp = "\n".join(lines) + f"\nPotential: {d.potential_erg:+.4f} ERG"
    else:
        opp = "No opportunities"
    t = d.trades
    trades = (f"{t['count']} trade{'s' if t['count'] != 1 else ''}, net {t['net_erg']:+.4f} ERG, "
              f"{t['failed']} failed") if t["count"] else "No trades"
    outages = "\n".join(f"{subject}: {duration(seconds)}{' (ongoing)' if ongoing else ''}"
                        for subject, seconds, ongoing in d.outages) or "No outages"
    fields = [field("Opportunities", opp, inline=False), field("Live trades", trades, inline=False),
              field(f"Outages ({d.outage_since})", outages, inline=False)]
    if d.wallet:
        fields.append(field("Wallet", f"{d.wallet.get('erg', 0):.4f} ERG · {d.wallet.get('sigusd', 0):.2f} SigUSD"))
    return embed(f"Daily digest · last {d.hours}h", BLUE, fields)


def startup_embed(mode: str) -> dict:
    fields = [
        field("Alerts", f">= {config.DISCORD_MIN_PROFIT_PERCENT}% and >= {config.DISCORD_MIN_PROFIT_ERG} ERG, "
                        f"held {config.DISCORD_CONFIRM_SECONDS:g}s"),
        field("Ping", f">= {config.DISCORD_TIER1_PROFIT_PERCENT}%, chain down, live paused"),
        field("Max trade", f"{config.MAX_TRADE_SIZE_ERG:g} ERG"),
        field("Digest", f"{config.DISCORD_DIGEST_HOUR}:00" if config.DISCORD_DIGEST_HOUR >= 0 else "off"),
    ]
    return embed(f"Ergo arbitrage started · {mode.upper()}", RED if mode == "live" else BLUE, fields)


def shutdown_embed(stats: dict) -> dict:
    stats = stats or {}
    fields = [field("Ran for", stats.get("session_duration", "—")),
              field("Opportunities", stats.get("opportunities_seen", 0))]
    return embed("Ergo arbitrage stopped", GREY, fields)
```

Add the new config in `config.py` now (`startup_embed` uses them), next to the other `DISCORD_*` settings:

```python
DISCORD_CONFIRM_SECONDS = float(os.getenv("DISCORD_CONFIRM_SECONDS", "10"))   # held this long before a message opens
DISCORD_CLOSE_SECONDS = float(os.getenv("DISCORD_CLOSE_SECONDS", "10"))       # gone this long before it closes
DISCORD_EDIT_SECONDS = float(os.getenv("DISCORD_EDIT_SECONDS", "30"))         # at most one edit per this
DISCORD_HEALTH_CHAIN_SECONDS = float(os.getenv("DISCORD_HEALTH_CHAIN_SECONDS", "120"))
DISCORD_HEALTH_VENUE_SECONDS = float(os.getenv("DISCORD_HEALTH_VENUE_SECONDS", "300"))
DISCORD_HEALTH_ORACLE_SECONDS = float(os.getenv("DISCORD_HEALTH_ORACLE_SECONDS", "600"))
DISCORD_HEALTH_REPEAT_SECONDS = float(os.getenv("DISCORD_HEALTH_REPEAT_SECONDS", "1800"))
DISCORD_DIGEST_HOUR = int(os.getenv("DISCORD_DIGEST_HOUR", "8"))              # local hour, -1 = off
```

Also add these to `tests/conftest.py` `TEST_DEFAULTS`: `"DISCORD_CONFIRM_SECONDS": 10, "DISCORD_CLOSE_SECONDS": 10, "DISCORD_EDIT_SECONDS": 30, "DISCORD_DIGEST_HOUR": 8, "DISCORD_MIN_PROFIT_PERCENT": 1.0, "DISCORD_MIN_PROFIT_ERG": 0.5, "DISCORD_TIER1_PROFIT_PERCENT": 2.0`.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_embeds.py -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add notifications/embeds.py tests/test_embeds.py config.py tests/conftest.py
git commit -m "Discord embed builders (episodes, health, wallet, digest, startup/shutdown) and settings"
```

---

### Task 2: `notifications/episodes.py` (episode lifecycle)

**Files:**
- Create: `notifications/episodes.py`
- Test: `tests/test_episodes.py`

**Interfaces:**
- Consumes: `PathRow`-like rows, with `status`, `label`, `steps`, and `choice` (fields `size_erg, profit_erg, profit_percent, break_even_erg`).
- Produces:
  - `@dataclass Episode` (fields per the spec, plus `close_reason: Optional[str]` and `closing_since: Optional[float]`)
  - `@dataclass EpisodeEvent(kind, episode)`
  - `EpisodeTracker(confirm_s, close_s, edit_s, min_percent, min_erg)` with `update(now, rows) -> list[EpisodeEvent]`, `note_trade(key, text)`, `close_all(now, reason) -> list[EpisodeEvent]`, and `open: dict[str, Episode]`

- [ ] **Step 1: Write the failing tests** (`tests/test_episodes.py`)

```python
"""Episode lifecycle: open after confirm, throttled edits, close with hysteresis (spec: discord episodes)."""
from types import SimpleNamespace

from notifications.episodes import EpisodeTracker

KEY = "Spectrum buy->Bank redeem"


def row(pct=3.0, erg=1.2, status="GO"):
    choice = SimpleNamespace(size_erg=40.0, profit_erg=erg, profit_percent=pct, break_even_erg=0.1, ok=status == "GO")
    return {KEY: SimpleNamespace(label="pool→redeem", status=status, choice=choice, steps=["a", "b"])}


def tracker():
    return EpisodeTracker(confirm_s=10, close_s=10, edit_s=30, min_percent=1.0, min_erg=0.5)


def kinds(events):
    return [e.kind for e in events]


def test_opens_only_after_confirm_seconds():
    t = tracker()
    assert t.update(0, row()) == [] and t.update(8, row()) == []
    ev = t.update(10, row())
    assert kinds(ev) == ["open"] and ev[0].episode.opened_at == 0 and ev[0].episode.steps == ["a", "b"]


def test_a_blip_below_confirm_sends_nothing():
    t = tracker()
    t.update(0, row())
    t.update(4, row(status="no edge"))
    assert t.update(12, row()) == []  # confirm restarted at 12
    assert kinds(t.update(22, row())) == ["open"]


def test_thresholds_apply():
    t = tracker()
    for now in (0, 10, 20):
        assert t.update(now, row(pct=0.8)) == []   # below DISCORD_MIN_PROFIT_PERCENT
        assert t.update(now, row(erg=0.3)) == []   # below DISCORD_MIN_PROFIT_ERG


def test_edits_are_throttled_by_time_and_change():
    t = tracker()
    t.update(0, row(3.0))
    t.update(10, row(3.0))                          # open
    assert t.update(20, row(3.5)) == []              # < 30 s since the open
    assert t.update(40, row(3.05)) == []             # 30 s passed but change <= 0.1 pp
    ev = t.update(45, row(3.5))
    assert kinds(ev) == ["update"] and ev[0].episode.profit_percent == 3.5
    assert t.update(50, row(4.0)) == []              # throttled again


def test_close_needs_close_seconds_and_requalifying_cancels_it():
    t = tracker()
    t.update(0, row())
    t.update(10, row())
    assert t.update(12, row(status="no edge")) == []
    assert t.update(18, row()) == []                 # back within 10 s: still open, no close
    assert t.update(20, row(status="no edge")) == []
    ev = t.update(30, row(status="no edge"))
    assert kinds(ev) == ["close"] and ev[0].episode.closed_at == 30 and KEY not in t.open


def test_missing_row_counts_as_not_qualifying():
    t = tracker()
    t.update(0, row())
    t.update(10, row())
    t.update(11, {})
    assert kinds(t.update(21, {})) == ["close"]


def test_peak_is_tracked():
    t = tracker()
    t.update(0, row(3.0, 1.2))
    t.update(10, row(3.0, 1.2))
    t.update(12, row(4.0, 1.6))
    t.update(14, row(2.0, 0.8))
    ep = t.open[KEY]
    assert (ep.peak_percent, ep.peak_erg, ep.profit_percent) == (4.0, 1.6, 2.0)


def test_trade_result_and_close_all():
    t = tracker()
    t.update(0, row())
    t.update(10, row())
    t.note_trade(KEY, "executed +0.42 ERG")
    ev = t.close_all(15, "bot stopped")
    assert kinds(ev) == ["close"] and ev[0].episode.trade == "executed +0.42 ERG"
    assert ev[0].episode.close_reason == "bot stopped" and t.open == {}


def test_flicker_every_poll_does_not_storm():
    t = tracker()
    events = []
    for i in range(60):  # 2 minutes of GO / not GO alternating every 2 s
        events += t.update(i * 2, row(status="GO" if i % 2 == 0 else "no edge"))
    assert events == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_episodes.py -q`
Expected: `ModuleNotFoundError: No module named 'notifications.episodes'`.

- [ ] **Step 3: Implement `notifications/episodes.py`**

```python
"""Opportunity episodes for Discord: open after a path qualifies for confirm_s, edit at most every
edit_s (and only on a > 0.1 pp change), close after it stops qualifying for close_s."""
from dataclasses import dataclass, field
from typing import Optional

EDIT_MIN_CHANGE_PP = 0.1


@dataclass
class Episode:
    key: str
    label: str
    opened_at: float
    last_seen_at: float
    size_erg: float
    profit_erg: float
    profit_percent: float
    break_even_erg: Optional[float]
    peak_erg: float
    peak_percent: float
    peak_size_erg: float
    steps: list = field(default_factory=list)
    message_id: Optional[str] = None
    last_edit_at: float = 0.0
    last_edit_percent: float = 0.0
    trade: Optional[str] = None
    closed_at: Optional[float] = None
    close_reason: Optional[str] = None
    closing_since: Optional[float] = None
    db_id: Optional[int] = None


@dataclass
class EpisodeEvent:
    kind: str          # open | update | close
    episode: Episode


class EpisodeTracker:
    def __init__(self, confirm_s: float, close_s: float, edit_s: float, min_percent: float, min_erg: float):
        self.confirm_s, self.close_s, self.edit_s = confirm_s, close_s, edit_s
        self.min_percent, self.min_erg = min_percent, min_erg
        self.open: dict[str, Episode] = {}
        self._pending: dict[str, float] = {}   # key -> time it started qualifying

    def _qualifies(self, row) -> bool:
        c = getattr(row, "choice", None) if row is not None else None
        return bool(row is not None and row.status == "GO" and c is not None
                    and c.profit_percent >= self.min_percent and c.profit_erg >= self.min_erg)

    @staticmethod
    def _refresh(ep: Episode, row, now: float):
        c = row.choice
        ep.size_erg, ep.profit_erg, ep.profit_percent = c.size_erg, c.profit_erg, c.profit_percent
        ep.break_even_erg = c.break_even_erg
        ep.last_seen_at = now
        if row.steps:
            ep.steps = list(row.steps)
        if c.profit_erg > ep.peak_erg:
            ep.peak_erg, ep.peak_percent, ep.peak_size_erg = c.profit_erg, c.profit_percent, c.size_erg

    def update(self, now: float, rows: dict) -> list[EpisodeEvent]:
        events = []
        for key in set(rows) | set(self.open) | set(self._pending):
            row = rows.get(key)
            q = self._qualifies(row)
            ep = self.open.get(key)
            if ep is None:
                if not q:
                    self._pending.pop(key, None)
                    continue
                first = self._pending.setdefault(key, now)
                if now - first < self.confirm_s:
                    continue
                c = row.choice
                ep = Episode(key, row.label, first, now, c.size_erg, c.profit_erg, c.profit_percent,
                             c.break_even_erg, c.profit_erg, c.profit_percent, c.size_erg, list(row.steps or []),
                             last_edit_at=now, last_edit_percent=c.profit_percent)
                self.open[key] = ep
                del self._pending[key]
                events.append(EpisodeEvent("open", ep))
                continue
            if q:
                ep.closing_since = None
                self._refresh(ep, row, now)
                if (now - ep.last_edit_at >= self.edit_s
                        and abs(ep.profit_percent - ep.last_edit_percent) > EDIT_MIN_CHANGE_PP):
                    ep.last_edit_at, ep.last_edit_percent = now, ep.profit_percent
                    events.append(EpisodeEvent("update", ep))
                continue
            if ep.closing_since is None:
                ep.closing_since = now
            elif now - ep.closing_since >= self.close_s:
                ep.closed_at = now
                del self.open[key]
                events.append(EpisodeEvent("close", ep))
        return events

    def note_trade(self, key: str, text: str):
        if key in self.open:
            self.open[key].trade = text

    def close_all(self, now: float, reason: str) -> list[EpisodeEvent]:
        events = []
        for key, ep in list(self.open.items()):
            ep.closed_at, ep.close_reason = now, reason
            events.append(EpisodeEvent("close", ep))
        self.open.clear()
        self._pending.clear()
        return events
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_episodes.py -q`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add notifications/episodes.py tests/test_episodes.py
git commit -m "Discord episode lifecycle: confirm, throttled edits, close hysteresis"
```

---

### Task 3: `notifications/health.py` (health alerts)

**Files:**
- Create: `notifications/health.py`
- Test: `tests/test_health_alerts.py`

**Interfaces:**
- Consumes: a `DashboardState`-like object with `chain_error`, `venues` (`VenueStatus`: `name, kind, state, error`), `live`, `live_detail`.
- Produces:
  - `@dataclass HealthEvent(subject, kind, text, ping)`
  - `HealthMonitor(chain_s, venue_s, oracle_s, repeat_s, started_at)` with `update(now, state) -> list[HealthEvent]`, `outages(now, since) -> list[tuple[str, float, bool]]` (subject, seconds, ongoing), and `started_at`
  - `CHAIN_VENUES = {"ErgoDEX pool", "SigmaUSD bank", "Oracle"}`

- [ ] **Step 1: Write the failing tests** (`tests/test_health_alerts.py`)

```python
"""Health alerts: thresholds, repeats, recoveries, pings (spec: discord episodes)."""
from types import SimpleNamespace

from notifications.health import HealthMonitor


def state(chain_error=None, venues=(), live="armed", detail=()):
    return SimpleNamespace(chain_error=chain_error, venues=list(venues), live=live, live_detail=list(detail))


def venue(name, st, error=""):
    return SimpleNamespace(name=name, kind="CEX", state=st, error=error)


def monitor():
    return HealthMonitor(chain_s=120, venue_s=300, oracle_s=600, repeat_s=1800, started_at=0)


def test_chain_alert_after_threshold_with_ping_and_recovery():
    m = monitor()
    assert m.update(0, state("timeout")) == [] and m.update(100, state("timeout")) == []
    ev = m.update(120, state("timeout"))
    assert [(e.subject, e.kind, e.ping) for e in ev] == [("chain", "alert", True)] and "timeout" in ev[0].text
    ev = m.update(370, state())
    assert [(e.subject, e.kind, e.ping) for e in ev] == [("chain", "recovered", False)]
    assert "6m 10s" in ev[0].text


def test_short_outage_has_no_alert_and_no_recovery():
    m = monitor()
    m.update(0, state("timeout"))
    assert m.update(60, state()) == []


def test_no_repeat_within_repeat_seconds_then_repeat():
    m = monitor()
    m.update(0, state("x"))
    assert len(m.update(120, state("x"))) == 1
    assert m.update(1000, state("x")) == []
    assert [e.kind for e in m.update(1920, state("x"))] == ["alert"]


def test_cex_venue_down_is_silent_and_chain_venues_are_not_alerted():
    m = monitor()
    down = [venue("Kucoin", "down", "no quote"), venue("ErgoDEX pool", "down", "timeout")]
    m.update(0, state(venues=down))
    ev = m.update(300, state(venues=down))
    assert [(e.subject, e.ping) for e in ev] == [("venue:Kucoin", False)] and "no quote" in ev[0].text


def test_stuck_oracle_update():
    m = monitor()
    pend = [venue("Oracle", "pending")]
    m.update(0, state(venues=pend))
    assert m.update(500, state(venues=pend)) == []
    assert [(e.subject, e.ping) for e in m.update(600, state(venues=pend))] == [("oracle", False)]
    assert [e.kind for e in m.update(650, state(venues=[venue("Oracle", "live")]))] == ["recovered"]


def test_live_pause_alerts_at_once_with_ping():
    m = monitor()
    ev = m.update(5, state(live="paused", detail=["leg 2 failed (oracle moved)"]))
    assert [(e.subject, e.ping) for e in ev] == [("live", True)] and "leg 2 failed" in ev[0].text


def test_outage_log_for_the_digest():
    m = monitor()
    m.update(0, state("x"))
    m.update(130, state("x"))
    m.update(250, state())
    m.update(300, state(venues=[venue("Kucoin", "down")]))
    m.update(700, state(venues=[venue("Kucoin", "down")]))
    out = m.outages(now=800, since=0)
    assert ("chain", 250.0, False) in out and ("venue:Kucoin", 500.0, True) in out
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_health_alerts.py -q`
Expected: `ModuleNotFoundError: No module named 'notifications.health'`.

- [ ] **Step 3: Implement `notifications/health.py`**

```python
"""Health alerts for Discord from the dashboard state: chain unreadable, a venue down, an oracle
update stuck, live trading paused. One alert per subject per repeat window; a recovery always
follows an alert. Outages are remembered (since this process started) for the daily digest."""
from dataclasses import dataclass
from typing import Optional

from notifications.embeds import duration

CHAIN_VENUES = {"ErgoDEX pool", "SigmaUSD bank", "Oracle"}  # covered by the "chain" subject


@dataclass
class HealthEvent:
    subject: str
    kind: str      # alert | recovered
    text: str
    ping: bool


class HealthMonitor:
    def __init__(self, chain_s: float, venue_s: float, oracle_s: float, repeat_s: float, started_at: float):
        self.chain_s, self.venue_s, self.oracle_s, self.repeat_s = chain_s, venue_s, oracle_s, repeat_s
        self.started_at = started_at
        self._since: dict[str, float] = {}
        self._alerted: dict[str, float] = {}
        self._log: list[tuple[str, float, Optional[float]]] = []   # (subject, start, end) of alerted outages

    def _failing(self, state) -> dict[str, tuple[float, bool, str]]:
        failing = {}
        if state.chain_error:
            failing["chain"] = (self.chain_s, True,
                                f"Chain state unreadable for over {duration(self.chain_s)}: {state.chain_error}")
        for v in state.venues or []:
            if v.state == "down" and v.name not in CHAIN_VENUES:
                failing[f"venue:{v.name}"] = (self.venue_s, False,
                                              f"{v.name} down for over {duration(self.venue_s)}: {v.error or 'no data'}")
            if v.name == "Oracle" and v.state == "pending":
                failing["oracle"] = (self.oracle_s, False,
                                     f"Oracle update pending for over {duration(self.oracle_s)} (stuck?)")
        if state.live == "paused":
            failing["live"] = (0.0, True, f"Live trading paused: {'; '.join(state.live_detail) or 'see console'}")
        return failing

    def update(self, now: float, state) -> list[HealthEvent]:
        events = []
        failing = self._failing(state)
        for subject, (threshold, ping, text) in failing.items():
            first = self._since.setdefault(subject, now)
            last = self._alerted.get(subject)
            if now - first >= threshold and (last is None or now - last >= self.repeat_s):
                if last is None:
                    self._log.append((subject, first, None))
                self._alerted[subject] = now
                events.append(HealthEvent(subject, "alert", text, ping))
        for subject in [s for s in self._since if s not in failing]:
            first = self._since.pop(subject)
            if self._alerted.pop(subject, None) is not None:
                self._log = [(s, a, now if s == subject and b is None and a == first else b) for s, a, b in self._log]
                name = "Chain state" if subject == "chain" else subject.split(":", 1)[-1].capitalize() \
                    if subject == "oracle" else subject.split(":", 1)[-1]
                events.append(HealthEvent(subject, "recovered", f"{name} recovered after {duration(now - first)}",
                                          False))
        return events

    def outages(self, now: float, since: float) -> list[tuple[str, float, bool]]:
        """(subject, seconds down within [since, now], still ongoing) for alerted outages."""
        out = []
        for subject, start, end in self._log:
            stop = end if end is not None else now
            if stop < since:
                continue
            out.append((subject, float(stop - max(start, since)), end is None))
        return out
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_health_alerts.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add notifications/health.py tests/test_health_alerts.py
git commit -m "Discord health alerts: chain, venues, stuck oracle, live paused; outage log"
```

---

### Task 4: Notifier queue, worker, post/edit, 429 retry, embed versions of wallet/startup/shutdown

**Files:**
- Modify: `notifications/discord.py` (`__init__`, `connect`, `disconnect`, `send_wallet_analysis`, `send_startup_message`, `send_summary_message`; new queue/worker methods)
- Test: `tests/test_discord_queue.py`

**Interfaces:**
- Consumes: `notifications.embeds` (Task 1).
- Produces on `DiscordNotifier`:
  - `QUEUE_MAX = 100`, `REQUEST_TIMEOUT`
  - `post(embed, content="", on_id=None)` and `edit(get_id, embed)`, both non-blocking and fine to call from the event loop
  - `async stop(timeout=10)` and `async _request(method, url, payload) -> (status, body)`
  - `self._sleep` (`asyncio.sleep`, replaceable in tests) and `self._session`
  - `send_wallet_analysis(wallet, analysis)`, `send_startup_message(mode)` and `send_summary_message(stats)` keep their names (all async) but now enqueue embeds

- [ ] **Step 1: Write the failing tests** (`tests/test_discord_queue.py`)

```python
"""Discord delivery: queue + worker, post/edit with ?wait=true, 429 retry, bounded queue (spec)."""
import asyncio

import pytest

import config
from notifications.discord import QUEUE_MAX, DiscordNotifier

HOOK = "https://discord.com/api/webhooks/1/abc"


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status, self._body, self.headers = status, body, headers or {}

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeHook:
    """Replies from a script per call; records (method, url, payload)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, json=None, timeout=None):
        self.calls.append((method, url, json))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, BaseException):
            raise r
        return r


@pytest.fixture
def notifier(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", HOOK)
    monkeypatch.setattr(config, "DISCORD_ENABLED", True)
    n = DiscordNotifier()
    n.slept = []

    async def fake_sleep(s):
        n.slept.append(s)

    n._sleep = fake_sleep
    return n


def run(coro):
    return asyncio.run(coro)


def test_post_then_edit_uses_the_returned_message_id(notifier):
    hook = FakeHook(Resp(200, {"id": "m1"}), Resp(200, {"id": "m1"}))
    notifier._session = hook
    got = {}

    async def go():
        notifier.post({"title": "open"}, content="<@1>", on_id=lambda mid: got.setdefault("id", mid))
        notifier.edit(lambda: got.get("id"), {"title": "closed"})
        await notifier.stop()

    run(go())
    (m1, u1, p1), (m2, u2, p2) = hook.calls
    assert (m1, u1) == ("POST", HOOK + "?wait=true") and p1 == {"content": "<@1>", "embeds": [{"title": "open"}]}
    assert (m2, u2) == ("PATCH", HOOK + "/messages/m1") and p2 == {"embeds": [{"title": "closed"}]}


def test_edit_without_message_id_is_dropped(notifier):
    hook = FakeHook(Resp(500, {"message": "boom"}))
    notifier._session = hook

    async def go():
        notifier.post({"title": "open"})
        notifier.edit(lambda: None, {"title": "closed"})
        await notifier.stop()

    run(go())
    assert [c[0] for c in hook.calls] == ["POST"]


def test_429_waits_and_retries_once_then_gives_up(notifier):
    hook = FakeHook(Resp(429, {"retry_after": 2.5}), Resp(200, {"id": "m1"}))
    notifier._session = hook
    run(notifier._request("POST", HOOK, {}))
    assert notifier.slept == [2.5] and len(hook.calls) == 2
    hook2 = FakeHook(Resp(429, {"retry_after": 99}))
    notifier._session, notifier.slept = hook2, []
    status, _ = run(notifier._request("POST", HOOK, {}))
    assert status == 429 and notifier.slept == [30] and len(hook2.calls) == 2


def test_rate_limit_headers_delay_the_next_request(notifier):
    hook = FakeHook(Resp(200, {"id": "a"}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "1.5"}),
                    Resp(200, {"id": "b"}))

    async def go():
        notifier._session = hook
        await notifier._request("POST", HOOK, {})
        await notifier._request("POST", HOOK, {})

    run(go())
    assert notifier.slept and 0 < notifier.slept[0] <= 1.5


def test_full_queue_drops_the_oldest_post_never_an_edit(notifier):
    notifier._start_worker = lambda: None  # keep jobs queued
    notifier.edit(lambda: "m0", {"title": "edit"})
    for i in range(QUEUE_MAX + 5):
        notifier.post({"title": f"p{i}"})
    kinds = [job[0] for job in notifier._jobs]
    assert len(notifier._jobs) == QUEUE_MAX and kinds[0] == "edit"
    assert notifier._jobs[1][1]["title"] == "p6"


def test_worker_survives_exceptions(notifier):
    hook = FakeHook(RuntimeError("network down"), Resp(200, {"id": "m2"}))
    notifier._session = hook
    got = []

    async def go():
        notifier.post({"title": "a"})
        notifier.post({"title": "b"}, on_id=got.append)
        await notifier.stop()

    run(go())
    assert got == ["m2"]


def test_disabled_notifier_queues_nothing(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_ENABLED", False)
    n = DiscordNotifier()
    n.post({"title": "x"})
    assert len(n._jobs) == 0


def test_wallet_analysis_is_one_embed(notifier):
    hook = FakeHook(Resp(200, {"id": "w"}))
    notifier._session = hook

    async def go():
        await notifier.send_wallet_analysis({"erg": 20.7}, {"erg": {"balance": 20.7, "options": []}})
        await notifier.stop()

    run(go())
    assert len(hook.calls) == 1 and hook.calls[0][2]["embeds"][0]["title"] == "Wallet"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_discord_queue.py -q`
Expected: `ImportError: cannot import name 'QUEUE_MAX'`.

- [ ] **Step 3: Implement in `notifications/discord.py`**

Imports: add `from collections import deque` and `from notifications import embeds`.

Module constants:

```python
QUEUE_MAX = 100
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
RETRY_CAP_S = 30.0
```

In `__init__` add:

```python
        self._jobs: deque = deque()          # ("post", embed, content, on_id) | ("edit", embed, get_id)
        self._worker: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._busy = False
        self._bucket_until = 0.0             # loop time before which the next request must wait
        self._sleep = asyncio.sleep
```

New methods (place them after `_send`):

```python
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
```

`connect()` must also run when the session is used through `_request`. The existing `connect()` creates `aiohttp.ClientSession()` only `if self.enabled and self._session is None`; keep it.

Replace the bodies of these three methods:

```python
    async def send_wallet_analysis(self, wallet: dict, analysis: dict):
        """Wallet balances and the best options per asset, as one embed."""
        self.post(embeds.wallet_embed(wallet, analysis))

    async def send_startup_message(self, mode: str = "notify"):
        self.post(embeds.startup_embed(mode), content=self._ping())

    async def send_summary_message(self, stats: dict):
        self.post(embeds.shutdown_embed(stats))
```

`disconnect()` first awaits `self.stop()`, then closes the session.

- [ ] **Step 4: Run to verify**

Run: `python -m pytest tests/test_discord_queue.py -q`, then `python -m pytest -q`.
Expected: 8 passed; full suite all-pass. Fix any old Discord test that asserted the text formats of the three replaced methods: assert on the posted embed instead, and record a ruling.

- [ ] **Step 5: Commit**

```bash
git add notifications/discord.py tests/test_discord_queue.py
git commit -m "Discord delivery queue: post/edit with ?wait=true, 429 retry, bounded queue; embeds for wallet/startup/shutdown"
```

---

### Task 5: Tracker `chain_episodes` + `meta`, and the daily digest

**Files:**
- Modify: `tracker/profit_tracker.py` (`SCHEMA_VERSION = 2`, `_migrate`, `__init__`, new methods)
- Create: `notifications/digest.py`
- Test: `tests/test_digest.py`

**Interfaces:**
- Consumes: `Episode` (Task 2), `HealthMonitor.outages` (Task 3), `embeds.digest_embed` (Task 1).
- Produces:
  - **Tracker:** `open_chain_episode(ep) -> int`, `update_chain_episode(id, ep)`, `close_chain_episode(id, ep)`, `chain_episodes_since(iso) -> list[dict]`, `trades_since(iso) -> list[dict]`, `get_meta(key) -> Optional[str]`, `set_meta(key, value)`
  - **Digest:** `@dataclass Digest(hours, paths, potential_erg, trades, outages, outage_since, wallet)`, `build_digest(tracker, health, wallet, now: datetime, hours=24) -> Digest`, `digest_due(tracker, now: datetime, hour) -> bool`, `mark_digest_sent(tracker, now)`

- [ ] **Step 1: Write the failing tests** (`tests/test_digest.py`)

```python
"""chain_episodes storage and the daily digest (spec: discord episodes)."""
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from notifications.digest import build_digest, digest_due, mark_digest_sent
from notifications.health import HealthMonitor
from tracker.profit_tracker import ProfitTracker


@pytest.fixture
def tracker(tmp_path):
    t = ProfitTracker(str(tmp_path / "t.db"))
    yield t
    t.close()


def ep(label="pool→redeem", peak_erg=1.5, peak_pct=3.5, last=0.3, trade=None):
    return SimpleNamespace(key="Spectrum buy->Bank redeem", label=label, peak_erg=peak_erg, peak_percent=peak_pct,
                           peak_size_erg=45.0, profit_percent=last, trade=trade)


def test_chain_episode_roundtrip(tracker):
    e = ep()
    i = tracker.open_chain_episode(e)
    e.peak_erg, e.peak_percent = 2.0, 4.1
    tracker.update_chain_episode(i, e)
    tracker.close_chain_episode(i, ep(peak_erg=2.0, peak_pct=4.1, last=0.2, trade="executed +0.42 ERG"))
    (row,) = tracker.chain_episodes_since("2000-01-01")
    assert row["path"] == "pool→redeem" and row["peak_profit_erg"] == 2.0 and row["closed_at"]
    assert row["trade"] == "executed +0.42 ERG" and row["last_profit_percent"] == 0.2


def test_open_episode_is_closed_after_a_restart(tmp_path):
    t = ProfitTracker(str(tmp_path / "r.db"))
    t.open_chain_episode(ep())
    t.close()
    t2 = ProfitTracker(str(tmp_path / "r.db"))
    try:
        (row,) = t2.chain_episodes_since("2000-01-01")
        assert row["closed_at"] is not None
    finally:
        t2.close()


def test_meta_roundtrip(tracker):
    assert tracker.get_meta("digest_date") is None
    tracker.set_meta("digest_date", "2026-10-02")
    assert tracker.get_meta("digest_date") == "2026-10-02"


def test_digest_on_a_fresh_database(tracker):
    d = build_digest(tracker, HealthMonitor(120, 300, 600, 1800, started_at=time.time()), {"erg": 20.7},
                     datetime.now())
    assert d.paths == {} and d.potential_erg == 0 and d.trades == {"count": 0, "net_erg": 0.0, "failed": 0}
    assert d.outages == [] and d.wallet == {"erg": 20.7}


def test_digest_with_episodes_trades_and_outages(tracker):
    for peak in (1.0, 2.0):
        i = tracker.open_chain_episode(ep(peak_erg=peak, peak_pct=peak * 2))
        tracker.close_chain_episode(i, ep(peak_erg=peak, peak_pct=peak * 2))
    t1 = tracker.start_trade(None, 10.0, 10.4, 0.4)
    tracker.complete_trade(t1, actual_output=10.42)
    t2 = tracker.start_trade(None, 5.0, 5.1, 0.1)
    tracker.fail_trade(t2, "leg 2 failed")
    now_w = time.time()  # the health log uses wall-clock times, like the scanner feeds it
    health = HealthMonitor(120, 300, 600, 1800, started_at=now_w - 1000)
    health._log = [("chain", now_w - 900, now_w - 650)]
    d = build_digest(tracker, health, None, datetime.now())
    p = d.paths["pool→redeem"]
    assert p["count"] == 2 and p["best_peak_percent"] == 4.0 and d.potential_erg == pytest.approx(3.0)
    assert d.trades["count"] == 2 and d.trades["failed"] == 1 and d.trades["net_erg"] == pytest.approx(0.42)
    assert ("chain", 250.0, False) in d.outages


def test_digest_due_once_per_day_and_survives_restart(tmp_path):
    t = ProfitTracker(str(tmp_path / "d.db"))
    morning = datetime(2026, 10, 2, 7, 59)
    nine = datetime(2026, 10, 2, 9, 0)
    assert not digest_due(t, morning, 8) and digest_due(t, nine, 8)
    mark_digest_sent(t, nine)
    t.close()
    t2 = ProfitTracker(str(tmp_path / "d.db"))
    try:
        assert not digest_due(t2, nine + timedelta(hours=3), 8)
        assert digest_due(t2, nine + timedelta(days=1), 8)
        assert not digest_due(t2, nine + timedelta(days=1), -1)
    finally:
        t2.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_digest.py -q`
Expected: `ModuleNotFoundError: No module named 'notifications.digest'`.

- [ ] **Step 3: Implement**

`tracker/profit_tracker.py`:
- Set `SCHEMA_VERSION = 2`.
- In `_migrate`, before the final version bump, add:
```python
        if version < 2:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS chain_episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    peak_profit_erg REAL NOT NULL,
                    peak_profit_percent REAL NOT NULL,
                    peak_size_erg REAL NOT NULL,
                    last_profit_percent REAL,
                    trade TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_chain_episodes_opened ON chain_episodes(opened_at);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """)
```
- Where `__init__` calls `self._close_stale_episodes()`, also run:
```python
        self.conn.execute("UPDATE chain_episodes SET closed_at = opened_at WHERE closed_at IS NULL")
        self.conn.commit()
```
  This must come after `_migrate()`, so the table exists.
- New methods:
```python
    def open_chain_episode(self, ep) -> int:
        cur = self.conn.execute(
            """INSERT INTO chain_episodes (path, opened_at, peak_profit_erg, peak_profit_percent, peak_size_erg,
               last_profit_percent) VALUES (?, ?, ?, ?, ?, ?)""",
            (ep.label, datetime.now().isoformat(), ep.peak_erg, ep.peak_percent, ep.peak_size_erg, ep.profit_percent))
        self.conn.commit()
        return cur.lastrowid

    def update_chain_episode(self, episode_id: int, ep):
        self.conn.execute(
            """UPDATE chain_episodes SET peak_profit_erg = ?, peak_profit_percent = ?, peak_size_erg = ?,
               last_profit_percent = ? WHERE id = ?""",
            (ep.peak_erg, ep.peak_percent, ep.peak_size_erg, ep.profit_percent, episode_id))
        self.conn.commit()

    def close_chain_episode(self, episode_id: int, ep):
        self.update_chain_episode(episode_id, ep)
        self.conn.execute("UPDATE chain_episodes SET closed_at = ?, trade = ? WHERE id = ?",
                          (datetime.now().isoformat(), ep.trade, episode_id))
        self.conn.commit()

    def chain_episodes_since(self, since_iso: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM chain_episodes WHERE opened_at >= ? ORDER BY opened_at",
                                 (since_iso,)).fetchall()
        return [dict(r) for r in rows]

    def trades_since(self, since_iso: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM trades WHERE started_at >= ? ORDER BY started_at",
                                 (since_iso,)).fetchall()
        return [dict(r) for r in rows]

    def get_meta(self, key: str):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str):
        self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self.conn.commit()
```
(`self.conn.row_factory` is already `sqlite3.Row`; `dict(r)` works. Check that with `grep -n row_factory tracker/profit_tracker.py`; if it is not set, build dicts from `cursor.description`.)

`notifications/digest.py`:

```python
"""Daily Discord digest: episodes, trades and outages over the last 24 h (once per day)."""
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

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


def build_digest(tracker, health, wallet: Optional[dict], now: datetime, hours: int = 24) -> Digest:
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
    wall_now = time.time()
    since_wall = max(wall_now - hours * 3600, health.started_at)
    d.outages = health.outages(now=wall_now, since=since_wall) if health else []
    d.outage_since = f"since {datetime.fromtimestamp(since_wall):%Y-%m-%d %H:%M}"
    return d


def digest_due(tracker, now: datetime, hour: int) -> bool:
    return hour >= 0 and now.hour >= hour and tracker.get_meta(DIGEST_KEY) != now.date().isoformat()


def mark_digest_sent(tracker, now: datetime):
    tracker.set_meta(DIGEST_KEY, now.date().isoformat())
```

The health log uses wall-clock times (`time.time()`) so outages line up with the digest's 24 h window. The scanner therefore feeds `HealthMonitor.update` with `time.time()` (Task 6).

- [ ] **Step 4: Run to verify**

Run: `python -m pytest tests/test_digest.py -q`, then `python -m pytest -q`.
Expected: 6 passed; full suite all-pass (the tracker migration also runs on the existing test DBs).

- [ ] **Step 5: Commit**

```bash
git add tracker/profit_tracker.py notifications/digest.py tests/test_digest.py
git commit -m "chain_episodes + meta tables (schema v2) and the daily digest"
```

---

### Task 6: Scanner wiring, docs, verification, PR

**Files:**
- Modify: `arbitrage/scanner.py` (`__init__`, `poll_once`, `_notify_discord`, `scan_once`, `_execute_trades`, `run`)
- Modify: `README.md` (Discord section), `.env.example`
- Test: `tests/test_scanner_discord.py`

**Interfaces:**
- Consumes everything from Tasks 1–5.
- Produces: `ArbitrageScanner.episodes: EpisodeTracker`, `ArbitrageScanner.health: HealthMonitor`, `_discord_tick(now)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_scanner_discord.py`)

```python
"""Scanner -> Discord: one message per episode, health alerts, legacy flow only for CEX/USE (spec)."""
import asyncio

import pytest

import arbitrage.scanner as scanner_module
import config
from arbitrage.scanner import ArbitrageScanner
from ergo.chain_state import ChainSnapshot
from tests.test_bank_redeem_tx import BANK_BOX, ORACLE_BOX
from tests.test_chain_arb import pool_box
from tests.test_chain_scanner import HEALTHY, WALLET, Reader, snap


def losing():
    return ChainSnapshot(1_885_701, dict(pool_box(0.31, 2_000 * 10**9), boxId="pool-b"), BANK_BOX, ORACLE_BOX,
                         frozenset(), 5.0)


@pytest.fixture
def notify(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_ENABLED", True)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/x")
    monkeypatch.setattr(config, "DISCORD_CONFIRM_SECONDS", 4)
    monkeypatch.setattr(config, "DISCORD_CLOSE_SECONDS", 4)
    monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 1000.0)
    s = ArbitrageScanner(mode="notify", db_path=str(tmp_path / "n.db"), view="dashboard")
    s.posts, s.edits = [], []
    monkeypatch.setattr(s.discord, "post", lambda embed, content="", on_id=None:
                        (s.posts.append((embed, content)), on_id and on_id(f"m{len(s.posts)}")))
    monkeypatch.setattr(s.discord, "edit", lambda get_id, embed: s.edits.append((get_id(), embed)))

    async def healthy():
        return dict(HEALTHY)

    async def wallet():
        return dict(WALLET)

    async def no_text(content):  # the legacy text sender (e.g. the 30 min summary) must not hit the network
        return True

    monkeypatch.setattr(s.ergo_node, "get_health", healthy)
    monkeypatch.setattr(s, "_fetch_wallet_balances", wallet)
    monkeypatch.setattr(s.discord, "_send", no_text)
    yield s
    s.tracker.close()


def run(coro):
    return asyncio.run(coro)


def episode_posts(s):
    return [(e, c) for e, c in s.posts if e["title"].startswith("OPEN")]


def test_one_message_per_episode_then_a_close_edit(notify, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), snap(), snap(), snap(), snap(),
                                                                losing(), losing(), losing(), losing()))
    for t in (0, 2, 4, 6, 8):
        run(notify.poll_once(t))
    assert len(episode_posts(notify)) == 1
    for t in (10, 12, 14, 16):
        run(notify.poll_once(t))
    closes = [(mid, e) for mid, e in notify.edits if e["title"].startswith("Closed")]
    opened_at = next(i for i, (e, _) in enumerate(notify.posts) if e["title"].startswith("OPEN"))
    assert len(closes) == 1 and closes[0][0] == f"m{opened_at + 1}"  # the edit targets the episode's message
    (row,) = notify.tracker.chain_episodes_since("2000-01-01")
    assert row["closed_at"] is not None


def test_tier1_open_pings(notify, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_USER_ID", "42")
    notify.discord.user_id = "42"
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    for t in (0, 2, 4):
        run(notify.poll_once(t))
    (embed, content), = episode_posts(notify)
    assert content == "<@42>"  # +3.36% >= DISCORD_TIER1_PROFIT_PERCENT 2.0


def test_chain_outage_alerts_with_ping(notify, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_HEALTH_CHAIN_SECONDS", 0)
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), RuntimeError("node down")))
    notify.health.chain_s = 0
    run(notify.poll_once(0))
    run(notify.poll_once(2))
    alerts = [e for e, c in notify.posts if e["title"].startswith("⚠️")]
    assert alerts and "node down" in alerts[0]["title"]


def test_legacy_flow_skips_on_chain_paths(notify, monkeypatch):
    sent = []

    async def legacy(opps, scan_number=0):
        sent.extend(o.path_key for o in opps)
        return len(opps)

    monkeypatch.setattr(notify.discord, "notify_opportunities", legacy)
    monkeypatch.setattr(config, "DISCORD_CONFIRM_SCANS", 1)
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    run(notify.poll_once(0))
    assert not any(k in scanner_module.LIVE_PATHS for k in sent)


def test_monitor_mode_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_ENABLED", True)
    s = ArbitrageScanner(mode="monitor", db_path=str(tmp_path / "m.db"), view="dashboard")
    posts = []
    monkeypatch.setattr(s.discord, "post", lambda *a, **k: posts.append(a))
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    for t in (0, 2, 4, 6, 8, 10, 12):
        run(s.poll_once(t))
    assert posts == []
    s.tracker.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_scanner_discord.py -q`
Expected: failures (no episode posts; `notify.health` missing).

- [ ] **Step 3: Implement in `arbitrage/scanner.py`**

Imports:
```python
from notifications import embeds
from notifications.digest import build_digest, digest_due, mark_digest_sent
from notifications.episodes import EpisodeTracker
from notifications.health import HealthMonitor
```

`__init__` (after `self.state = DashboardState(mode)`):
```python
        self.episodes = EpisodeTracker(config.DISCORD_CONFIRM_SECONDS, config.DISCORD_CLOSE_SECONDS,
                                       config.DISCORD_EDIT_SECONDS, config.DISCORD_MIN_PROFIT_PERCENT,
                                       config.DISCORD_MIN_PROFIT_ERG)
        self.health = HealthMonitor(config.DISCORD_HEALTH_CHAIN_SECONDS, config.DISCORD_HEALTH_VENUE_SECONDS,
                                    config.DISCORD_HEALTH_ORACLE_SECONDS, config.DISCORD_HEALTH_REPEAT_SECONDS,
                                    started_at=time.time())
```

New methods:
```python
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
                              on_id=lambda mid, ep=ep: setattr(ep, "message_id", mid))
            self.state.add_event("info", f"Discord: opened {ep.label} {ep.profit_percent:+.2f}%")
        elif event.kind == "update":
            self.tracker.update_chain_episode(ep.db_id, ep)
            self.discord.edit(lambda ep=ep: ep.message_id, self._episode_embed(ep, "open"))
        else:
            self.tracker.close_chain_episode(ep.db_id, ep)
            self.discord.edit(lambda ep=ep: ep.message_id, self._episode_embed(ep, "closed"))
            self.state.add_event("info", f"Discord: closed {ep.label} (peak {ep.peak_percent:+.2f}%)")

    def _discord_tick(self, now: float):
        """Episodes and health alerts from the freshly refreshed state. Never awaits Discord."""
        if not self.discord_enabled:
            return
        try:
            for event in self.episodes.update(now, self.state.paths):
                self._handle_episode(event)
            for h in self.health.update(time.time(), self.state):
                self.discord.post(embeds.health_embed(h), content=self.discord._ping() if h.ping else "")
                self.state.add_event("warn" if h.kind == "alert" else "info", f"Discord: {h.text}")
        except Exception as e:
            logger.error(f"Discord tick error: {e}", exc_info=True)
```

`poll_once`: right after the `try/finally` that calls `self._refresh_state(now)`, add `self._discord_tick(now)`.

`_notify_discord`: in the loop that builds `confirmed`, skip the paths now handled by episodes:
```python
            if opp.path_key in LIVE_PATHS:
                continue  # on-chain paths: one edited message per episode (_discord_tick)
```

`scan_once`: after the periodic summary block, add the digest:
```python
        if self.discord_enabled and digest_due(self.tracker, datetime.now(), config.DISCORD_DIGEST_HOUR):
            self.discord.post(embeds.digest_embed(build_digest(self.tracker, self.health, self._last_wallet,
                                                               datetime.now())))
            mark_digest_sent(self.tracker, datetime.now())
```

`_execute_trades`: after the result `msg` is built, also attach it to the episode:
```python
        self.episodes.note_trade(key, f"{result.status}: {profit:+.4f} ERG" if result.status == "executed"
                                 else f"{result.status}: {result.message or ''}")
```

`run`'s `finally`, before `send_summary_message`:
```python
            if self.discord_enabled:
                for event in self.episodes.close_all(self._now, "bot stopped"):
                    self._handle_episode(event)
```
`disconnect_all` already calls `self.discord.disconnect()`, which now stops (drains) the queue first.

- [ ] **Step 4: Run to verify**

Run: `python -m pytest tests/test_scanner_discord.py -q`, then `python -m pytest -q`.
Expected: 5 passed; full suite all-pass.

- [ ] **Step 5: Docs**

README Discord section: add a short subsection "Discord messages":
- one message per on-chain opportunity, updated while open and closed with its duration and peak
- health alerts: chain ≥ 2 min (ping), venue ≥ 5 min, oracle stuck ≥ 10 min, live paused (ping)
- the daily digest at `DISCORD_DIGEST_HOUR`
- the settings table (the 8 new names and defaults)
- **Privacy:** the webhook receives wallet balances and the Discord user id; use a private channel

`.env.example`: add the 8 settings with one-line comments.

- [ ] **Step 6: Verification**

- Run `python -m pytest -q` and `python -m pytest -m live -q`. Both must pass.
- Run `python main.py --notify` for 2 minutes. The dashboard runs, and no Discord error appears in Events or `arbitrage.log`. (No opportunity is expected right now, so normally nothing is posted.)
- **Only with the user's go-ahead:** post one test episode embed to the real webhook, then edit it to closed, via a one-off script built on `DiscordNotifier.post`/`edit`/`stop` and `embeds.episode_embed`.

- [ ] **Step 7: Commit, then PR**

```bash
git add arbitrage/scanner.py tests/test_scanner_discord.py README.md .env.example
git commit -m "Scanner -> Discord: episode messages, health alerts, digest; CEX/USE keep the legacy flow"
```
Check the PR state, push `feature/discord-episodes`, and open a PR that closes #15 (Telegram is noted as a future follow-up).
