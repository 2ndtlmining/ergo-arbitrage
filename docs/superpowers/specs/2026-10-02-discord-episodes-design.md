# Discord: episode messages, health alerts, daily digest (issue #15)

Date: 2026-10-02. Status: implemented (PR #41). Design record; the README is authoritative.

## Goal

Discord should tell the user, without spamming, about four things:
- when an on-chain opportunity opens, how it develops, and when it closes (and how long it lasted)
- when something the bot depends on is down or stuck
- what live trading did
- once a day, what happened

## Scope

**In:**
- rich embeds
- one message per opportunity episode, edited while open and on close
- HTTP 429 retry
- health alerts with recoveries
- the wallet analysis as one embed
- a daily digest
- embed versions of the startup and shutdown messages
- a README privacy note

**Out:**
- Telegram (a later notifier can use the same interfaces)
- the CEX/USE paths: they are off for this user and keep the current Discord flow (`notify_opportunities`, confirm-by-scans, cooldown) unchanged until enabled
- the 30-minute text scan summary: unchanged

## Decisions

- **Source of truth:** episodes follow the exact on-chain sizing that the dashboard and the live gate use. The source is `DashboardState.paths`, a `PathRow` (`status`, `choice: SizeChoice`, `steps`) per `LIVE_PATHS` key, updated every chain poll (~2 s). The older full-scan calculator figures are not used for these paths.
- **Storage:** a new table `chain_episodes`. The existing `opportunity_episodes` table is filled from the 15-second grid and feeds tracker stats; it is left unchanged.
- **Delivery:**
  - One sender queue and one background worker task, so a 429 wait never blocks the 2-second poll.
  - Messages are posted with `?wait=true` to get the message id, and edited with `PATCH {webhook}/messages/{id}`.

## Episode lifecycle (`notifications/episodes.py`)

A path qualifies when its `status == "GO"`, `profit_percent >= DISCORD_MIN_PROFIT_PERCENT` and
`profit_erg >= DISCORD_MIN_PROFIT_ERG`.

| Transition | Rule |
|---|---|
| **pending → open** | qualifies continuously for `DISCORD_CONFIRM_SECONDS` (10). If it stops qualifying first, reset silently (no message). |
| **update** | while open, at most every `DISCORD_EDIT_SECONDS` (30), and only when `|last_pct − last_edited_pct| > 0.1` pp. The embed shows current and peak profit. |
| **closing → closed** | while open, stops qualifying for `DISCORD_CLOSE_SECONDS` (10); qualifying again before that cancels the close. On close, the message is edited to grey: "Closed after 6m 20s · peak +2.4% · last +0.3%", plus the trade result if live mode traded during the episode. |
| **shutdown** | open episodes are closed ("Closed: bot stopped"). |

```python
@dataclass
class Episode:
    key: str; label: str
    opened_at: float; last_seen_at: float
    size_erg: float; profit_erg: float; profit_percent: float; break_even_erg: Optional[float]
    peak_erg: float; peak_percent: float; peak_size_erg: float
    steps: list[str]
    message_id: Optional[str] = None
    last_edit_at: float = 0.0; last_edit_percent: float = 0.0
    trade: Optional[str] = None          # e.g. "executed +0.42 ERG" / "leg 2 failed"
    closed_at: Optional[float] = None
    db_id: Optional[int] = None

@dataclass
class EpisodeEvent:
    kind: str        # "open" | "update" | "close"
    episode: Episode

class EpisodeTracker:
    def __init__(self, confirm_s, close_s, edit_s, min_percent, min_erg)
    def update(self, now: float, rows: dict[str, PathRow]) -> list[EpisodeEvent]
    def note_trade(self, key: str, text: str)        # attaches a live result to the open episode
    def close_all(self, now: float, reason: str) -> list[EpisodeEvent]
```

## Health alerts (`notifications/health.py`)

`HealthMonitor.update(now, state: DashboardState) -> list[HealthEvent]`. A `HealthEvent` has a
subject, a kind (`alert` | `recovered`), text, and ping (bool).

| Subject | Alert when | Ping | Recovery |
|---|---|---|---|
| `chain` | `state.chain_error` set for ≥ `DISCORD_HEALTH_CHAIN_SECONDS` (120) | yes | when readable again, "after 4m 10s" |
| `venue:<name>` | a venue `state == "down"` for ≥ `DISCORD_HEALTH_VENUE_SECONDS` (300). The pool, bank and oracle are covered by `chain` and are not alerted separately. | no | when no longer down |
| `oracle` | Oracle venue `state == "pending"` for ≥ `DISCORD_HEALTH_ORACLE_SECONDS` (600) | no | when it confirms |
| `live` | `state.live == "paused"` | yes | (none: a pause lasts until restart) |

- **Repeats:** a subject alerts again only after `DISCORD_HEALTH_REPEAT_SECONDS` (1800) if it is still failing.
- **Recoveries:** a recovery is sent only if an alert was sent, and always is.
- **Outage log:** every alert and recovery is also kept in memory for the digest (outages per subject with durations).

## Notifier (`notifications/discord.py`)

New API alongside the existing methods (which stay for CEX/USE and live text alerts):

```python
async def start(self)                      # starts the sender worker (called from connect)
async def stop(self, timeout=10)           # drains the queue, stops the worker
def post(self, embed: dict, content: str = "", on_id: Callable[[str], None] | None = None)  # enqueue
def edit(self, get_id: Callable[[], Optional[str]], embed: dict)                          # enqueue
async def _request(self, method, url, payload) -> tuple[int, Optional[dict]]   # one retry on 429
```

- **429 handling:** on HTTP 429, sleep `min(retry_after, 30)` (JSON `retry_after`, else the `Retry-After` header, else 5 s) and retry once. When `X-RateLimit-Remaining == 0`, sleep `X-RateLimit-Reset-After` (≤ 30 s) before the next request.
- **Edits:** they resolve the message id at send time through `get_id()`. FIFO order guarantees the open's post has finished. An edit whose id is still None (the post failed) is dropped and logged.
- **Field limits:** title 256, field value 1024, description 4096. Text is cut with "…".
- **Explorer links:** `https://explorer.ergoplatform.com/en/token/{NFT}` for the pool and the bank.

Embed builders (pure functions, `notifications/embeds.py`):
- `episode_embed(ep, status: "open"|"closed", height, data_age_s) -> dict`
  - colour: green `0x2ECC71` open, grey `0x95A5A6` closed
  - fields: size, profit (ERG / % / USD via the oracle), break-even, peak, duration, steps, links
  - footer: `h{height} · data {age}s`
- `health_embed(event) -> dict`: red `0xE74C3C` alert, green recovery, yellow `0xF1C40F` for non-ping alerts
- `wallet_embed(wallet, analysis) -> dict`: one field per asset, using `.get()` everywhere
- `digest_embed(d: Digest) -> dict`
- `startup_embed(mode, settings) -> dict`, `shutdown_embed(stats) -> dict`

## Daily digest (`notifications/digest.py`)

- **When:** at the first full scan after `DISCORD_DIGEST_HOUR` (8, local; −1 disables) on a day the digest has not been sent yet. The last sent date is stored in a tracker `meta` table (key/value), so a restart does not resend.
- **Contents:**
  - **episodes:** for each path over the last 24 h, the count, the longest episode and the best peak %
  - **potential ERG:** the sum of `peak_erg` over those episodes
  - **trades:** count, net ERG, failures (from `trades` over the last 24 h)
  - **outages:** per subject, with total downtime (from the HealthMonitor log, which only covers time since this process started; this is stated in the embed)
  - **wallet:** ERG and SigUSD
- `build_digest(tracker, health_log, wallet, now) -> Digest`, then `digest_embed`.

## Tracker

Schema migration `user_version` 2 adds:

```sql
CREATE TABLE IF NOT EXISTS chain_episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL,
  opened_at TEXT NOT NULL, closed_at TEXT,
  peak_profit_erg REAL NOT NULL, peak_profit_percent REAL NOT NULL, peak_size_erg REAL NOT NULL,
  last_profit_percent REAL, trade TEXT);
CREATE INDEX IF NOT EXISTS ix_chain_episodes_opened ON chain_episodes(opened_at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
```

Methods:
- `open_chain_episode(ep) -> id`
- `update_chain_episode(id, ep)`
- `close_chain_episode(id, ep)`
- `chain_episodes_since(iso) -> list[dict]`
- `trades_since(iso) -> list[dict]`
- `get_meta(key)` / `set_meta(key, value)`
- close stale open `chain_episodes` at startup, like `_close_stale_episodes`

## Scanner wiring

In notify/live modes (`self.discord_enabled`):
- **After `_refresh_state` on every poll:**
  - `episodes.update(now, state.paths)`. For each event: `open` → `tracker.open_chain_episode`, `discord.post(episode_embed(...), content=ping if tier1, on_id=set ep.message_id)`. `update` → `tracker.update_chain_episode`, `discord.edit(...)`. `close` → `tracker.close_chain_episode`, `discord.edit(...)`. Add a dashboard event for opens and closes ("Discord: opened …").
  - `health.update(now, state)` → `discord.post(health_embed(e), content=ping if e.ping)`
- **`_notify_discord`:** skip `LIVE_PATHS` keys. Those paths are now handled by episodes; the CEX/USE paths keep the old flow.
- **Wallet analysis:** `send_wallet_analysis` becomes one embed, posted via the queue, on the same interval.
- **Digest:** checked on each full scan.
- **Live trade results:** `notify_live` (text, ping) is unchanged; `episodes.note_trade(key, text)` also attaches the result to the episode.
- **Startup and shutdown:**
  - startup: the embed version of today's message
  - shutdown: `episodes.close_all`, then the shutdown embed, then `discord.stop()` (drain ≤ 10 s)
- **Pings:** `<@DISCORD_USER_ID>` in `content`, only for:
  - tier-1 opens (`profit_percent >= DISCORD_TIER1_PROFIT_PERCENT`)
  - `chain` and `live` health alerts
  - the existing `notify_live`

## Config

| Name | Default |
|---|---|
| `DISCORD_CONFIRM_SECONDS` | 10 |
| `DISCORD_CLOSE_SECONDS` | 10 |
| `DISCORD_EDIT_SECONDS` | 30 |
| `DISCORD_HEALTH_CHAIN_SECONDS` | 120 |
| `DISCORD_HEALTH_VENUE_SECONDS` | 300 |
| `DISCORD_HEALTH_ORACLE_SECONDS` | 600 |
| `DISCORD_HEALTH_REPEAT_SECONDS` | 1800 |
| `DISCORD_DIGEST_HOUR` | 8 (−1 = off) |

`DISCORD_CONFIRM_SCANS` and `DISCORD_COOLDOWN_SECONDS` remain, for the CEX/USE flow only.

## Error handling

- **Discord down or slow:** the worker logs and drops after the retry. The queue is bounded (100). When it is full, the oldest non-edit job is dropped with a warning. Nothing in the poll path awaits Discord.
- **Edit of a deleted message** (404): logged; the episode keeps running and the next open posts a new message.
- **Notifier exceptions** are caught in the worker; the scanner never sees them.
- **Shutdown:** the queue is drained with a 10-second cap.

## Docs

- README Discord section:
  - the episode messages, health alerts and digest, with their settings
  - privacy: the webhook receives wallet balances and the Discord user id, so use a private channel
- `.env.example`: the new settings.

## Testing

- **Episodes:**
  - open only after confirm seconds; a blip below confirm sends nothing
  - update throttled by time and by delta
  - a close needs close seconds; a re-qualify cancels the close
  - peak tracking
  - `note_trade` shows on close
  - `close_all`
- **Health:**
  - each subject's threshold
  - no repeat within repeat seconds
  - recovery only after an alert
  - pool/bank/oracle "down" are not alerted as venues
  - ping flags
- **Notifier, with a fake webhook (aiohttp-shaped):**
  - post returns the id
  - edit uses it
  - a 429 waits and retries once, then gives up
  - rate-limit headers are honoured
  - a full queue drops the oldest post
  - the worker survives exceptions
  - `stop` drains
- **Embeds:** limits and truncation, colours, wallet embed with missing keys, digest with no data.
- **Digest:** `build_digest` from a temporary tracker DB; scheduling (hour, once per day, survives a restart via `meta`).
- **Scanner:**
  - with discord enabled and a fake notifier, a GO path for 5 polls → exactly one post, then a close edit after it stops
  - CEX/USE flow unchanged
  - nothing awaits Discord in the poll path
- **Live check:** only with the user's go-ahead, post a test episode embed to the real webhook, then edit it to closed.
