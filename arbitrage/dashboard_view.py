"""Render DashboardState as one screen (rich Layout). Pure: reads the state, never changes it
(except render_safe, which records a render error as an event)."""
from datetime import datetime
from typing import Optional

from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import config
from arbitrage.dashboard_state import DashboardState
from notifications.embeds import duration

SPARK = "▁▂▃▄▅▆▇█"
VENUE_STYLE = {"live": ("●", "green"), "pending": ("◐", "yellow"), "watch": ("○", "cyan"),
               "down": ("●", "red"), "disabled": ("–", "dim")}
STATUS_STYLE = {"GO": "bold green", "no edge": "dim", "BLOCKED": "red", "stale": "yellow", "no data": "dim"}
LIVE_STYLE = {"armed": "bold green", "blocked": "yellow", "paused": "bold red", "off": "dim"}
EVENT_STYLE = {"good": "green", "warn": "yellow", "error": "bold red", "trade": "bold cyan", "info": ""}
EVENTS_SHOWN = 10
LIVE_REASONS_SHOWN = 5
HISTORY_SHOWN = 5
RR_TREND_HOURS = 6
RR_TREND_WIDTH = 24
EXCHANGE_STYLE = {"live": "", "down": "red", "disabled": "dim"}


def sparkline(values) -> str:
    values = list(values)
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return SPARK[0] * len(values)
    return "".join(SPARK[int((v - lo) / (hi - lo) * (len(SPARK) - 1))] for v in values)


def _level(value, warn, bad) -> str:
    """Style for a number that is fine below `warn`, yellow below `bad`, red at or above it."""
    if value is None:
        return "dim"
    return "red" if value >= bad else ("yellow" if value >= warn else "green")


def health_strip(s: DashboardState) -> Text:
    """One line: how long each source took and whether anything is backed up or stale."""
    h = s.health or {}
    t = Text(" ")
    parts = []
    if s.read_ms is not None:
        parts.append((f"chain {s.read_ms:.0f} ms", _level(s.read_ms, 500, 2000)))
    cex = h.get("cex_ms") or {}
    if cex:
        parts.append((" · ".join(f"{n} {ms:.0f}" for n, ms in cex.items()) + " ms",
                      _level(max(cex.values()), 1000, 3000)))
    if h.get("full_scan_ms") is not None:
        parts.append((f"full scan {h['full_scan_ms']:.0f} ms", _level(h["full_scan_ms"], 2000, 8000)))
    if h.get("discord_queue") is not None:
        parts.append((f"Discord queue {h['discord_queue']}", _level(h["discord_queue"], 10, 50)))
    if h.get("data_age_s") is not None:
        parts.append((f"data {h['data_age_s']:.0f}s", _level(h["data_age_s"], 30, 60)))
    if not parts:
        t.append("waiting for the first scan…", style="dim")
    for i, (label, style) in enumerate(parts):
        if i:
            t.append("  ·  ", style="dim")
        t.append(label, style=style)
    return t


def rr_trend(samples, now: float, hours: float = RR_TREND_HOURS, width: int = RR_TREND_WIDTH) -> str:
    """Reserve-ratio sparkline over the last `hours` (bucket averages), e.g. "▁▂▃▅ (6h, 322→330%)"."""
    recent = [v for t, v in samples if now - t <= hours * 3600]
    if len(recent) < 2:
        return ""
    n = min(width, len(recent))
    buckets = [recent[i * len(recent) // n:(i + 1) * len(recent) // n] for i in range(n)]
    means = [sum(b) / len(b) for b in buckets if b]
    return f"{sparkline(means)} ({hours:g}h, {means[0]:.0f}→{means[-1]:.0f}%)"


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
    if s.discord_on:
        t.append("  discord ")
        t.append_text(_dot(True))
    else:
        t.append("  discord off", style="dim")
    t.append(f"  poll {config.CHAIN_POLL_SECONDS:g}s")
    if s.next_full_scan_in is not None:
        t.append(f"  full scan in {s.next_full_scan_in:.0f}s")
    t.append(f"  confirm {config.LIVE_CONFIRM_POLLS}  {datetime.now():%H:%M:%S}")
    t.append("  [u] SigUSD view" if s.view_mode == "erg" else "  [e] ERG view", style="dim")
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
        mint = s.mint_text or ("✓" if bank.get("can_mint_sigusd") else "✗ (<400%)")
        rows.append(f"Bank    RR {bank['reserve_ratio']:.0f}%  redeem ✓  mint {mint}")
        trend = rr_trend(s.rr_history, datetime.now().timestamp())
        if trend:
            rows.append(f"        RR trend {trend}")
    return Panel("\n".join(rows), title="Prices")


def live_panel(s: DashboardState) -> Panel:
    if not s.live:
        return Panel(Text("waiting for the first scan…", style="dim"), title="Live")
    lines = Text(s.live, style=LIVE_STYLE.get(s.live, ""), no_wrap=True, overflow="ellipsis")  # one line per reason
    lines.append(f"   trades today {s.trades_today}/{config.LIVE_MAX_TRADES_PER_DAY}  "
                 f"drawdown {s.drawdown:.2f}/{config.LIVE_MAX_DRAWDOWN_ERG:g} ERG", style="dim")
    shown = s.live_detail[:LIVE_REASONS_SHOWN]
    for d in shown:
        lines.append(f"\n  {d}", style="dim")
    if len(s.live_detail) > len(shown):
        lines.append(f"\n  +{len(s.live_detail) - len(shown)} more", style="dim")
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


COMPACT_WIDTH = 100    # narrower: fewer columns, the path status wraps, venues and exchanges in one table
COMPACT_HEIGHT = 32    # shorter: no Events/History and no GO steps, so every path and venue still fits
PATH_COLUMNS = (("path", "left", 14), ("size", "right", 7), ("profit", "right", 8), ("%", "right", 7),
                ("b/even", "right", 6), ("conf", "right", 4), ("status", "left", 8), ("last 30", "left", 8))
COMPACT_DROPS = {"b/even", "last 30"}


def _status_text(row) -> str:
    ok = bool(row.choice and row.choice.ok)
    return row.status if ok or not row.detail else f"{row.status}: {row.detail}"


def paths_panel(s: DashboardState, compact: bool = False, steps: bool = True) -> Panel:
    if not s.paths:
        return Panel(Text("waiting for the first scan…", style="dim"), title="Paths")
    t = Table(expand=True, box=None, pad_edge=False)
    # Fixed minimums keep the numbers whole on narrow terminals; only the status reason shrinks (or, compact,
    # wraps so the reason stays readable).
    columns = [c for c in PATH_COLUMNS if not (compact and c[0] in COMPACT_DROPS)]
    for col, justify, width in columns:
        t.add_column(col, justify=justify, no_wrap=col != "status", overflow="fold" if col == "status" else "ellipsis",
                     min_width=width, ratio=1 if col == "status" else None)
    go_steps = []
    for row in s.paths.values():
        c = row.choice
        ok = bool(c and c.ok)
        cells = {"path": row.label,
                 "size": f"{c.size_erg:.2f}" if ok else "—",
                 "profit": f"{c.profit_erg:+.4f}" if ok else (f"{c.max_profit_erg:+.4f}" if c and c.max_profit_size_erg
                                                             else "—"),
                 "%": f"{c.profit_percent:+.2f}%" if ok else "—",
                 "b/even": f"{c.break_even_erg:.2f}" if c and c.break_even_erg else "—",
                 "conf": f"{row.streak}/{config.LIVE_CONFIRM_POLLS}" if c else "—",
                 "status": Text(_status_text(row), style=STATUS_STYLE.get(row.status, "")),
                 "last 30": sparkline(row.history)}
        t.add_row(*[cells[col] for col, _, _ in columns])
        if steps and ok and row.steps and not go_steps:
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


def _hhmm(iso) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except (TypeError, ValueError):
        return "--:--"


def _lasted(row) -> str:
    try:
        return duration((datetime.fromisoformat(row["closed_at"])
                         - datetime.fromisoformat(row["opened_at"])).total_seconds())
    except (KeyError, TypeError, ValueError):
        return "open"


def history_panel(s: DashboardState) -> Panel:
    """The last opportunity episodes and live trades, from the tracker database."""
    if not (s.recent_episodes or s.recent_trades):
        return Panel(Text("no episodes or trades yet", style="dim"), title="History")
    t = Text()
    if s.recent_episodes:
        t.append("Episodes", style="bold")
        for row in s.recent_episodes[:HISTORY_SHOWN]:
            t.append(f"\n {_hhmm(row.get('opened_at'))} ", style="dim")
            t.append(f"{row.get('path', '?')}  {_lasted(row)}  peak {row.get('peak_profit_percent') or 0:+.2f}%")
    if s.recent_trades:
        if s.recent_episodes:
            t.append("\n")
        t.append("Trades", style="bold")
        for row in s.recent_trades[:HISTORY_SHOWN]:
            profit = row.get("actual_profit_erg")
            if profit is None:
                profit = row.get("expected_profit_erg")
            status = row.get("status", "?")
            style = "green" if status == "completed" else ("red" if status == "failed" else "")
            t.append(f"\n {_hhmm(row.get('started_at'))} ", style="dim")
            t.append(f"{status}", style=style)
            path = (row.get("notes") or "").split(";")[0]
            t.append(f"  {path}  {row.get('input_erg') or 0:g} ERG"
                     + (f"  {profit:+.4f} ERG" if profit is not None else ""))
    return Panel(t, title="History")


def exchanges_panel(s: DashboardState) -> Panel:
    """Watch-only CEX books: bid/ask, gap to the oracle, fees (live or default), response time."""
    t = Table(expand=True, box=None, pad_edge=False)
    for col, justify in (("exchange", "left"), ("bid", "right"), ("ask", "right"), ("vs oracle", "right"),
                         ("taker · withdrawal", "left"), ("ms", "right")):
        t.add_column(col, justify=justify, no_wrap=True, overflow="ellipsis",
                     ratio=1 if col == "taker · withdrawal" else None)
    for e in s.exchanges:
        style = EXCHANGE_STYLE.get(e.state, "")
        if e.state == "live":
            t.add_row(e.name, f"{e.bid:.4f}", f"{e.ask:.4f}",
                      f"{e.vs_oracle_percent:+.2f}%" if e.vs_oracle_percent is not None else "—",
                      e.fees, f"{e.latency_ms:.0f}" if e.latency_ms is not None else "")
        else:
            t.add_row(e.name, "—", "—", "—", Text(f"{e.state}: {e.error}" if e.error else e.state, style=style),
                      f"{e.latency_ms:.0f}" if e.latency_ms is not None else "", style="dim" if e.state == "disabled" else None)
    body = Group(t, Text(f"best spread: {s.spread_text}", style="dim")) if s.spread_text else t
    return Panel(body, title="Exchanges (watch-only, * = fee published by the exchange)")


def venues_panel(s: DashboardState) -> Panel:
    if not s.venues:
        return Panel(Text("waiting for the first scan…", style="dim"), title="Venues")
    split = bool(s.exchanges)        # CEX rows live in the Exchanges panel; these are all on-chain
    venues = [v for v in s.venues if v.kind != "CEX"] if split else s.venues
    t = Table(expand=True, box=None, pad_edge=False)
    cols = (("venue", "left"), ("status", "left"), ("quote", "left"), ("age", "right"), ("ms", "right"))
    if not split:
        cols = cols[:1] + (("kind", "left"),) + cols[1:]
    for col, justify in cols:
        t.add_column(col, justify=justify, no_wrap=True, overflow="ellipsis",
                     ratio=1 if col == "quote" else None)
    for v in venues:
        symbol, style = VENUE_STYLE.get(v.state, ("?", ""))
        label = "update pending" if v.state == "pending" and v.name == "Oracle" else v.state.replace("watch", "watch only")
        status = Text(f"{symbol} {label}", style=style)
        quote = v.quote if v.state != "down" else (v.error or "no data")
        cells = [v.name, status, quote, _age(v.age_s), f"{v.latency_ms:.0f}" if v.latency_ms is not None else ""]
        if not split:
            cells.insert(1, v.kind)
        t.add_row(*cells, style="dim" if v.state == "disabled" else None)
    return Panel(t, title="Venues")


def exchanges_line(s: DashboardState) -> Text:
    """Short terminals: every exchange in one (wrapping) line: mid price and gap to the oracle."""
    parts = []
    for e in s.exchanges:
        if e.state == "live":
            gap = f" {e.vs_oracle_percent:+.2f}%" if e.vs_oracle_percent is not None else ""
            parts.append(f"{e.name} {(e.bid + e.ask) / 2:.4f}{gap}")
        else:
            parts.append(f"{e.name} {e.state}")
    return Text("CEX (watch-only): " + " · ".join(parts), style="cyan")


def usd_routes_panel(s: DashboardState, compact: bool = False) -> Panel:
    """The USD view: every exit for the wallet's SigUSD, best dollar value first (information only)."""
    r = s.usd_routes
    if r is None:
        return Panel(Text("waiting for the pool, bank and exchange prices…", style="dim"), title="SigUSD routes")
    held = f"example {r.sigusd:g} SigUSD (wallet holds none)" if r.example else f"wallet {r.sigusd:,.2f} SigUSD"
    premium = f" · pool premium {r.premium_percent:+.1f}% vs bank" if r.premium_percent is not None else ""
    t = Table(expand=True, box=None, pad_edge=False)
    columns = [("route", "left"), ("you get", "right"), ("$ value", "right"), (f"vs ${r.sigusd:,.0f}", "right")]
    if not compact:
        columns.append(("vs bank", "right"))
    for col, justify in columns:
        t.add_column(col, justify=justify, no_wrap=True, overflow="ellipsis", ratio=1 if col == "route" else None)
    for route in r.routes:
        if route.amount is None:
            cells = [route.name, "—", "—", Text(route.note or "—", style="dim")]
            if not compact:
                cells.append("")
            t.add_row(*cells, style="dim")
            continue
        face, bank = r.vs_face(route), r.vs_bank(route)
        star = "*" if route.unit == "ERG" else ""
        cells = [route.name, f"{route.amount:,.2f} {route.unit}", f"{route.usd_text}{star}",
                 Text(f"{face:+.1f}%", style="green" if face > 0 else "red")]
        if not compact:
            cells.append(f"{bank:+.1f}%" if bank is not None else "—")
        t.add_row(*cells, style="bold" if route is r.best else None)
    foot = Text("* ERG valued at the oracle price; exchange routes sell into the order book after the taker "
                "fee (deposits assumed free). Information only: nothing is sent to an exchange.", style="dim")
    return Panel(Group(t, foot), title=f"SigUSD routes · {held}{premium}")


def _usd_rows(s: DashboardState) -> int:
    return (len(s.usd_routes.routes) if s.usd_routes else 1) + 5


def venues_compact_panel(s: DashboardState, short: bool = False) -> Panel:
    """Narrow terminals: on-chain venues and exchanges in one three-column table (short: exchanges in one line)."""
    if not s.venues:
        return Panel(Text("waiting for the first scan…", style="dim"), title="Venues")
    t = Table(expand=True, box=None, pad_edge=False, show_header=not short)
    for col in ("venue", "status", "quote"):
        t.add_column(col, no_wrap=True, overflow="ellipsis", ratio=1 if col == "quote" else None)
    on_chain = [v for v in s.venues if v.kind != "CEX"] if s.exchanges else s.venues
    for v in on_chain:
        symbol, style = VENUE_STYLE.get(v.state, ("?", ""))
        label = "update pending" if v.state == "pending" and v.name == "Oracle" else v.state.replace("watch", "watch only")
        t.add_row(v.name, Text(f"{symbol} {label}", style=style), v.quote if v.state != "down" else (v.error or "no data"),
                  style="dim" if v.state == "disabled" else None)
    for e in ([] if short else s.exchanges):
        if e.state == "live":
            gap = f" ({e.vs_oracle_percent:+.2f}%)" if e.vs_oracle_percent is not None else ""
            t.add_row(e.name, Text("○ watch only", style="cyan"), f"{e.bid:.4f} / {e.ask:.4f}{gap}")
        else:
            t.add_row(e.name, Text(e.state, style=EXCHANGE_STYLE.get(e.state, "")), e.error or "",
                      style="dim" if e.state == "disabled" else None)
    body = Group(t, exchanges_line(s)) if short and s.exchanges else t
    return Panel(body, title="Venues and exchanges (watch-only)" if s.exchanges else "Venues")


def _paths_rows(s: DashboardState, width: int, compact: bool, steps: bool) -> int:
    rows = 4      # the status column gets what the fixed columns leave; long reasons wrap onto more lines
    drops = COMPACT_DROPS if compact else set()
    fixed = sum(w + 1 for col, _, w in PATH_COLUMNS if col not in drops | {"status"}) + 4
    room = max(8, int((width - fixed) * 0.8))   # the table gives some columns more than their minimum
    rows += sum(-(-len(_status_text(r)) // room) for r in (s.paths or {}).values())
    if steps:
        rows += max((len(r.steps) for r in (s.paths or {}).values() if r.choice and r.choice.ok), default=0)
    return max(rows, 5)


def render(s: DashboardState, width: Optional[int] = None, height: Optional[int] = None) -> Layout:
    """width/height: the terminal size (None = a large one). Small terminals get the compact layout."""
    compact = width is not None and width < COMPACT_WIDTH
    short = height is not None and height < COMPACT_HEIGHT
    if compact or short:
        return _render_compact(s, width or 200, short)
    return _render_full(s, width or 200)


def _top(s: DashboardState) -> tuple[Layout, int]:
    trend_rows = 1 if rr_trend(s.rr_history, datetime.now().timestamp()) else 0
    top = Layout(name="top", size=max(7 + trend_rows, 6 + min(len(s.live_detail), LIVE_REASONS_SHOWN + 1)))
    right = Layout()
    right.split_column(Layout(live_panel(s), ratio=3), Layout(wallet_panel(s), size=3))
    top.split_row(Layout(prices_panel(s)), right)
    return top, top.size


def _render_compact(s: DashboardState, width: int, short: bool) -> Layout:
    layout = Layout()
    top, _ = _top(s)
    venue_rows = (len([v for v in s.venues if v.kind != "CEX"] if s.exchanges else s.venues)
                  + len(s.exchanges) + 3) if s.venues else 3
    middle_panel = (Layout(usd_routes_panel(s, compact=True), size=_usd_rows(s) + 1) if s.view_mode == "usd" else
                    Layout(paths_panel(s, compact=True, steps=not short), size=_paths_rows(s, width, True, not short)))
    parts = [Layout(header(s), size=1)] + ([] if short else [Layout(health_strip(s), size=1)]) + [top, middle_panel]
    if short:
        parts.append(Layout(venues_compact_panel(s, short=True)))     # takes what is left
    else:
        middle = Layout(name="middle", minimum_size=4)
        middle.split_row(Layout(events_panel(s), ratio=3), Layout(history_panel(s), ratio=2))
        parts += [middle, Layout(venues_compact_panel(s), size=venue_rows)]
    layout.split_column(*parts)
    return layout


def _render_full(s: DashboardState, width: int) -> Layout:
    layout = Layout()
    paths_rows = _paths_rows(s, width, False, True)
    on_chain = [v for v in s.venues if v.kind != "CEX"] if s.exchanges else s.venues
    bottom_rows = max(len(on_chain), len(s.exchanges) + (2 if s.spread_text else 0)) + 3 if s.venues else 3
    top, _ = _top(s)
    layout.split_column(
        Layout(header(s), size=1),
        Layout(health_strip(s), size=1),
        top,
        Layout(usd_routes_panel(s), size=_usd_rows(s)) if s.view_mode == "usd" else
        Layout(paths_panel(s), size=paths_rows),
        Layout(name="middle", minimum_size=4),
        Layout(name="bottom", size=bottom_rows),
    )
    layout["middle"].split_row(Layout(events_panel(s), ratio=3), Layout(history_panel(s), ratio=2))
    if s.exchanges:
        layout["bottom"].split_row(Layout(venues_panel(s), ratio=1), Layout(exchanges_panel(s), ratio=1))
    else:
        layout["bottom"].update(venues_panel(s))
    return layout


def render_safe(s: DashboardState, width: Optional[int] = None, height: Optional[int] = None):
    """Never raises: a renderer bug shows as an event instead of killing the dashboard (or the bot)."""
    try:
        return render(s, width, height)
    except Exception as e:
        try:
            s.add_event("error", f"dashboard render error: {e.__class__.__name__}: {e}")
            return Group(Text(f"dashboard render error: {e}", style="bold red"), events_panel(s))
        except Exception:
            return Text(f"dashboard render error: {e}", style="bold red")
