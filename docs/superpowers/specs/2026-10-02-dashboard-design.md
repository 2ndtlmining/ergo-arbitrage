# Live terminal dashboard (issue #14)

Date: 2026-10-02. Status: implemented (PR #38, extended in #54). Design record; the README is authoritative.

## Goal

Replace the roughly 100 lines of scrolling output per scan with one screen the user
watches live. It shows at a glance what the bot sees, what it would do, and why it is
or is not trading.

- **Main use:** a person watching it in a terminal. The dashboard is the default.
- **Other uses:** logs, piping and occasional unattended runs, via `--plain` (today's
  output) and `--json`.

**Out of scope:**
- interactivity (keys, scrolling); the STOP file stays the kill switch
- the venue/leg refactor (#13)
- the web dashboard (#16)
- Discord changes (#15)
- new venues

## Decisions

- **Approach:** `rich.live.Live` with a `Layout`, read-only, Ctrl+C quits. `rich` is
  already used everywhere and works in any terminal including Windows; the render
  function is pure and testable. Textual was rejected: it would own the event loop and
  logging, so it needs more restructuring.
- **One state, three views:** the scanner fills a `DashboardState`.
  - The dashboard (default) renders it.
  - `--plain` keeps today's prints.
  - `--json` writes one line per full scan.

  Computing code does not change.
- **Venues registry:** a small list of venues with name, kind, enabled setting and a
  quote formatter, at most 7 rows. Adding a venue later is one entry plus the scanner
  reporting its result. This is not the #13 abstraction.
- **CLI:** `--sizes` and `--paths` from #14 are skipped; with two on-chain paths they add
  little, and `.env` `TRADE_SIZES` remains.

## Layout

```
 ERGO ARB  LIVE  h1885693  node ● 6 ms  wallet ●  poll 2s  full scan in 9s  confirm 2  08:41:07
┌ Prices ──────────────────────────────────┐┌ Live ──────────────────────────────────┐
│ Pool   0.3127 SigUSD/ERG  20,114 ERG res ││ not armed: oracle update pending       │
│ Oracle $0.3242/ERG   SigUSD peg +3.7%    ││ trades today 0/10  drawdown 0.00/5 ERG │
│ Bank   RR 330%  redeem ✓  mint ✗ (<400%) │├ Wallet ────────────────────────────────┤
└──────────────────────────────────────────┘│ 20.7119 ERG  0.00 SigUSD  ~$6.72       │
                                            └────────────────────────────────────────┘
┌ Paths ───────────────────────────────────────────────────────────────────────────────┐
│ path            size     profit      %      b/even  conf  status    last 30           │
│ pool→redeem     —        -0.066      —      —       0/2   no edge   ▁▁▂▁▁▁▂▃          │
│ mint→pool sell  —        —           —      —       —     BLOCKED   RR 330% < 400%    │
│ (steps of a GO path are listed under the table)                                       │
└───────────────────────────────────────────────────────────────────────────────────────┘
┌ Events ──────────────────────────────────────────────────────────────────────────────┐
│ 08:40:51 CHAIN pool, bank changed (pending: pool)                                     │
│ 08:39:02 oracle update confirmed                                                      │
└───────────────────────────────────────────────────────────────────────────────────────┘
┌ Venues ──────────────────────────────────────────────────────────────────────────────┐
│ venue          kind      status            quote                 age   ms            │
│ ErgoDEX pool   on-chain  ● live            0.3127 SigUSD/ERG     1s    6             │
│ SigmaUSD bank  on-chain  ● live            RR 330%, mint ✗       1s    6             │
│ Oracle         on-chain  ◐ update pending  $0.3242/ERG           1s    6             │
│ Dexy USE       on-chain  – disabled        ENABLE_USE=false                          │
│ Kucoin         CEX       ○ watch only      $0.3301 bid/ask …     4s    210           │
│ NonKYC         CEX       ○ watch only      …                                         │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

- **Header:** mode (LIVE in red), block height, node and wallet health, poll interval,
  countdown to the next full scan, `LIVE_CONFIRM_POLLS`, clock.
- **Prices:** on-chain only (pool spot and ERG reserve, oracle, SigUSD peg vs the oracle,
  bank RR with redeem/mint flags). CEX prices appear in Venues.
- **Live:**
  - monitor/notify mode: "trading off (monitor)"
  - otherwise: "ARMED" when nothing blocks, else the blocker reasons (path blockers
    for the best path plus global blockers); "paused: …" while paused
  - always: trades today vs the limit, and drawdown vs `LIVE_MAX_DRAWDOWN_ERG`
- **Wallet:** ERG, SigUSD, total value in ERG and USD, stale-box count if any. Hidden
  with `--no-wallet`.
- **Paths:** one row per on-chain path, from `last_sizing` and the live streaks.
  - **Status:** GO (sizing ok), no edge (not profitable), BLOCKED (mint RR), stale
    (chain state unavailable).
  - **last 30:** sparkline of the path's best profit % over the last 30 polls.
  - **Steps:** when a path is GO, its `ArbitrageOpportunity.steps` are listed under the
    table (one GO path, the best).
- **Events:** the newest 10, from the generated events (below).
- **Venues:** see Registry. At most 7 rows; disabled venues stay listed, dimmed.

## Components

### `arbitrage/dashboard_state.py` (new)

```python
@dataclass
class VenueStatus:
    name: str
    kind: str              # "on-chain" | "CEX"
    state: str             # "live" | "pending" | "down" | "watch" | "disabled"
    quote: str = ""
    age_s: Optional[float] = None
    latency_ms: Optional[float] = None
    error: str = ""

@dataclass
class PathRow:
    key: str               # LIVE_PATHS key
    label: str             # "pool→redeem"
    choice: Optional[SizeChoice]
    streak: int
    status: str            # "GO" | "no edge" | "BLOCKED" | "stale"
    detail: str            # reason text for BLOCKED/no edge
    history: deque[float]  # best profit % per poll, maxlen 30
    steps: list[str]

class DashboardState:
    mode, height, scan_count, next_full_scan_in, node_ok, wallet_ok, read_ms
    prices: dict           # the on-chain price entries
    venues: list[VenueStatus]
    paths: dict[str, PathRow]
    live: str; live_detail: list[str]; trades_today: int; drawdown: float
    wallet: Optional[dict]
    events: deque[tuple[datetime, str, str]]   # (time, level, text), maxlen 200

    def add_event(level, text)
    def update_paths(last_sizing, streaks, chain_error, opportunities_by_key)  # emits opened/closed
    def update_venues(rows)   # emits down/recovered transitions
    def to_json() -> dict
```

Generated events:
- an opportunity opens (a path becomes GO) or closes, with its profit
- a venue goes down or recovers
- an oracle update is pending, then confirmed
- a CHAIN change (which boxes changed)
- live blocked/armed transitions
- trade progress (runner log lines)
- Discord alert sent
- warnings and errors from the `ergo_arb` logger

### `arbitrage/venues.py` (new): registry

```python
@dataclass(frozen=True)
class Venue:
    name: str
    kind: str
    enabled: Callable[[], bool]          # e.g. lambda: config.ENABLE_CEX or config.CEX_WATCH
    describe: Callable[[dict], VenueStatus]   # from the scanner's prices dict + timestamps
VENUES: tuple[Venue, ...]   # ErgoDEX pool, SigmaUSD bank, Oracle, Dexy USE, Kucoin, NonKYC (<= 7)
```

### `arbitrage/dashboard_view.py` (new)

`render(state: DashboardState) -> Layout`, a pure function. The panels match the layout
above; the event and venue lists are truncated to fit.

### Scanner

- `ArbitrageScanner(view="dashboard"|"plain"|"json")`.
- The existing display methods (`_display_prices`, `_display_opportunities`,
  `_display_wallet_opportunities`, `_display_cex_watch`, blocker lines, the CHAIN line, the
  startup panel) run only when `view == "plain"`.
- The scanner always fills `self.state` at the end of `poll_once` and `scan_once`. Those
  are the two places that already have sizing, streaks, blockers, wallet and prices.
- During a live trade, `run_arb` gets `log=` a function that writes to the plain console
  or adds an event, and always writes to the log file.
- `--json`: after each full scan, print `json.dumps(state.to_json())` on one line.
- Dashboard mode: `main.py` runs `Live(get_renderable=lambda: render(scanner.state),
  refresh_per_second=2, screen=True)` around `scanner.run()`.

### Logging (`logging_config.py`)

- **File:** `TimedRotatingFileHandler("arbitrage.log", when="midnight", backupCount=14)`,
  level from `--log-level` (default INFO). Each full scan logs one INFO summary line.
- **Console:**
  - plain: `RichHandler`, as today
  - dashboard: a `logging.Handler` that adds WARNING+ records to `state.events`
  - json: no console log handler

### CLI (`main.py`)

| Flag | Effect |
|---|---|
| `--notify` / `--live` | unchanged |
| `--plain` | today's scrolling output |
| `--json` | one JSON line per full scan, nothing else on stdout |
| `--once` | one full scan (with the chosen view; the dashboard prints its final frame once), then exit |
| `--interval N` | overrides `SCAN_INTERVAL_SECONDS` |
| `--max-trade-erg X` | overrides `MAX_TRADE_SIZE_ERG` |
| `--log-level L` | file log level |
| `--db PATH` | tracker database path |
| `--no-wallet` | no wallet panel or wallet analysis |

`--plain` and `--json` are mutually exclusive. The startup banner in `main.py` prints
only in plain mode.

### Docs

- README: `cd ergo_arbitrage` becomes `cd ergo-arbitrage`; usage gets the new flags and a
  dashboard screenshot-in-text.
- README and FLOWS: SigUSD pool swaps are direct (miner fee only, no ~0.785 ERG Crux
  service fee) unless `POOL_SWAP_ROUTE=crux`; update the stale examples and fee rows.

## Error handling

- A render error must never stop the scanner: the render function catches exceptions and
  shows them in the Events panel.
- **Terminal too small:** panels shrink, and lists are truncated before the Paths table.
- A chain outage shows "stale" on paths and "down" on the pool/bank/oracle venues; the Live
  panel shows the blocker.

## Testing

- **`DashboardState`:**
  - opened/closed events on GO transitions
  - venue down/recovered
  - oracle pending → confirmed
  - history capped at 30, events capped at 200
  - `to_json()` schema
- **Venues:** enabled/disabled per config, quote text from prices, max 7 rows.
- **`render`:** render to text with `Console(record=True, width=120)` and check:
  - each panel's title and key values
  - GO steps shown
  - disabled venues dimmed
  - a render exception lands in Events
- **Scanner:**
  - plain view still prints today's output (existing tests)
  - dashboard view prints nothing during `poll_once`/`scan_once`
  - state is filled after both
  - `--json` prints exactly one parseable line per full scan
- **CLI:** parsing, flag overrides applied to config, `--once` exits after one full scan.
- **Logging:** rotating handler configured; dashboard handler routes warnings to events.
- **Live (read-only):** `python main.py --once --plain`, `--once --json`, and a 2-minute
  dashboard run against the node.
