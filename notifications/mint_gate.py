"""Bank mint gate: confirmed open/closed transitions of SigUSD minting, and the dashboard text.

Open means the bank lets you mint at least `min_room_erg` worth now (post-mint RR >= 400%).
A transition needs `confirm_polls` agreeing readings in a row; a missing reading changes nothing.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from exchanges.sigmausd import mint_open_price, mint_room_nanoerg

logger = logging.getLogger("ergo_arb.mint_gate")


@dataclass
class MintOpening:
    opened_at: float
    message_id: Optional[str] = None   # the Discord message of this opening, set once posted


@dataclass
class MintGateEvent:
    kind: str                          # opened | closed
    reserve_ratio: float
    room_erg: float
    open_price: Optional[float]        # ERG/USD at which RR reaches 400%
    oracle_price: float
    opening: MintOpening
    open_for_s: Optional[float]        # closed: how long it was open
    ping: bool


def _reading(bank: Optional[dict]):
    """(reserve_ratio, room_erg, open_price, oracle_price) from a bank price entry, or None."""
    state = (bank or {}).get("state")
    if state is None:
        return None
    try:
        return (state.reserve_ratio, mint_room_nanoerg(state) / 1e9, mint_open_price(state),
                state.oracle_usd_per_erg)
    except Exception as e:  # a malformed state must not stop the poll
        logger.warning(f"Mint gate: unreadable bank state ({e})")
        return None


class MintGateWatcher:
    def __init__(self, confirm_polls: int, min_room_erg: float, ping_cooldown_s: float):
        self.confirm_polls = max(int(confirm_polls), 1)
        self.min_room_erg, self.ping_cooldown_s = min_room_erg, ping_cooldown_s
        self.is_open = False
        self._streak = 0
        self._opening: Optional[MintOpening] = None
        self._last_ping: Optional[float] = None

    def update(self, now: float, bank: Optional[dict]) -> Optional[MintGateEvent]:
        r = _reading(bank)
        if r is None:
            self._streak = 0
            return None
        rr, room, open_price, oracle = r
        if (room >= self.min_room_erg) == self.is_open:
            self._streak = 0
            return None
        self._streak += 1
        if self._streak < self.confirm_polls:
            return None
        self._streak = 0
        self.is_open = not self.is_open
        if self.is_open:
            self._opening = MintOpening(opened_at=now)
            ping = self._last_ping is None or now - self._last_ping >= self.ping_cooldown_s
            if ping:
                self._last_ping = now
            return MintGateEvent("opened", rr, room, open_price, oracle, self._opening, None, ping)
        opening = self._opening or MintOpening(opened_at=now)
        return MintGateEvent("closed", rr, room, open_price, oracle, opening, now - opening.opened_at, False)


def mint_gate_text(bank: Optional[dict], min_room_erg: float) -> Optional[str]:
    """Short status for the dashboard and digest ("✓ room ~85 ERG", "✗ needs ERG $0.392 (+21.7%)")."""
    r = _reading(bank)
    if r is None:
        return None
    rr, room, open_price, oracle = r
    if room >= min_room_erg:
        return f"✓ room ~{room:,.0f} ERG"
    if open_price is None or not oracle:
        return "✗"
    if open_price <= oracle:            # RR is already >= 400%, but the room is too small to trade
        return f"✗ room only {room:.2f} ERG"
    return f"✗ needs ERG ${open_price:.3f} ({(open_price / oracle - 1) * 100:+.1f}%)"
