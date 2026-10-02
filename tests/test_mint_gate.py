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
