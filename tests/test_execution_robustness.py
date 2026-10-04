"""Execution review fixes: bookkeeping before anything that can fail, watch survives node blips,
every leg-2 id tracked, honest statuses, drawdown baseline only from a good reading."""
import asyncio
import sqlite3

import aiohttp

import ergo.arb_runner as runner
from arbitrage.calculator import ArbitrageOpportunity, FeeBreakdown
from ergo.arb_runner import ArbResult, leg1_landed, watch_leg2
from notifications.discord import DiscordNotifier
from tests.fake_node import FakeSession
from tests.test_live import WALLET


def run(coro):
    return asyncio.run(coro)


# --- I1: a tracker error after a trade must not undo the cooldown, the count or the pause -----------

def test_failed_bookkeeping_still_pauses_and_counts(live, monkeypatch):
    live._next_result = ArbResult("leg2_failed", "oracle moved", erg_in=10**10, sigusd_cents=310, tx1="t1")

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(live.tracker, "set_trade_input", locked)
    monkeypatch.setattr(live.tracker, "fail_trade", locked)
    run(live._execute_trades(WALLET, live._prices))          # must not raise into the poll loop
    assert live._live_paused and live._last_trade_time > 0 and live._trades_today_count() == 1
    run(live._execute_trades(WALLET, live._prices))
    assert len(live.calls) == 1                                # no second trade 2 s later


# --- I2 / M1 / M2: watching leg 2 ------------------------------------------------------------------------

def test_a_node_blip_while_watching_counts_as_pending():
    answers = iter([aiohttp.ClientConnectionError("reset"), asyncio.TimeoutError(), "confirmed"])

    async def status(tx_id):
        a = next(answers)
        if isinstance(a, BaseException):
            raise a
        return a

    async def rebuild():
        raise AssertionError("must not rebuild on a blip")

    assert run(watch_leg2("tx2", status, rebuild, timeout=5, interval=0, max_rebuilds=1,
                          log=lambda m: None)) == ("confirmed", "tx2")


def test_an_earlier_leg2_id_that_confirms_after_a_rebuild_counts():
    seen = {"tx2": iter(["dropped", "confirmed"]), "tx3": iter(["pending", "pending"])}

    async def status(tx_id):
        return next(seen[tx_id], "pending")

    async def rebuild():
        return "tx3"

    assert run(watch_leg2("tx2", status, rebuild, timeout=5, interval=0, max_rebuilds=3,
                          log=lambda m: None)) == ("confirmed", "tx2")


def test_leg2_still_pending_at_the_deadline_is_reported_as_pending():
    async def status(tx_id):
        return "pending"

    async def rebuild():
        raise AssertionError

    assert run(watch_leg2("tx2", status, rebuild, timeout=0.05, interval=0.01, max_rebuilds=1,
                          log=lambda m: None))[0] == "pending"


def test_leg1_found_in_the_wallet_counts_as_landed():
    """Nodes without extraIndex 404 on /blockchain/transaction/byId; the wallet still knows the tx."""
    ns = FakeSession({"/wallet/transactionById?id=t1": (200, {"numConfirmations": 3})})
    assert run(leg1_landed(ns, "t1", "box1", attempts=1, delay=0))


# --- M3: a node blip before leg 1 aborts cleanly ------------------------------------------------------

def test_a_timeout_before_leg1_is_an_abort_not_an_error(offline, monkeypatch):
    async def slow(ns, nft, explorer=None):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(runner, "latest_box", slow)
    assert run(runner.run_arb(None, "redeem", None, log=lambda m: None)).status == "aborted"


# --- I4: drawdown baseline ------------------------------------------------------------------------------

def test_no_baseline_from_an_unreadable_wallet(live):
    blockers = run(live._global_blockers({"erg": 0, "sigusd": 0, "use": 0, "ok": False}, live._prices))
    assert live._live_start_value is None and any("wallet balance unreadable" in b for b in blockers)


def test_no_baseline_without_an_oracle_price_when_holding_sigusd(live):
    blockers = run(live._global_blockers({"erg": 20.0, "sigusd": 5.0, "use": 0}, {"bank": {}}))
    assert live._live_start_value is None and any("drawdown baseline" in b for b in blockers)
    run(live._global_blockers(WALLET, live._prices))
    assert live._live_start_value is not None


def test_unreadable_balances_are_marked(live, monkeypatch):
    async def none():
        return None

    monkeypatch.setattr(live.ergo_node, "get_wallet_balances", none)
    assert run(live._fetch_wallet_balances())["ok"] is False


# --- 30-minute summary: blocked rows are labelled ------------------------------------------------------

def test_summary_labels_blocked_paths():
    blocked = ArbitrageOpportunity(
        path="Bank mint->ErgoDEX sell [100 ERG]", input_erg=100, output_erg=102.57, profit_erg=2.5681,
        profit_percent=2.57, fees=FeeBreakdown(), source_price=0, target_price=0, source_exchange="Bank",
        target_exchange="Pool", is_profitable=True, blocked=True,
        blocked_reason="Bank mint blocked (RR=329%, post-mint RR would drop below 400%)", profit_usd=0.83)
    text = DiscordNotifier()._format_scan_summary([blocked], scan_number=566)
    row = next(line for line in text.splitlines() if "Bank mint->ErgoDEX sell" in line)
    assert "BLOCKED" in row and "RR=329%" in text
    assert "0 profitable paths" in text           # a blocked path is never counted as profitable
