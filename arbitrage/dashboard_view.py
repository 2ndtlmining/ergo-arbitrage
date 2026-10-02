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
LIVE_REASONS_SHOWN = 5


def sparkline(values) -> str:
    values = list(values)
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return SPARK[0] * len(values)
    return "".join(SPARK[int((v - lo) / (hi - lo) * (len(SPARK) - 1))] for v in values)


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
        mint = s.mint_text or ("✓" if bank.get("can_mint_sigusd") else "✗ (<400%)")
        rows.append(f"Bank    RR {bank['reserve_ratio']:.0f}%  redeem ✓  mint {mint}")
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


def paths_panel(s: DashboardState) -> Panel:
    if not s.paths:
        return Panel(Text("waiting for the first scan…", style="dim"), title="Paths")
    t = Table(expand=True, box=None, pad_edge=False)
    # Fixed minimums keep the numbers whole on narrow terminals; only the status reason shrinks.
    for col, justify, width in (("path", "left", 14), ("size", "right", 7), ("profit", "right", 8),
                                ("%", "right", 7), ("b/even", "right", 6), ("conf", "right", 4),
                                ("status", "left", 8), ("last 30", "left", 8)):
        t.add_column(col, justify=justify, no_wrap=True, overflow="ellipsis", min_width=width,
                     ratio=1 if col == "status" else None)
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
        Layout(name="top", size=max(7, 6 + min(len(s.live_detail), LIVE_REASONS_SHOWN + 1))),
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
