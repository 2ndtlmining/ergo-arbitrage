"""Fail-closed behaviour from the chain-watcher branch review."""
import asyncio

import aiohttp

import arbitrage.scanner as scanner_module
import config
from ergo.chain_state import ChainSnapshot
from tests.test_bank_redeem_tx import BANK_BOX, ORACLE_BOX
from tests.test_chain_arb import pool_box
from tests.test_chain_scanner import KEY, Reader, scanner, snap  # noqa: F401  (fixture)


# Important 1: any failure to read or price chain state fails closed

def test_unexpected_read_error_in_fast_poll_blocks_and_resets(scanner, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), TypeError("string indices must be integers")))
    asyncio.run(scanner.poll_once(0))
    asyncio.run(scanner.poll_once(2))
    asyncio.run(scanner.poll_once(4))
    assert scanner.calls == []
    assert scanner._chain_error and scanner._live_streak.get(KEY, 0) == 0


def test_unexpected_read_error_in_full_scan_does_not_trade_on_stale_state(scanner, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), TypeError("bad json")))
    asyncio.run(scanner.poll_once(0))     # good read, streak 1
    asyncio.run(scanner.poll_once(15))    # full scan, read fails
    asyncio.run(scanner.poll_once(30))    # full scan, read fails
    assert scanner.calls == []
    assert scanner._chain_error


def test_unpriceable_snapshot_counts_as_unreadable(scanner, monkeypatch):
    broken = ChainSnapshot(1, dict(pool_box(0.35, 2_000 * 10**9), additionalRegisters={}), BANK_BOX, ORACLE_BOX,
                           frozenset(), 1.0)  # pool box without its fee register
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), broken))
    for t in (0, 2, 4):
        asyncio.run(scanner.poll_once(t))
    assert scanner.calls == [] and scanner._chain_error


# Important 2: an unexpected runner error pauses live trading instead of escaping

def test_runner_crash_pauses_live_and_records_the_trade(scanner, monkeypatch):
    async def crash(ns, path, erg_in, **kw):
        raise aiohttp.ClientConnectionError("server disconnected")

    monkeypatch.setattr(scanner_module, "run_arb", crash)
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    asyncio.run(scanner.poll_once(0))
    asyncio.run(scanner.poll_once(2))     # would trade: runner raises
    assert scanner._live_paused and "server disconnected" in scanner._live_paused
    assert scanner._last_trade_time > 0
    row = scanner.tracker.conn.execute("SELECT status FROM trades").fetchone()
    assert row["status"] == "failed"


def test_leg2_rebuild_retries_after_a_connection_error():
    from ergo.arb_runner import watch_leg2
    statuses = ["dropped", "dropped", "confirmed"]
    calls = []

    async def status(tx):
        return statuses.pop(0)

    async def rebuild():
        calls.append(1)
        if len(calls) == 1:
            raise aiohttp.ClientConnectionError("node restarting")
        return "tx2-ok"

    result = asyncio.run(watch_leg2("tx2", status, rebuild, timeout=5, interval=0, max_rebuilds=3, log=lambda m: None))
    assert result == ("confirmed", "tx2-ok")


# Important 3: CLI bank actions wait out a pending oracle update

def test_cli_redeem_aborts_while_an_oracle_update_is_pending(monkeypatch):
    from ergo import actions

    async def latest(ns, nft, explorer=None):
        boxes = {config.SIGMAUSD_BANK_NFT: BANK_BOX, config.SIGMAUSD_ORACLE_NFT: ORACLE_BOX}
        return boxes[nft], nft == config.SIGMAUSD_ORACLE_NFT

    async def no_wallet(ns):
        raise AssertionError("must abort before reading the wallet")

    monkeypatch.setattr(actions, "latest_box", latest)
    monkeypatch.setattr(actions, "wallet_context", no_wallet)
    logs = []
    asyncio.run(actions.redeem(None, 1.0, "execute", logs.append))
    assert any("ABORTED" in m and "oracle update pending" in m for m in logs)


def test_cli_quote_still_works_while_an_oracle_update_is_pending(monkeypatch):
    from ergo import actions

    async def latest(ns, nft, explorer=None):
        boxes = {config.SPECTRUM_SIGUSD_POOL_NFT: pool_box(0.35, 2_000 * 10**9), config.SIGMAUSD_BANK_NFT: BANK_BOX,
                 config.SIGMAUSD_ORACLE_NFT: ORACLE_BOX}
        return boxes[nft], nft == config.SIGMAUSD_ORACLE_NFT

    monkeypatch.setattr(actions, "latest_box", latest)
    logs = []
    asyncio.run(actions.quote(None, None, None, logs.append))
    assert any("Best arbitrage size" in m for m in logs)


# Important 4: blocked live output is not repeated every poll

def test_blocked_live_lines_print_once_until_the_reasons_change(scanner, monkeypatch, tmp_path):
    from logging_config import console
    open(config.LIVE_STOP_FILE, "w").close()
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    asyncio.run(scanner.poll_once(0))                     # full scan prints the blockers
    with console.capture() as cap:
        for t in (2, 4, 6, 8):
            asyncio.run(scanner.poll_once(t))
    # printed at most once (when the streak blocker cleared and only the STOP file remained), not every poll
    assert cap.get().count("LIVE redeem: not trading") <= 1
    import os
    os.remove(config.LIVE_STOP_FILE)
    assert scanner.calls == []
    asyncio.run(scanner.poll_once(10))                    # reasons gone: trades
    assert scanner.calls == ["redeem"]
