"""Health alerts for Discord from the dashboard state: chain unreadable, a venue down, an oracle
update stuck, live trading paused. One alert per subject per repeat window; a recovery always
follows an alert. Outages are remembered (since this process started) for the daily digest."""
from dataclasses import dataclass
from typing import Optional

from notifications.embeds import duration

LOG_KEEP_S = 48 * 3600  # outages that ended longer ago are dropped (the digest covers 24 h)
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
            # no ping: the live trade alert (notify_live) already pinged for the failure that paused it
            failing["live"] = (0.0, False, f"Live trading paused: {'; '.join(state.live_detail) or 'see console'}")
        return failing

    def update(self, now: float, state) -> list[HealthEvent]:
        events = []
        self._log = [e for e in self._log if e[2] is None or now - e[2] < LOG_KEEP_S]
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
