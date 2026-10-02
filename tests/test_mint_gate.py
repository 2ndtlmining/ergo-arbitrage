"""Bank mint gate: forecast, watcher, text (spec: 2026-10-03-mint-gate-design)."""
import pytest

from exchanges.sigmausd import (BankState, can_mint_sigusd, mint_cost_nanoerg, mint_open_price, mint_room_cents,
                                mint_room_nanoerg)

ORACLE_R4 = 3_125_000_000                                            # $0.32 per ERG, rate 31_250_000 per cent
BLOCKED = BankState(1_006_250 * 10**9, 10_000_000, ORACLE_R4)        # RR 322.0%
OPEN = BankState(1_281_250 * 10**9, 10_000_000, ORACLE_R4)           # RR 410.0%
TINY = BankState(1_250_000 * 10**9 + 10**9, 10_000_000, ORACLE_R4)   # RR 400.0003%, room 0.32 ERG
NO_CIRCULATION = BankState(10**15, 0, ORACLE_R4)                    # RR infinite
NO_ORACLE = BankState(10**15, 10_000_000, 0)                        # rate 0


def test_open_price_matches_hand_arithmetic():
    assert BLOCKED.reserve_ratio == 322.0 and BLOCKED.oracle_usd_per_erg == 0.32
    assert mint_open_price(BLOCKED) == pytest.approx(0.32 * 400 / 322)   # $0.3975, ERG +24.2%


def test_open_price_is_none_without_a_ratio_or_oracle():
    assert mint_open_price(NO_CIRCULATION) is None and mint_open_price(NO_ORACLE) is None


def test_room_is_zero_while_blocked():
    assert mint_room_cents(BLOCKED) == 0 and mint_room_nanoerg(BLOCKED) == 0
    assert mint_room_cents(NO_ORACLE) == 0 and mint_room_nanoerg(NO_ORACLE) == 0


def test_room_is_the_largest_mint_that_keeps_rr_at_400():
    cents = mint_room_cents(OPEN)
    assert cents == 335_570 and can_mint_sigusd(OPEN, cents) and not can_mint_sigusd(OPEN, cents + 1)
    assert mint_room_nanoerg(OPEN) == mint_cost_nanoerg(OPEN, cents)
    assert 10_700 < mint_room_nanoerg(OPEN) / 1e9 < 10_750


def test_room_just_above_400_is_tiny():
    assert mint_room_cents(TINY) == 10 and mint_room_nanoerg(TINY) / 1e9 == pytest.approx(0.31975)


def test_room_without_circulation_is_large():
    assert mint_room_cents(NO_CIRCULATION) > 0


from notifications.mint_gate import MintGateWatcher, mint_gate_text


def bank(state):
    return {"state": state, "reserve_ratio": state.reserve_ratio}


def watcher(confirm=3, min_room=1.0, cooldown=3600):
    return MintGateWatcher(confirm_polls=confirm, min_room_erg=min_room, ping_cooldown_s=cooldown)


def test_opens_after_three_agreeing_polls_with_a_ping():
    w = watcher()
    assert w.update(0, bank(OPEN)) is None and w.update(2, bank(OPEN)) is None
    ev = w.update(4, bank(OPEN))
    assert (ev.kind, ev.ping, round(ev.reserve_ratio)) == ("opened", True, 410) and w.is_open
    assert 10_700 < ev.room_erg < 10_750 and ev.opening.opened_at == 4
    assert w.update(6, bank(OPEN)) is None          # steady state: silent


def test_a_single_odd_reading_is_ignored():
    w = watcher()
    for t in (0, 2):
        w.update(t, bank(OPEN))
    w.update(4, bank(BLOCKED))                      # the streak is broken
    assert w.update(6, bank(OPEN)) is None and w.update(8, bank(OPEN)) is None
    assert w.update(10, bank(OPEN)).kind == "opened"


def test_a_missing_reading_resets_the_streak():
    w = watcher()
    w.update(0, bank(OPEN))
    w.update(2, bank(OPEN))
    assert w.update(4, None) is None and w.update(5, {}) is None
    assert w.update(6, bank(OPEN)) is None          # counting again from 1


def test_starts_closed_so_blocked_is_silent():
    w = watcher()
    assert all(w.update(t, bank(BLOCKED)) is None for t in range(0, 20, 2)) and not w.is_open


def test_tiny_room_counts_as_closed():
    w = watcher()
    assert all(w.update(t, bank(TINY)) is None for t in range(0, 20, 2))


def test_closes_after_three_polls_and_reports_how_long():
    w = watcher()
    for t in (0, 2, 4):
        opened = w.update(t, bank(OPEN))
    for t in (100, 102):
        assert w.update(t, bank(BLOCKED)) is None
    ev = w.update(104, bank(BLOCKED))
    assert (ev.kind, ev.ping, ev.open_for_s) == ("closed", False, 100)
    assert ev.opening is opened.opening and ev.open_price == pytest.approx(0.32 * 400 / 322)


def test_each_opening_has_its_own_message_slot():
    """A close edit must still find the first message after a quick reopen (Review Focus 2)."""
    w = watcher(confirm=1)
    first = w.update(0, bank(OPEN))
    first.opening.message_id = "m1"
    closed = w.update(2, bank(BLOCKED))
    again = w.update(4, bank(OPEN))
    assert closed.opening.message_id == "m1" and again.opening is not first.opening
    assert again.opening.message_id is None


def test_ping_cooldown_while_rr_hovers():
    w = watcher(confirm=1, cooldown=3600)
    assert w.update(0, bank(OPEN)).ping is True
    w.update(10, bank(BLOCKED))
    second = w.update(20, bank(OPEN))
    assert second.kind == "opened" and second.ping is False   # posted, but no second ping within the hour
    w.update(30, bank(BLOCKED))
    assert w.update(3700, bank(OPEN)).ping is True


def test_text_for_each_state():
    assert mint_gate_text(bank(OPEN), 1.0) == "✓ room ~10,721 ERG"
    assert mint_gate_text(bank(BLOCKED), 1.0) == "✗ needs ERG $0.398 (+24.2%)"
    assert mint_gate_text(bank(TINY), 1.0) == "✗ room only 0.32 ERG"
    assert mint_gate_text(bank(NO_ORACLE), 1.0) == "✗"
    assert mint_gate_text(bank(NO_CIRCULATION), 1.0).startswith("✓ room ~")
    assert mint_gate_text(None, 1.0) is None and mint_gate_text({"reserve_ratio": 300}, 1.0) is None
