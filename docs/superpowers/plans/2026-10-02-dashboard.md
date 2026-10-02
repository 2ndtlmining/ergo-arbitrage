# Live Terminal Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `python main.py` shows a live one-screen dashboard (header, prices, live, wallet, paths, events, venues). `--plain` keeps today's scrolling output and `--json` prints one line per full scan.

**Architecture:**
- `DashboardState` (new) holds what any view shows; the scanner fills it after every poll and full scan.
- `arbitrage/venues.py` describes each monitored venue from the scanner's price data.
- `arbitrage/dashboard_view.render(state)` is a pure `rich` renderer, driven by `rich.live.Live` from `main.py`.
- The scanner's existing printing goes through `self.out`: the real console in plain mode, a quiet console otherwise.

**Tech Stack:** Python 3.13, rich 14.2 (`Live(get_renderable=...)`, `Layout`, `Table`, `Panel`), asyncio, pytest.

**Spec:** `docs/superpowers/specs/2026-10-02-dashboard-design.md`

## Global Constraints

- Default view in `main.py` is the dashboard. `ArbitrageScanner(view=...)` defaults to `"plain"`, so library callers and existing tests keep today's output.
- At most 7 venue rows. Disabled venues are listed, dimmed.
- Events kept: 200. Events shown: the newest 10. Path history: the last 30 polls.
- Dashboard refresh: `refresh_per_second=2`, `screen=True`. With `--once`: `screen=False`, so the final frame stays.
- Log file: `arbitrage.log` via `TimedRotatingFileHandler(when="midnight", backupCount=14)`, level from `--log-level` (default INFO).
- `--json`: stdout carries only JSON lines (one per full scan).
- No new dependencies.

## Review Focus

1. **Terminal narrower or shorter than the layout** (80×24): render must not raise. Test in Task 3.
2. **Dashboard before the first scan finishes** (empty state): render shows placeholders, not an exception. Test in Task 3.
3. **A watched CEX with no quote** (API down) and a venue describe function raising: the row shows "down" and the other rows still render. Test in Task 2.
4. **`--json` stdout must be clean:** no rich output or log lines mixed in. Test in Task 4.
5. **A bug in the renderer must not stop the bot:** the error appears in Events and scanning continues. Test in Task 3.

---

### Task 1: `arbitrage/dashboard_state.py`

**Files:**
- Create: `arbitrage/dashboard_state.py`
- Test: `tests/test_dashboard_state.py`

**Interfaces:**
- Consumes: `arbitrage.sizing.SizeChoice` (fields `ok`, `reason`, `size_erg`, `profit_erg`, `profit_percent`, `cost_erg`, `max_profit_size_erg`, `max_profit_erg`, `break_even_erg`, `summary()`).
- Produces:
  - `HISTORY = 30`, `MAX_EVENTS = 200`, `MAX_VENUES = 7`
  - `PATH_LABELS: dict[str, str]` (scanner path key → short label)
  - `@dataclass VenueStatus(name, kind, state, quote="", age_s=None, latency_ms=None, error="")`, with `state` one of `live|pending|watch|down|disabled`
  - `@dataclass PathRow(key, label, choice=None, streak=0, status="no data", detail="", history=deque(maxlen=30), steps=[])`
  - `path_status(choice, chain_error) -> tuple[str, str]`
  - `class DashboardState(mode)` with attributes:
    - `mode, height, scan_count, next_full_scan_in, node_ok, wallet_ok, read_ms, chain_error`
    - `prices: dict, venues: list[VenueStatus], paths: dict[str, PathRow]`
    - `live: str` (`off|armed|blocked|paused`), `live_detail: list[str]`, `trades_today: int`, `drawdown: float`
    - `wallet: Optional[dict]`
    - `events: deque[(datetime, level, text)]`

    and methods:
    - `add_event(level, text)`
    - `update_paths(sizing, streaks, chain_error, steps_by_key)`
    - `update_venues(venues)`
    - `set_live(live, detail)`
    - `to_json() -> dict`

- [ ] **Step 1: Write the failing tests** (`tests/test_dashboard_state.py`)

```python
"""DashboardState: what every view shows, plus the events it generates (spec: dashboard)."""
import json

from arbitrage.dashboard_state import MAX_EVENTS, DashboardState, VenueStatus, path_status
from arbitrage.sizing import SizeChoice

POOL = "Spectrum buy->Bank redeem"
MINT = "Bank mint->Spectrum sell"


def go(pct=3.0):
    return SizeChoice("redeem", size_nanoerg=40 * 10**9, sigusd_cents=1400, profit_erg=1.2, profit_percent=pct,
                      cost_erg=40.0, max_profit_size_erg=55.0, max_profit_erg=1.5, break_even_erg=0.1, cap_erg=100)


def no_edge():
    return SizeChoice("redeem", max_profit_size_erg=1.0, max_profit_erg=-0.06, cap_erg=100,
                      reason="no profitable size up to 100.00 ERG")


def blocked():
    return SizeChoice("mint", cap_erg=100, reason="bank mint blocked (RR 330%, must stay >= 400% after minting)")


def texts(state):
    return [t for _, _, t in state.events]


def test_path_status():
    assert path_status(go(), None)[0] == "GO"
    assert path_status(no_edge(), None)[0] == "no edge"
    assert path_status(blocked(), None)[0] == "BLOCKED"
    assert path_status(go(), "node down") == ("stale", "chain state unavailable (node down)")
    assert path_status(None, None)[0] == "no data"


def test_opportunity_opened_and_closed_events():
    s = DashboardState("live")
    s.update_paths({POOL: no_edge(), MINT: blocked()}, {}, None, {})
    assert not any("opportunity" in t for t in texts(s))
    s.update_paths({POOL: go(), MINT: blocked()}, {POOL: 1}, None, {POOL: ["step 1", "step 2"]})
    assert any(t.startswith("opportunity opened: pool→redeem") for t in texts(s))
    assert s.paths[POOL].status == "GO" and s.paths[POOL].streak == 1 and s.paths[POOL].steps == ["step 1", "step 2"]
    s.update_paths({POOL: no_edge(), MINT: blocked()}, {}, None, {})
    assert any(t.startswith("opportunity closed: pool→redeem") for t in texts(s))


def test_history_is_capped_and_skips_stale_polls():
    s = DashboardState()
    for _ in range(40):
        s.update_paths({POOL: go(2.0)}, {}, None, {})
    assert len(s.paths[POOL].history) == 30
    s.update_paths({POOL: go(2.0)}, {}, "node down", {})
    assert len(s.paths[POOL].history) == 30 and s.paths[POOL].status == "stale"


def test_venue_down_recovered_and_oracle_pending_events():
    s = DashboardState()
    live = VenueStatus("ErgoDEX pool", "on-chain", "live")
    s.update_venues([live, VenueStatus("Oracle", "on-chain", "live")])
    s.update_venues([VenueStatus("ErgoDEX pool", "on-chain", "down", error="timeout"),
                     VenueStatus("Oracle", "on-chain", "pending")])
    s.update_venues([live, VenueStatus("Oracle", "on-chain", "live")])
    t = texts(s)
    assert "ErgoDEX pool down: timeout" in t and "ErgoDEX pool recovered" in t
    assert "Oracle: update pending" in t and "Oracle: update confirmed" in t


def test_pool_pending_is_not_an_event():
    s = DashboardState()
    s.update_venues([VenueStatus("ErgoDEX pool", "on-chain", "live")])
    s.update_venues([VenueStatus("ErgoDEX pool", "on-chain", "pending")])
    assert texts(s) == []


def test_at_most_seven_venues():
    s = DashboardState()
    s.update_venues([VenueStatus(f"v{i}", "CEX", "live") for i in range(9)])
    assert len(s.venues) == 7


def test_live_category_change_is_an_event_detail_change_is_not():
    s = DashboardState("live")
    s.set_live("blocked", ["cooldown 120s"])
    s.set_live("blocked", ["cooldown 118s"])
    s.set_live("armed", [])
    assert texts(s) == ["live: armed"]


def test_events_are_capped():
    s = DashboardState()
    for i in range(MAX_EVENTS + 50):
        s.add_event("info", f"e{i}")
    assert len(s.events) == MAX_EVENTS and texts(s)[-1] == f"e{MAX_EVENTS + 49}"


def test_to_json_is_serialisable_and_complete():
    s = DashboardState("live")
    s.height, s.scan_count = 1885700, 3
    s.update_paths({POOL: go(), MINT: blocked()}, {POOL: 2}, None, {})
    s.update_venues([VenueStatus("ErgoDEX pool", "on-chain", "live", quote="0.3127 SigUSD/ERG")])
    s.set_live("armed", [])
    s.wallet = {"erg": 20.7, "sigusd": 0.0}
    d = json.loads(json.dumps(s.to_json()))
    assert d["height"] == 1885700 and d["live"]["state"] == "armed"
    assert d["paths"]["pool→redeem"] == {"status": "GO", "size_erg": 40.0, "profit_erg": 1.2, "profit_percent": 3.0,
                                         "break_even_erg": 0.1, "streak": 2, "reason": ""}
    assert d["paths"]["mint→pool sell"]["status"] == "BLOCKED" and d["paths"]["mint→pool sell"]["size_erg"] is None
    assert d["venues"][0]["quote"] == "0.3127 SigUSD/ERG" and d["wallet"]["erg"] == 20.7
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_dashboard_state.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'arbitrage.dashboard_state'`.

- [ ] **Step 3: Implement `arbitrage/dashboard_state.py`**

```python
"""What the dashboard, --json and the plain views show. The scanner fills it; views only read it.

It also turns changes into events: an opportunity opening or closing, a venue going down
or recovering, an oracle update pending/confirmed, live trading armed/blocked/paused.
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from arbitrage.sizing import SizeChoice

HISTORY = 30
MAX_EVENTS = 200
MAX_VENUES = 7
PATH_LABELS = {"Spectrum buy->Bank redeem": "pool→redeem", "Bank mint->Spectrum sell": "mint→pool sell"}


@dataclass
class VenueStatus:
    name: str
    kind: str                       # "on-chain" | "CEX"
    state: str                      # live | pending | watch | down | disabled
    quote: str = ""
    age_s: Optional[float] = None
    latency_ms: Optional[float] = None
    error: str = ""


@dataclass
class PathRow:
    key: str
    label: str
    choice: Optional[SizeChoice] = None
    streak: int = 0
    status: str = "no data"         # GO | no edge | BLOCKED | stale | no data
    detail: str = ""
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    steps: list = field(default_factory=list)


def path_status(choice: Optional[SizeChoice], chain_error: Optional[str]) -> tuple[str, str]:
    if chain_error:
        return "stale", f"chain state unavailable ({chain_error})"
    if choice is None:
        return "no data", "pool reserves or bank state missing"
    if choice.ok:
        return "GO", ""
    if "blocked" in choice.reason:
        return "BLOCKED", choice.reason
    return "no edge", choice.reason


def _best_percent(choice: Optional[SizeChoice]) -> float:
    if choice is None:
        return 0.0
    if choice.ok:
        return choice.profit_percent
    if choice.max_profit_size_erg > 0:
        return choice.max_profit_erg / choice.max_profit_size_erg * 100
    return 0.0


class DashboardState:
    def __init__(self, mode: str = "monitor"):
        self.mode = mode
        self.height = 0
        self.scan_count = 0
        self.next_full_scan_in: Optional[float] = None
        self.node_ok: Optional[bool] = None
        self.wallet_ok: Optional[bool] = None
        self.read_ms: Optional[float] = None
        self.chain_error: Optional[str] = None
        self.prices: dict = {}
        self.venues: list[VenueStatus] = []
        self.paths: dict[str, PathRow] = {}
        self.live = ""
        self.live_detail: list[str] = []
        self.trades_today = 0
        self.drawdown = 0.0
        self.wallet: Optional[dict] = None
        self.events: deque = deque(maxlen=MAX_EVENTS)

    def add_event(self, level: str, text: str):
        """level: info | good | warn | error | trade"""
        self.events.append((datetime.now(), level, text))

    def update_paths(self, sizing: dict, streaks: dict, chain_error: Optional[str], steps_by_key: dict):
        for key, label in PATH_LABELS.items():
            row = self.paths.setdefault(key, PathRow(key, label))
            choice = sizing.get(key)
            status, detail = path_status(choice, chain_error)
            if status == "GO" and row.status != "GO":
                self.add_event("good", f"opportunity opened: {label} {choice.summary()}")
            elif row.status == "GO" and status != "GO":
                self.add_event("info", f"opportunity closed: {label} ({detail or status})")
            row.choice, row.status, row.detail, row.streak = choice, status, detail, streaks.get(key, 0)
            if status != "stale":
                row.history.append(_best_percent(choice))
            if key in steps_by_key:
                row.steps = list(steps_by_key[key])

    def update_venues(self, venues: list[VenueStatus]):
        before = {v.name: v.state for v in self.venues}
        for v in venues:
            was = before.get(v.name)
            if was is None:
                continue
            if v.state == "down" and was != "down":
                self.add_event("warn", f"{v.name} down: {v.error or 'no data'}")
            elif was == "down" and v.state != "down":
                self.add_event("info", f"{v.name} recovered")
            if v.name == "Oracle" and v.state == "pending" and was != "pending":
                self.add_event("info", "Oracle: update pending")
            elif v.name == "Oracle" and was == "pending" and v.state == "live":
                self.add_event("info", "Oracle: update confirmed")
        self.venues = list(venues)[:MAX_VENUES]

    def set_live(self, live: str, detail: list[str]):
        """live: off | armed | blocked | paused. Only a change of category is an event."""
        if self.live and live != self.live:
            self.add_event("warn" if live == "paused" else "info", f"live: {live}")
        self.live, self.live_detail = live, list(detail)

    def to_json(self) -> dict:
        bank = self.prices.get("bank") or {}
        paths = {}
        for row in self.paths.values():
            c = row.choice
            ok = bool(c and c.ok)
            paths[row.label] = {
                "status": row.status,
                "size_erg": c.size_erg if ok else None,
                "profit_erg": c.profit_erg if ok else None,
                "profit_percent": c.profit_percent if ok else None,
                "break_even_erg": c.break_even_erg if c else None,
                "streak": row.streak,
                "reason": row.detail,
            }
        return {
            "time": datetime.now().isoformat(timespec="seconds"),
            "mode": self.mode,
            "height": self.height,
            "scan": self.scan_count,
            "chain_error": self.chain_error,
            "live": {"state": self.live, "detail": self.live_detail, "trades_today": self.trades_today,
                     "drawdown_erg": self.drawdown},
            "prices": {"pool_sigusd_per_erg": self.prices.get("spectrum_erg_sigusd"),
                       "oracle_usd_per_erg": bank.get("oracle_erg_usd"),
                       "reserve_ratio": bank.get("reserve_ratio"),
                       "can_mint": bank.get("can_mint_sigusd")},
            "paths": paths,
            "venues": [{"name": v.name, "kind": v.kind, "state": v.state, "quote": v.quote} for v in self.venues],
            "wallet": self.wallet,
        }
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_dashboard_state.py -q`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add arbitrage/dashboard_state.py tests/test_dashboard_state.py
git commit -m "Dashboard state: what every view shows, with generated events"
```

---

### Task 2: `arbitrage/venues.py` (venues registry)

**Files:**
- Create: `arbitrage/venues.py`
- Test: `tests/test_venues.py`

**Interfaces:**
- Consumes: `VenueStatus`, `MAX_VENUES` (Task 1). The scanner's prices dict:
  - `spectrum_erg_sigusd: float`
  - `spectrum_pool: PoolState` (`reserve_x`)
  - `bank: {oracle_erg_usd, reserve_ratio, can_mint_sigusd}`
  - `use_lp` (`price_y_in_x`)
  - `kucoin_erg_usdt`, `nonkyc_erg_usdt: float`
  - `cex_watch: {"Kucoin"|"NonKYC": PriceQuote(bid, ask, timestamp)}`
- Produces:
  - `@dataclass VenueContext(prices, timestamps, now, chain_error, pending, read_ms, enable_cex, enable_use, cex_watch)`
  - `VENUES: tuple[Callable[[VenueContext], VenueStatus], ...]`
  - `describe_all(ctx) -> list[VenueStatus]`

Ruling recorded here: the spec's `Venue(name, kind, enabled, describe)` dataclass collapses to one describe function per venue, which returns `state="disabled"` itself. Same behaviour, one place per venue.

- [ ] **Step 1: Write the failing tests** (`tests/test_venues.py`)

```python
"""Venues registry: one row per monitored venue (spec: dashboard)."""
import time
from datetime import datetime, timedelta

from arbitrage.venues import VENUES, VenueContext, describe_all
from exchanges.base import PoolState, PriceQuote

POOL = PoolState(exchange="t", pool_id="p", token_x="ERG", token_y="SigUSD", reserve_x=20_114, reserve_y=6_290,
                 fee_num=995, fee_denom=1000)
PRICES = {"spectrum_erg_sigusd": 0.3127, "spectrum_pool": POOL,
          "bank": {"oracle_erg_usd": 0.3242, "reserve_ratio": 330.1, "can_mint_sigusd": False}}


def ctx(**kw):
    now = time.time()
    base = dict(prices=dict(PRICES), timestamps={"spectrum": now - 1, "bank": now - 1}, now=now, chain_error=None,
                pending=frozenset(), read_ms=6.0, enable_cex=False, enable_use=False, cex_watch=False)
    base.update(kw)
    return VenueContext(**base)


def by_name(rows):
    return {r.name: r for r in rows}


def test_on_chain_rows():
    rows = by_name(describe_all(ctx()))
    pool, bank, oracle = rows["ErgoDEX pool"], rows["SigmaUSD bank"], rows["Oracle"]
    assert pool.state == "live" and "0.3127 SigUSD/ERG" in pool.quote and "20,114 ERG" in pool.quote
    assert pool.age_s is not None and pool.latency_ms == 6.0
    assert bank.quote == "RR 330%, mint ✗" and oracle.quote == "$0.3242/ERG"


def test_pending_oracle_and_chain_outage():
    assert by_name(describe_all(ctx(pending=frozenset({"oracle"}))))["Oracle"].state == "pending"
    rows = by_name(describe_all(ctx(chain_error="node down")))
    assert all(rows[n].state == "down" and rows[n].error == "node down"
               for n in ("ErgoDEX pool", "SigmaUSD bank", "Oracle"))


def test_disabled_venues_stay_listed():
    rows = by_name(describe_all(ctx()))
    assert rows["Dexy USE"].state == "disabled" and rows["Dexy USE"].quote == "ENABLE_USE=false"
    assert rows["Kucoin"].state == "disabled" and rows["NonKYC"].state == "disabled"


def test_cex_watch_quote_and_missing_quote():
    q = PriceQuote(exchange="Kucoin", pair="ERG/USDT", bid=0.3301, ask=0.3305,
                   timestamp=datetime.now() - timedelta(seconds=4))
    rows = by_name(describe_all(ctx(cex_watch=True, prices=dict(PRICES, cex_watch={"Kucoin": q}))))
    assert rows["Kucoin"].state == "watch" and rows["Kucoin"].quote == "$0.3301 / $0.3305"
    assert 3 <= rows["Kucoin"].age_s <= 6
    assert rows["NonKYC"].state == "down" and rows["NonKYC"].error == "no quote"


def test_a_failing_venue_does_not_take_the_others_down(monkeypatch):
    import arbitrage.venues as venues

    def broken(c):
        raise ValueError("boom")

    monkeypatch.setattr(venues, "VENUES", (broken,) + venues.VENUES[1:])
    rows = describe_all(ctx())
    assert rows[0].state == "down" and "boom" in rows[0].error
    assert by_name(rows)["SigmaUSD bank"].state == "live"


def test_registry_fits_the_panel():
    assert len(VENUES) <= 7
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_venues.py -q`
Expected: `ModuleNotFoundError: No module named 'arbitrage.venues'`.

- [ ] **Step 3: Implement `arbitrage/venues.py`**

```python
"""The venues the dashboard lists: one describe function per venue (at most MAX_VENUES).

Adding a venue = one function here + the scanner putting its result into `prices`.
"""
from dataclasses import dataclass
from typing import Callable, Optional

from arbitrage.dashboard_state import MAX_VENUES, VenueStatus


@dataclass
class VenueContext:
    prices: dict
    timestamps: dict          # scanner._price_timestamps: source -> time.time()
    now: float
    chain_error: Optional[str]
    pending: frozenset        # ChainSnapshot.pending
    read_ms: Optional[float]
    enable_cex: bool
    enable_use: bool
    cex_watch: bool


def _age(c: VenueContext, key: str) -> Optional[float]:
    t = c.timestamps.get(key)
    return c.now - t if t else None


def _chain_down(name: str, c: VenueContext) -> Optional[VenueStatus]:
    if c.chain_error:
        return VenueStatus(name, "on-chain", "down", error=c.chain_error)
    return None


def pool(c: VenueContext) -> VenueStatus:
    spot = c.prices.get("spectrum_erg_sigusd")
    down = _chain_down("ErgoDEX pool", c)
    if down or not spot:
        return down or VenueStatus("ErgoDEX pool", "on-chain", "down", error="no data")
    p = c.prices.get("spectrum_pool")
    reserve = f", {p.reserve_x:,.0f} ERG" if p else ""
    return VenueStatus("ErgoDEX pool", "on-chain", "pending" if "pool" in c.pending else "live",
                       f"{spot:.4f} SigUSD/ERG{reserve}", _age(c, "spectrum"), c.read_ms)


def bank(c: VenueContext) -> VenueStatus:
    b = c.prices.get("bank") or {}
    down = _chain_down("SigmaUSD bank", c)
    if down or b.get("reserve_ratio") is None:
        return down or VenueStatus("SigmaUSD bank", "on-chain", "down", error="no data")
    return VenueStatus("SigmaUSD bank", "on-chain", "pending" if "bank" in c.pending else "live",
                       f"RR {b['reserve_ratio']:.0f}%, mint {'✓' if b.get('can_mint_sigusd') else '✗'}",
                       _age(c, "bank"), c.read_ms)


def oracle(c: VenueContext) -> VenueStatus:
    b = c.prices.get("bank") or {}
    down = _chain_down("Oracle", c)
    if down or not b.get("oracle_erg_usd"):
        return down or VenueStatus("Oracle", "on-chain", "down", error="no data")
    return VenueStatus("Oracle", "on-chain", "pending" if "oracle" in c.pending else "live",
                       f"${b['oracle_erg_usd']:.4f}/ERG", _age(c, "bank"), c.read_ms)


def dexy_use(c: VenueContext) -> VenueStatus:
    if not c.enable_use:
        return VenueStatus("Dexy USE", "on-chain", "disabled", "ENABLE_USE=false")
    lp = c.prices.get("use_lp")
    if lp is None:
        return VenueStatus("Dexy USE", "on-chain", "down", error="no data")
    return VenueStatus("Dexy USE", "on-chain", "live", f"{lp.price_y_in_x:.4f} ERG/USE", _age(c, "use"))


def _cex(name: str, price_key: str, ts_key: str) -> Callable[[VenueContext], VenueStatus]:
    def describe(c: VenueContext) -> VenueStatus:
        if not (c.enable_cex or c.cex_watch):
            return VenueStatus(name, "CEX", "disabled", "ENABLE_CEX=false")
        if c.enable_cex:
            price = c.prices.get(price_key)
            if not price:
                return VenueStatus(name, "CEX", "down", error="no quote")
            return VenueStatus(name, "CEX", "live", f"${price:.4f}", _age(c, ts_key))
        q = (c.prices.get("cex_watch") or {}).get(name)
        if q is None:
            return VenueStatus(name, "CEX", "down", error="no quote")
        return VenueStatus(name, "CEX", "watch", f"${q.bid:.4f} / ${q.ask:.4f}", c.now - q.timestamp.timestamp())
    return describe


VENUES: tuple = (pool, bank, oracle, dexy_use, _cex("Kucoin", "kucoin_erg_usdt", "kucoin"),
                 _cex("NonKYC", "nonkyc_erg_usdt", "nonkyc"))


def describe_all(c: VenueContext) -> list[VenueStatus]:
    """One row per venue; a venue whose description fails shows as down instead of breaking the panel."""
    rows = []
    for describe in VENUES[:MAX_VENUES]:
        try:
            rows.append(describe(c))
        except Exception as e:
            name = getattr(describe, "__name__", "venue")
            rows.append(VenueStatus(name, "?", "down", error=f"{e.__class__.__name__}: {e}"))
    return rows
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_venues.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add arbitrage/venues.py tests/test_venues.py
git commit -m "Venues registry for the dashboard (on-chain + CEX, max 7 rows)"
```

---

### Task 3: `arbitrage/dashboard_view.py` (renderer)

**Files:**
- Create: `arbitrage/dashboard_view.py`
- Test: `tests/test_dashboard_view.py`

**Interfaces:**
- Consumes: `DashboardState`, `VenueStatus`, `PathRow` (Task 1).
- Produces:
  - `render(state) -> rich.layout.Layout`
  - `render_safe(state) -> RenderableType` (never raises; records a render error as an event)
  - `sparkline(values) -> str`

- [ ] **Step 1: Write the failing tests** (`tests/test_dashboard_view.py`)

```python
"""Dashboard renderer: pure function of DashboardState (spec: dashboard)."""
from rich.console import Console

from arbitrage.dashboard_state import DashboardState, VenueStatus
from arbitrage.dashboard_view import render, render_safe, sparkline
from tests.test_dashboard_state import MINT, POOL, blocked, go


def text(renderable, width=140, height=50):
    c = Console(record=True, width=width, height=height, color_system=None)
    c.print(renderable)
    return c.export_text()


def full_state():
    s = DashboardState("live")
    s.height, s.scan_count, s.read_ms, s.node_ok, s.wallet_ok, s.next_full_scan_in = 1885700, 4, 6.0, True, True, 9
    s.prices = {"spectrum_erg_sigusd": 0.3127,
                "bank": {"oracle_erg_usd": 0.3242, "reserve_ratio": 330.1, "can_mint_sigusd": False}}
    s.update_paths({POOL: go(3.36), MINT: blocked()}, {POOL: 1}, None,
                   {POOL: ["Swap 40 ERG -> SigUSD on the pool", "Redeem at the bank"]})
    s.update_venues([VenueStatus("ErgoDEX pool", "on-chain", "live", "0.3127 SigUSD/ERG", 1.0, 6.0),
                     VenueStatus("Kucoin", "CEX", "disabled", "ENABLE_CEX=false")])
    s.set_live("blocked", ["profitable 1/2 polls in a row"])
    s.wallet = {"erg": 20.7119, "sigusd": 0.0, "value_erg": 20.7119, "value_usd": 6.72}
    s.add_event("info", "CHAIN pool, bank changed")
    return s


def test_every_panel_and_key_value_is_shown():
    out = text(render(full_state()))
    for needle in ("LIVE", "h1885700", "Prices", "0.3127", "RR 330%", "Live", "blocked", "profitable 1/2 polls",
                   "Wallet", "20.7119 ERG", "Paths", "pool→redeem", "GO", "+3.36%", "1/2", "mint→pool sell",
                   "BLOCKED", "Events", "CHAIN pool, bank changed", "Venues", "ErgoDEX pool", "ENABLE_CEX=false"):
        assert needle in out, needle


def test_steps_of_the_go_path_are_listed():
    out = text(render(full_state()))
    assert "Swap 40 ERG -> SigUSD on the pool" in out and "Redeem at the bank" in out


def test_empty_state_before_the_first_scan():
    out = text(render(DashboardState("monitor")))
    assert "waiting for the first scan" in out


def test_small_terminal_does_not_raise():
    text(render(full_state()), width=80, height=24)


def test_render_error_lands_in_events_and_does_not_raise():
    s = full_state()
    s.prices = None  # a bug somewhere: prices_panel calls .get on it
    out = text(render_safe(s))
    assert "dashboard render error" in out
    assert any("dashboard render error" in t for _, _, t in s.events)


def test_sparkline():
    assert sparkline([]) == ""
    assert sparkline([1.0, 1.0, 1.0]) == "▁▁▁"
    assert sparkline([0.0, 5.0, 10.0]) == "▁▄█"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_dashboard_view.py -q`
Expected: `ModuleNotFoundError: No module named 'arbitrage.dashboard_view'`.

- [ ] **Step 3: Implement `arbitrage/dashboard_view.py`**

```python
"""Render DashboardState as one screen (rich Layout). Pure: reads the state, never changes it
(except render_safe, which records a render error as an event)."""
from datetime import datetime

from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import config
from arbitrage.dashboard_state import DashboardState

SPARK = "▁▂▃▄▅▆▇█"
VENUE_STYLE = {"live": ("●", "green"), "pending": ("◐", "yellow"), "watch": ("○", "cyan"),
               "down": ("●", "red"), "disabled": ("–", "dim")}
STATUS_STYLE = {"GO": "bold green", "no edge": "dim", "BLOCKED": "red", "stale": "yellow", "no data": "dim"}
LIVE_STYLE = {"armed": "bold green", "blocked": "yellow", "paused": "bold red", "off": "dim"}
EVENT_STYLE = {"good": "green", "warn": "yellow", "error": "bold red", "trade": "bold cyan", "info": ""}
EVENTS_SHOWN = 10


def sparkline(values) -> str:
    values = list(values)
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return SPARK[0] * len(values)
    return "".join(SPARK[round((v - lo) / (hi - lo) * (len(SPARK) - 1))] for v in values)


def _age(seconds) -> str:
    if seconds is None:
        return ""
    return f"{seconds:.0f}s" if seconds < 60 else f"{seconds / 60:.0f}m"


def _dot(ok) -> Text:
    return Text("●", style="green" if ok else ("red" if ok is False else "dim"))


def header(s: DashboardState) -> Text:
    mode_style = {"live": "bold white on red", "notify": "bold green", "monitor": "bold"}.get(s.mode, "bold")
    t = Text()
    t.append(" ERGO ARB ", style="bold magenta")
    t.append(f" {s.mode.upper()} ", style=mode_style)
    t.append(f"  h{s.height}" if s.height else "  h-")
    t.append("  node ")
    t.append_text(_dot(s.node_ok))
    t.append(f" {s.read_ms:.0f} ms" if s.read_ms is not None else "")
    t.append("  wallet ")
    t.append_text(_dot(s.wallet_ok))
    t.append(f"  poll {config.CHAIN_POLL_SECONDS:g}s")
    if s.next_full_scan_in is not None:
        t.append(f"  full scan in {s.next_full_scan_in:.0f}s")
    t.append(f"  confirm {config.LIVE_CONFIRM_POLLS}  {datetime.now():%H:%M:%S}")
    return t


def prices_panel(s: DashboardState) -> Panel:
    bank = s.prices.get("bank") or {}
    spot, oracle = s.prices.get("spectrum_erg_sigusd"), bank.get("oracle_erg_usd")
    if not spot and not oracle:
        return Panel(Text("waiting for the first scan…", style="dim"), title="Prices")
    rows = []
    if spot:
        pool = s.prices.get("spectrum_pool")
        reserve = f"  {pool.reserve_x:,.0f} ERG reserve" if pool else ""
        rows.append(f"Pool    {spot:.4f} SigUSD/ERG{reserve}")
    if oracle:
        peg = f"  SigUSD peg {(oracle / spot - 1) * 100:+.1f}%" if spot else ""
        rows.append(f"Oracle  ${oracle:.4f}/ERG{peg}")
    if bank.get("reserve_ratio") is not None:
        mint = "✓" if bank.get("can_mint_sigusd") else "✗ (<400%)"
        rows.append(f"Bank    RR {bank['reserve_ratio']:.0f}%  redeem ✓  mint {mint}")
    return Panel("\n".join(rows), title="Prices")


def live_panel(s: DashboardState) -> Panel:
    if not s.live:
        return Panel(Text("waiting for the first scan…", style="dim"), title="Live")
    lines = Text(s.live, style=LIVE_STYLE.get(s.live, ""))
    for d in s.live_detail[:3]:
        lines.append(f"\n  {d}", style="dim")
    lines.append(f"\ntrades today {s.trades_today}/{config.LIVE_MAX_TRADES_PER_DAY}  "
                 f"drawdown {s.drawdown:.2f}/{config.LIVE_MAX_DRAWDOWN_ERG:g} ERG", style="dim")
    return Panel(lines, title="Live")


def wallet_panel(s: DashboardState) -> Panel:
    w = s.wallet
    if not w:
        return Panel(Text("—", style="dim"), title="Wallet")
    line = f"{w.get('erg', 0):.4f} ERG   {w.get('sigusd', 0):.2f} SigUSD"
    if w.get("value_erg") is not None:
        line += f"   ~{w['value_erg']:.4f} ERG"
    if w.get("value_usd") is not None:
        line += f" / ${w['value_usd']:.2f}"
    return Panel(line, title="Wallet")


def paths_panel(s: DashboardState) -> Panel:
    if not s.paths:
        return Panel(Text("waiting for the first scan…", style="dim"), title="Paths")
    t = Table(expand=True, box=None, pad_edge=False)
    for col, justify in (("path", "left"), ("size", "right"), ("profit", "right"), ("%", "right"),
                         ("b/even", "right"), ("conf", "right"), ("status", "left"), ("last 30", "left")):
        t.add_column(col, justify=justify, no_wrap=True)
    go_steps = []
    for row in s.paths.values():
        c = row.choice
        ok = bool(c and c.ok)
        t.add_row(row.label,
                  f"{c.size_erg:.2f}" if ok else "—",
                  f"{c.profit_erg:+.4f}" if ok else (f"{c.max_profit_erg:+.4f}" if c and c.max_profit_size_erg else "—"),
                  f"{c.profit_percent:+.2f}%" if ok else "—",
                  f"{c.break_even_erg:.2f}" if c and c.break_even_erg else "—",
                  f"{row.streak}/{config.LIVE_CONFIRM_POLLS}" if c else "—",
                  Text(row.status if ok or not row.detail else f"{row.status}: {row.detail}",
                       style=STATUS_STYLE.get(row.status, "")),
                  sparkline(row.history))
        if ok and row.steps and not go_steps:
            go_steps = [f"  {i}. {step}" for i, step in enumerate(row.steps, 1)]
    body = Group(t, Text("\n".join(go_steps), style="green")) if go_steps else t
    return Panel(body, title="Paths")


def events_panel(s: DashboardState) -> Panel:
    events = list(s.events)[-EVENTS_SHOWN:]
    if not events:
        return Panel(Text("no events yet", style="dim"), title="Events")
    t = Text()
    for i, (when, level, text) in enumerate(reversed(events)):
        if i:
            t.append("\n")
        t.append(f"{when:%H:%M:%S} ", style="dim")
        t.append(text, style=EVENT_STYLE.get(level, ""))
    return Panel(t, title="Events")


def venues_panel(s: DashboardState) -> Panel:
    if not s.venues:
        return Panel(Text("waiting for the first scan…", style="dim"), title="Venues")
    t = Table(expand=True, box=None, pad_edge=False)
    for col, justify in (("venue", "left"), ("kind", "left"), ("status", "left"), ("quote", "left"),
                         ("age", "right"), ("ms", "right")):
        t.add_column(col, justify=justify, no_wrap=True)
    for v in s.venues:
        symbol, style = VENUE_STYLE.get(v.state, ("?", ""))
        label = "update pending" if v.state == "pending" and v.name == "Oracle" else v.state.replace("watch", "watch only")
        status = Text(f"{symbol} {label}", style=style)
        quote = v.quote if v.state != "down" else (v.error or "no data")
        t.add_row(v.name, v.kind, status, quote, _age(v.age_s),
                  f"{v.latency_ms:.0f}" if v.latency_ms is not None else "",
                  style="dim" if v.state == "disabled" else None)
    return Panel(t, title="Venues")


def render(s: DashboardState) -> Layout:
    layout = Layout()
    paths_rows = len(s.paths or {}) + 4 + max((len(r.steps) for r in (s.paths or {}).values()
                                               if r.choice and r.choice.ok), default=0)
    layout.split_column(
        Layout(header(s), size=1),
        Layout(name="top", size=7),
        Layout(paths_panel(s), size=paths_rows),
        Layout(events_panel(s), minimum_size=4),
        Layout(venues_panel(s), size=len(s.venues) + 3 if s.venues else 3),
    )
    right = Layout()
    right.split_column(Layout(live_panel(s), ratio=3), Layout(wallet_panel(s), size=3))
    layout["top"].split_row(Layout(prices_panel(s)), right)
    return layout


def render_safe(s: DashboardState):
    """Never raises: a renderer bug shows as an event instead of killing the dashboard (or the bot)."""
    try:
        return render(s)
    except Exception as e:
        try:
            s.add_event("error", f"dashboard render error: {e.__class__.__name__}: {e}")
            return Group(Text(f"dashboard render error: {e}", style="bold red"), events_panel(s))
        except Exception:
            return Text(f"dashboard render error: {e}", style="bold red")
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_dashboard_view.py -q`
Expected: 6 passed.

- [ ] **Step 5: Visual check**

Run (prints one frame of `full_state()` at 140 columns):
```bash
python -c "from rich.console import Console; from tests.test_dashboard_view import full_state; from arbitrage.dashboard_view import render; Console(width=140, height=40).print(render(full_state()))"
```
Expected: all panels visible and aligned, with nothing cut off at 140×40. Adjust panel sizes if a panel is cropped.

- [ ] **Step 6: Commit**

```bash
git add arbitrage/dashboard_view.py tests/test_dashboard_view.py
git commit -m "Dashboard renderer: header, prices, live, wallet, paths, events, venues"
```

---

### Task 4: Scanner fills the state; views (plain, dashboard, json)

**Files:**
- Modify: `arbitrage/scanner.py` (constructor around line 54; every `console.` use; `_note_change`; `_notify_discord`; `_global_blockers` around line 1114; `_execute_trades`; `scan_once`; `poll_once`; `run`)
- Test: `tests/test_scanner_views.py`

**Interfaces:**
- Consumes: `DashboardState`, `PATH_LABELS` (Task 1); `VenueContext`, `describe_all` (Task 2).
- Produces:
  - `ArbitrageScanner(mode="monitor", db_path=..., ..., view="plain", show_wallet=True)`
  - `self.view`, `self.out` (rich Console; quiet unless plain), `self.state: DashboardState`
  - `async run(once: bool = False)`
  - `_refresh_state(now: float)`

- [ ] **Step 1: Write the failing tests** (`tests/test_scanner_views.py`)

```python
"""Scanner views: plain prints as before, dashboard/json print nothing but fill the state (spec: dashboard)."""
import asyncio
import json

import pytest

import arbitrage.scanner as scanner_module
import config
from arbitrage.scanner import ArbitrageScanner
from tests.test_chain_scanner import HEALTHY, KEY, WALLET, Reader, snap


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 1000.0)
    monkeypatch.setattr(config, "LIVE_STOP_FILE", str(tmp_path / "STOP"))
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    made = []

    def factory(view, mode="monitor"):
        s = ArbitrageScanner(mode=mode, db_path=str(tmp_path / f"{view}.db"), view=view)

        async def healthy():
            return dict(HEALTHY)

        async def wallet():
            return dict(WALLET)

        monkeypatch.setattr(s.ergo_node, "get_health", healthy)
        monkeypatch.setattr(s, "_fetch_wallet_balances", wallet)
        made.append(s)
        return s

    yield factory
    for s in made:
        s.tracker.close()


def test_plain_view_prints_the_scan(make, capsys):
    from logging_config import console
    s = make("plain")
    with console.capture() as cap:
        asyncio.run(s.poll_once(0))
    assert "Current Prices" in cap.get()


@pytest.mark.parametrize("view", ["dashboard", "json"])
def test_other_views_print_nothing_while_scanning(make, view):
    from logging_config import console
    s = make(view)
    with console.capture() as cap:
        asyncio.run(s.poll_once(0))
        asyncio.run(s.poll_once(2))
    assert cap.get() == ""


def test_state_is_filled_after_a_full_scan_and_a_fast_poll(make):
    s = make("dashboard", mode="live")
    asyncio.run(s.poll_once(0))
    st = s.state
    assert st.height == 1_885_700 and st.scan_count == 1 and st.node_ok is True
    assert st.paths[KEY].status == "GO" and st.paths[KEY].steps
    assert {v.name for v in st.venues} >= {"ErgoDEX pool", "SigmaUSD bank", "Oracle"}
    assert st.live in ("blocked", "armed") and st.wallet["erg"] == WALLET["erg"]
    assert any(t.startswith("opportunity opened") for _, _, t in st.events)
    asyncio.run(s.poll_once(2))
    assert len(st.paths[KEY].history) == 2 and st.next_full_scan_in == pytest.approx(13, abs=0.5)


def test_monitor_mode_live_panel_is_off(make):
    s = make("dashboard", mode="monitor")
    asyncio.run(s.poll_once(0))
    assert s.state.live == "off"


def test_chain_outage_shows_stale_paths_and_down_venues(make, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), RuntimeError("node down")))
    s = make("dashboard", mode="live")
    asyncio.run(s.poll_once(0))
    asyncio.run(s.poll_once(2))
    assert s.state.paths[KEY].status == "stale" and s.state.chain_error == "node down"
    assert {v.name: v.state for v in s.state.venues}["ErgoDEX pool"] == "down"
    assert s.state.live == "blocked" and any("chain state unavailable" in d for d in s.state.live_detail)


def test_json_view_writes_one_clean_line_per_full_scan(make, capsys):
    s = make("json")
    for t in (0, 2, 4, 15):
        asyncio.run(s.poll_once(t))
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 2
    assert all(json.loads(l)["paths"]["pool→redeem"]["status"] == "GO" for l in lines)


def test_trade_progress_goes_to_events(make, monkeypatch):
    from ergo.arb_runner import ArbResult
    s = make("dashboard", mode="live")

    async def fake_run(ns, path, erg_in, *, log, **kw):
        log("  Leg 1 submitted: https://explorer/tx/t1")
        return ArbResult("executed", erg_in=5 * 10**9, sigusd_cents=100, profit_nanoerg=10**8,
                         profit_percent=2.0, tx1="t1", tx2="t2", path=path)

    monkeypatch.setattr(scanner_module, "run_arb", fake_run)
    asyncio.run(s.poll_once(0))
    asyncio.run(s.poll_once(2))
    kinds = [(lvl, t) for _, lvl, t in s.state.events]
    assert ("trade", "Leg 1 submitted: https://explorer/tx/t1") in kinds
    assert any(lvl == "good" and "executed" in t for lvl, t in kinds)


def test_once_stops_after_the_first_full_scan(make, monkeypatch):
    s = make("json")
    calls = []
    real = s.poll_once

    async def counting(now):
        calls.append(now)
        await real(now)

    async def no_connect():
        return None

    monkeypatch.setattr(s, "poll_once", counting)
    monkeypatch.setattr(s, "connect_all", no_connect)
    monkeypatch.setattr(s, "disconnect_all", no_connect)
    asyncio.run(s.run(once=True))
    assert len(calls) == 1
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_scanner_views.py -q`
Expected: `TypeError: ArbitrageScanner.__init__() got an unexpected keyword argument 'view'`.

- [ ] **Step 3: Implement in `arbitrage/scanner.py`**

1. **Imports:** add `import json`, `import sys`, and:
```python
from rich.console import Console
from arbitrage.dashboard_state import PATH_LABELS, DashboardState
from arbitrage.venues import VenueContext, describe_all
```

2. **Constructor:** add `view: str = "plain", show_wallet: bool = True` to the signature (after `cex_watch`), and in the body:
```python
        self.view = view                    # plain | dashboard | json
        self.show_wallet = show_wallet
        self.out = console if view == "plain" else Console(quiet=True)  # all scan printing goes here
        self.state = DashboardState(mode)
        self._last_wallet: Optional[dict] = None
        self._now = 0.0
```

3. **Printing:** inside `ArbitrageScanner` methods, replace every `console.print(` with `self.out.print(` and `console.rule(` with `self.out.rule(` (34 sites; check with `grep -n "console\." arbitrage/scanner.py`, which must then show only the import). `self.out` is `console` in plain mode, so existing tests that use `console.capture()` keep working.

4. **`_note_change`:** after building the line, also record it. Insert before `self._last_key = snap.key`:
```python
        self.state.add_event("info", f"CHAIN h{snap.height} changed: {', '.join(changed)}{pending}")
```

5. **`_notify_discord`:** where `sent = await self.discord.notify_opportunities(` returns a truthy result, add after it:
```python
            if sent:
                self.state.add_event("info", f"Discord alert sent ({len(to_notify)} path(s))")
```
(Use the actual name of the list passed to `notify_opportunities` in that method.)

6. **Split the global blockers** into a sync part (usable for the dashboard every poll) and the async node-health part:
```python
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
        if self._live_start_value is None:
            self._live_start_value = self._wallet_value_erg(wallet, prices)
        blockers = self._sync_global_blockers(wallet, prices)
        health = await self.ergo_node.get_health()
        if not health.get("ok_to_trade"):
            blockers.append(f"node not ready (synced={health.get('synced')}, unlocked={health.get('unlocked')})")
        return blockers
```
(This replaces the existing `_global_blockers` body. Behaviour is unchanged.)

7. **`_execute_trades`:** the runner log goes to the right place:
```python
    def _trade_log(self, message: str):
        """Runner progress: printed in plain view, an event otherwise, always in the log file."""
        self.out.print(message)
        text = Text.from_markup(message).plain.strip() if "[" in message else message.strip()
        if text:
            self.state.add_event("trade", text)
            logger.info(f"LIVE {text}")
```
- Pass `log=self._trade_log` to `run_arb` instead of `log=lambda m: console.print(m)`.
- At the end, after `logger.warning(f"LIVE {msg}")`, add:
```python
        self.state.add_event("good" if result.status == "executed" else "warn", f"LIVE {msg}")
```

8. **Wallet memory:** in `scan_once`, right after `wallet = await self._fetch_wallet_balances()`, add `self._last_wallet = wallet`. Wrap `self._display_wallet_opportunities(...)` in `if self.show_wallet:`. In `poll_once`'s fast-path trading branch, fetch once and keep it:
```python
            wallet = await self._fetch_wallet_balances()
            self._last_wallet = wallet
            await self._execute_trades(wallet, prices, quiet=True)
```

9. **`_refresh_state`** (new; called at the end of every `poll_once`):
```python
    def _refresh_state(self, now: float):
        s = self.state
        snap = self._snapshot
        prices = self._chain_prices or {}
        s.scan_count, s.chain_error = self.scan_count, self._chain_error
        s.node_ok = self._chain_error is None and snap is not None
        s.height = snap.height if snap else s.height
        s.read_ms = snap.read_ms if snap else None
        s.next_full_scan_in = max(0.0, config.SCAN_INTERVAL_SECONDS - (now - self._last_full_scan))
        s.prices = prices
        s.update_venues(describe_all(VenueContext(
            prices={**prices, **{k: v for k, v in self._last_prices.items() if k not in prices}},
            timestamps=self._price_timestamps, now=time.time(), chain_error=self._chain_error,
            pending=snap.pending if snap else frozenset(), read_ms=s.read_ms,
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
        if self._live_start_value is not None and wallet is not None and prices:
            s.drawdown = max(0.0, self._live_start_value - self._wallet_value_erg(wallet, prices))
        if not self.trading_enabled:
            s.set_live("off", [f"{self.mode} mode: no trading"])
        elif self._live_paused:
            s.set_live("paused", [self._live_paused, "restart to resume"])
        else:
            reasons = self._sync_global_blockers(wallet, prices)
            ready = [k for k in LIVE_PATHS if not self._path_blockers(k, wallet or {})]
            if not ready:
                best = max(LIVE_PATHS, key=lambda k: self._live_streak.get(k, 0))
                reasons = self._path_blockers(best, wallet or {}) + reasons
            s.set_live("blocked" if reasons else "armed", reasons)
```
Keep `self._last_prices` = the full prices dict from the last `fetch_all_prices` (CEX and USE entries): set `self._last_prices = results` at the end of `fetch_all_prices` and initialise `self._last_prices: dict = {}` in the constructor.

10. **`poll_once`:** rename the existing body to `async def _poll(self, now)`, then add a new
    `poll_once` that refreshes the state exactly once per tick (also when `_poll` raises) and, in json
    view, writes the line after a full scan:
```python
    async def poll_once(self, now: float):
        """One tick: full scan when due, otherwise a fast node read, exact sizing and the live gate."""
        self._now = now
        full = now - self._last_full_scan >= config.SCAN_INTERVAL_SECONDS
        try:
            await self._poll(now)
        finally:
            self._refresh_state(now)
        if full and self.view == "json":
            sys.stdout.write(json.dumps(self.state.to_json(), default=str) + "
")
            sys.stdout.flush()
```

11. **`run(once=False)`:**
- Change the signature to `async def run(self, once: bool = False):`.
- Print the startup panel via `self.out.print(...)`.
- Make the loop's first statement after `await self.poll_once(loop.time())` this:
```python
                if once:
                    break
```
- Make the "Shutting down…" print go through `self.out`.
- Gate `self.tracker.print_summary()` with `if self.view == "plain":`.

- [ ] **Step 4: Run to verify**

Run: `python -m pytest tests/test_scanner_views.py -q`, then `python -m pytest -q`.
Expected: 9 passed; the full suite all-pass (existing tests use the default `view="plain"`).

- [ ] **Step 5: Live check**

Run `timeout 40 python -c "import asyncio; from dotenv import load_dotenv; load_dotenv(); from arbitrage.scanner import ArbitrageScanner; s=ArbitrageScanner(view='json'); asyncio.run(s.run(once=True))"`.
Expected: exactly one JSON line with the current height, venues, and pool→redeem status.

- [ ] **Step 6: Commit**

```bash
git add arbitrage/scanner.py tests/test_scanner_views.py
git commit -m "Scanner fills DashboardState; plain/dashboard/json views; --once support"
```

---

### Task 5: Logging and CLI (`main.py`, `logging_config.py`)

**Files:**
- Modify: `logging_config.py` (`setup_logging`)
- Modify: `main.py` (rewrite `main`)
- Test: `tests/test_main_cli.py`

**Interfaces:**
- Consumes: `ArbitrageScanner(view=..., show_wallet=..., db_path=...)`, `scanner.run(once=...)`, `scanner.state` (Task 4); `render_safe` (Task 3).
- Produces:
  - `logging_config.setup_logging(log_level="INFO", console_handler=True, log_path="arbitrage.log") -> logging.Logger`
  - `logging_config.EventLogHandler(state)`
  - `main.build_parser()`, `main.view_of(args) -> str`, `main.apply_overrides(args)`

First check who else calls `setup_logging`: `grep -rn "setup_logging" --include=*.py .`. Keep the first positional parameter `log_level` so those callers still work.

- [ ] **Step 1: Write the failing tests** (`tests/test_main_cli.py`)

```python
"""main.py flags and logging setup (spec: dashboard)."""
import logging
import logging.handlers

import pytest

import config
from main import apply_overrides, build_parser, view_of


def test_default_view_is_the_dashboard():
    assert view_of(build_parser().parse_args([])) == "dashboard"
    assert view_of(build_parser().parse_args(["--plain"])) == "plain"
    assert view_of(build_parser().parse_args(["--json", "--live"])) == "json"


def test_plain_and_json_are_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--plain", "--json"])


def test_flags_parse():
    a = build_parser().parse_args(["--once", "--interval", "30", "--max-trade-erg", "5", "--log-level", "debug",
                                   "--db", "x.db", "--no-wallet"])
    assert a.once and a.interval == 30 and a.max_trade_erg == 5.0 and a.log_level == "debug"
    assert a.db == "x.db" and a.no_wallet


def test_overrides_apply_to_config():
    apply_overrides(build_parser().parse_args(["--interval", "30", "--max-trade-erg", "5"]))
    assert config.SCAN_INTERVAL_SECONDS == 30 and config.MAX_TRADE_SIZE_ERG == 5.0


def test_no_overrides_leave_config_alone():
    before = (config.SCAN_INTERVAL_SECONDS, config.MAX_TRADE_SIZE_ERG)
    apply_overrides(build_parser().parse_args([]))
    assert (config.SCAN_INTERVAL_SECONDS, config.MAX_TRADE_SIZE_ERG) == before


def test_log_file_rotates_daily_and_keeps_two_weeks(tmp_path):
    from logging_config import setup_logging
    logger = setup_logging("info", console_handler=False, log_path=str(tmp_path / "arbitrage.log"))
    files = [h for h in logger.handlers if isinstance(h, logging.handlers.TimedRotatingFileHandler)]
    assert len(files) == 1 and files[0].backupCount == 14 and files[0].when == "MIDNIGHT"
    assert files[0].level == logging.INFO
    assert not any(type(h).__name__ == "RichHandler" for h in logger.handlers)
    setup_logging("info", console_handler=False, log_path=str(tmp_path / "arbitrage.log"))
    assert len(logging.getLogger("ergo_arb").handlers) == 1  # no duplicates on a second call
    for h in list(logger.handlers):
        h.close()
        logger.removeHandler(h)


def test_event_handler_puts_warnings_in_the_dashboard():
    from arbitrage.dashboard_state import DashboardState
    from logging_config import EventLogHandler
    state = DashboardState()
    logger = logging.getLogger("ergo_arb.test_events")
    handler = EventLogHandler(state)
    logger.addHandler(handler)
    try:
        logger.info("quiet")
        logger.warning("Chain state unavailable: timeout")
    finally:
        logger.removeHandler(handler)
    assert [(lvl, t) for _, lvl, t in state.events] == [("warn", "Chain state unavailable: timeout")]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_main_cli.py -q`
Expected: `ImportError: cannot import name 'apply_overrides' from 'main'`.

- [ ] **Step 3: Implement**

`logging_config.py`: replace `setup_logging` with:

```python
import logging.handlers


class EventLogHandler(logging.Handler):
    """WARNING+ log records as dashboard events (the dashboard replaces the console log)."""

    def __init__(self, state):
        super().__init__(level=logging.WARNING)
        self.state = state

    def emit(self, record):
        try:
            level = "error" if record.levelno >= logging.ERROR else "warn"
            self.state.add_event(level, record.getMessage())
        except Exception:
            self.handleError(record)


def setup_logging(log_level: str = "INFO", console_handler: bool = True,
                  log_path: str = "arbitrage.log") -> logging.Logger:
    """File log rotated at midnight (14 days kept) + optional rich console log. Safe to call twice."""
    root_logger = logging.getLogger("ergo_arb")
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
        h.close()
    root_logger.setLevel(logging.DEBUG)

    file_handler = logging.handlers.TimedRotatingFileHandler(log_path, when="midnight", backupCount=14,
                                                             encoding="utf-8")
    file_handler.setLevel(getattr(logging, log_level.upper()))
    file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"))
    root_logger.addHandler(file_handler)

    if console_handler:
        rich_handler = RichHandler(console=console, show_time=True, show_path=False, markup=True,
                                   rich_tracebacks=True)
        rich_handler.setLevel(getattr(logging, log_level.upper()))
        root_logger.addHandler(rich_handler)
    return root_logger
```

(Remove the now-unused `from datetime import datetime` if nothing else in the file uses it.)

`main.py`: replace the file body with:

```python
import argparse
import asyncio
import sys

from rich.live import Live

import config
from arbitrage.dashboard_view import render_safe
from arbitrage.scanner import ArbitrageScanner
from logging_config import EventLogHandler, console, setup_logging


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Ergo Arbitrage Monitor (live dashboard by default)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--notify", action="store_true", help="monitor + Discord alerts (no trades)")
    mode.add_argument("--live", action="store_true", help="monitor + Discord alerts + auto-execute trades")
    view = p.add_mutually_exclusive_group()
    view.add_argument("--plain", action="store_true", help="scrolling output instead of the dashboard")
    view.add_argument("--json", action="store_true", help="one JSON line per full scan on stdout")
    p.add_argument("--once", action="store_true", help="one full scan, then exit")
    p.add_argument("--interval", type=int, help="seconds between full scans (SCAN_INTERVAL_SECONDS)")
    p.add_argument("--max-trade-erg", type=float, help="cap on any executed trade (MAX_TRADE_SIZE_ERG)")
    p.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"],
                   help="log file level (arbitrage.log, rotated daily, 14 days kept)")
    p.add_argument("--db", default="arbitrage_tracker.db", help="tracker database path")
    p.add_argument("--no-wallet", action="store_true", help="hide the wallet panel and wallet analysis")
    return p


def view_of(args) -> str:
    return "plain" if args.plain else ("json" if args.json else "dashboard")


def mode_of(args) -> str:
    return "live" if args.live else ("notify" if args.notify else "monitor")


def apply_overrides(args):
    if args.interval:
        config.SCAN_INTERVAL_SECONDS = args.interval
    if args.max_trade_erg:
        config.MAX_TRADE_SIZE_ERG = args.max_trade_erg


def print_banner(mode: str):
    labels = {"monitor": "[dim]MONITOR ONLY[/dim]", "notify": "[bold green]NOTIFICATION MODE[/bold green]",
              "live": "[bold red]LIVE TRADING MODE[/bold red]"}
    console.print("[bold magenta]ERGO ARBITRAGE MONITOR[/bold magenta]  " + labels[mode])
    if mode == "live":
        console.print("[bold red]WARNING: live trading executes real transactions.[/bold red]")


async def run(scanner: ArbitrageScanner, view: str, once: bool):
    if view != "dashboard":
        await scanner.run(once=once)
        return
    with Live(get_renderable=lambda: render_safe(scanner.state), console=console, refresh_per_second=2,
              screen=not once, redirect_stdout=False, redirect_stderr=False):
        await scanner.run(once=once)


def main(argv=None):
    args = build_parser().parse_args(argv)
    apply_overrides(args)
    view, mode = view_of(args), mode_of(args)
    logger = setup_logging(args.log_level, console_handler=view == "plain")
    if view == "plain":
        print_banner(mode)
    scanner = ArbitrageScanner(mode=mode, db_path=args.db, view=view, show_wallet=not args.no_wallet)
    if view == "dashboard":
        logger.addHandler(EventLogHandler(scanner.state))
    try:
        asyncio.run(run(scanner, view, args.once))
    except KeyboardInterrupt:
        if view != "json":
            console.print("[bold yellow]Interrupted. Goodbye![/bold yellow]")
        sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify**

Run: `python -m pytest tests/test_main_cli.py -q`, then `python -m pytest -q`.
Expected: 7 passed; full suite all-pass.

- [ ] **Step 5: Live checks (read-only)**

- `python main.py --once --plain`: today's scan output, then exit.
- `python main.py --once --json`: exactly one JSON line on stdout.
- `python main.py --once`: one dashboard frame, then exit.
- `timeout 120 python main.py`: the live dashboard for 2 minutes. The header clock and countdown tick, there are no tracebacks, and Venues lists 6 rows.
- `ls arbitrage.log`: it exists and contains `Scan #` lines.

- [ ] **Step 6: Commit**

```bash
git add main.py logging_config.py tests/test_main_cli.py
git commit -m "main.py: dashboard by default, --plain/--json/--once and overrides; rotating log file"
```

---

### Task 6: Docs, whole-branch verification, PR

**Files:**
- Modify: `README.md`, `FLOWS.md`

- [ ] **Step 1: README**
  - `cd ergo_arbitrage` becomes `cd ergo-arbitrage`.
  - In the usage section, add the dashboard (default), `--plain`, `--json`, `--once`, `--interval`, `--max-trade-erg`, `--log-level`, `--db` and `--no-wallet`, with a text frame of the dashboard (the spec's layout block).
  - Mention `arbitrage.log` (rotated daily, 14 days).

- [ ] **Step 2: Fees (README and FLOWS)**
  - SigUSD pool swaps are direct (miner fee only) unless `POOL_SWAP_ROUTE=crux`.
  - Update the examples and fee rows that still say "~0.785 ERG Crux service" for SigUSD pool swaps (README lines ~53, ~67; FLOWS lines ~24, ~32, ~62, ~73, ~135) to say "direct pool swap: miner fee only (0.785 ERG Crux service only with POOL_SWAP_ROUTE=crux)".
  - Leave the USE/Crux LP rows alone; they really go through Crux.

- [ ] **Step 3:** Run `python -m pytest -q` and `python -m pytest -m live -q`. Both must pass.

- [ ] **Step 4:** Commit:
```bash
git add README.md FLOWS.md
git commit -m "Docs: dashboard usage and flags; direct pool swaps pay only the miner fee"
```

- [ ] **Step 5:** Check the PR state (the user merges fast), push `feature/dashboard`, and open a PR that closes #14.
