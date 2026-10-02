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
