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
