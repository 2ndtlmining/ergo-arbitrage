"""Follow-ups to the chain-watcher review: accurate failure reports, less noise, fewer explorer calls."""
import asyncio

import aiohttp
import pytest

import arbitrage.scanner as scanner_module
import config
from ergo.arb_runner import ArbResult, LegNotReady, leg1_landed, watch_leg2
from ergo.chain_state import latest_box
from tests.fake_node import FakeSession, Seq
from tests.test_chain_scanner import Reader, snap


def run(coro):
    return asyncio.run(coro)


def box(box_id):
    return {"boxId": box_id, "value": 10**12, "ergoTree": "00", "assets": [], "additionalRegisters": {}}


# 1. Was leg 1 actually spent when leg 2 fails?

TX1, OUT1 = "tx1", "leg1-out"


@pytest.mark.parametrize("route", [f"/blockchain/transaction/byId/{TX1}",              # confirmed
                                   f"/transactions/unconfirmed/byTransactionId/{TX1}",  # in the mempool
                                   f"/utxo/withPool/byId/{OUT1}"])                       # its output exists
def test_leg1_landed_when_any_sign_of_it_exists(route):
    assert run(leg1_landed(FakeSession({route: (200, {})}), TX1, OUT1, delay=0)) is True


def test_leg1_dropped_only_after_every_check_fails_repeatedly():
    ns = FakeSession({})
    assert run(leg1_landed(ns, TX1, OUT1, attempts=3, delay=0)) is False
    assert sum(TX1 in c for c in ns.calls) >= 6  # both tx checks, three times


def test_leg1_seen_on_a_later_attempt_counts_as_landed():
    ns = FakeSession({f"/blockchain/transaction/byId/{TX1}": Seq((404, None), (200, {}))})
    assert run(leg1_landed(ns, TX1, OUT1, attempts=3, delay=0)) is True


def test_node_errors_count_as_landed():
    """Unsure means assume the SigUSD may be in the wallet (the cautious report)."""
    ns = FakeSession({f"/blockchain/transaction/byId/{TX1}": aiohttp.ClientConnectionError("down")})
    assert run(leg1_landed(ns, TX1, OUT1, attempts=2, delay=0)) is True


def test_scanner_does_not_pause_when_nothing_was_spent(scanner, monkeypatch):
    async def dropped(ns, path, erg_in, **kw):
        return ArbResult("leg1_dropped", "leg 1 was dropped from the mempool", erg_in=5 * 10**9, tx1="t1", path=path)

    monkeypatch.setattr(scanner_module, "run_arb", dropped)
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    run(scanner.poll_once(0))
    run(scanner.poll_once(2))
    assert scanner._live_paused is None
    row = scanner.tracker.conn.execute("SELECT status, notes FROM trades").fetchone()
    assert row["status"] == "failed" and "nothing spent" in row["notes"]


# 2. First leg-2 build blocked by an oracle update: hand over to the watcher

def test_watch_starts_by_building_leg2_when_there_is_none_yet():
    statuses = ["confirmed"]
    built = []

    async def status(tx):
        assert tx is not None
        return statuses.pop(0)

    async def rebuild():
        built.append(1)
        return "tx2-first"

    result = run(watch_leg2(None, status, rebuild, timeout=5, interval=0, max_rebuilds=1, log=lambda m: None))
    assert result == ("confirmed", "tx2-first") and built == [1]


def test_waiting_on_an_oracle_update_does_not_log_a_rebuild_count():
    statuses = ["dropped", "dropped", "confirmed"]
    calls, logs = [], []

    async def status(tx):
        return statuses.pop(0)

    async def rebuild():
        calls.append(1)
        if len(calls) == 1:
            raise LegNotReady("oracle update pending")
        return "tx2-new"

    run(watch_leg2("tx2", status, rebuild, timeout=5, interval=0, max_rebuilds=3, log=logs.append))
    waiting = [m for m in logs if "waiting" in m]
    assert waiting and not any("(1/3)" in m for m in logs[:logs.index(waiting[0]) + 1])
    assert any("(1/3)" in m for m in logs)  # the real rebuild is counted


# 3. Pending check vs confirmed lookup race

def test_box_spent_between_checks_is_retried_as_pending():
    nft = config.SPECTRUM_SIGUSD_POOL_NFT
    ns = FakeSession({f"/transactions/unconfirmed/outputs/byTokenId/{nft}": Seq((200, []), (200, [box("p2")])),
                      "/utxo/withPool/byId/p2": (200, box("p2")),
                      f"/blockchain/box/unspent/byTokenId/{nft}?offset=0&limit=1": (200, [box("c1")]),
                      "/utxo/withPool/byId/c1": (404, None)})  # spent by a TX that arrived meanwhile
    b, pending = run(latest_box(ns, nft))
    assert b["boxId"] == "p2" and pending


# 4. Confirmed box ids are cached (no index/explorer lookup every poll)

def test_confirmed_box_id_is_cached_between_polls():
    nft = config.SPECTRUM_SIGUSD_POOL_NFT
    index = f"/blockchain/box/unspent/byTokenId/{nft}?offset=0&limit=1"
    ns = FakeSession({f"/transactions/unconfirmed/outputs/byTokenId/{nft}": (200, []),
                      index: (200, [box("c1")]), "/utxo/withPool/byId/c1": (200, box("c1"))})
    run(latest_box(ns, nft))
    run(latest_box(ns, nft))
    assert sum(c.endswith(index) for c in ns.calls) == 1


def test_cached_box_id_is_refreshed_once_the_box_is_spent():
    nft = config.SIGMAUSD_ORACLE_NFT
    index = f"/blockchain/box/unspent/byTokenId/{nft}?offset=0&limit=1"
    ns = FakeSession({f"/transactions/unconfirmed/outputs/byTokenId/{nft}": (200, []),
                      index: Seq((200, [box("o1")]), (200, [box("o2")])),
                      "/utxo/byId/o1": Seq((200, box("o1")), (404, None)), "/utxo/byId/o2": (200, box("o2"))})
    assert run(latest_box(ns, nft))[0]["boxId"] == "o1"
    assert run(latest_box(ns, nft))[0]["boxId"] == "o2"  # o1 spent in a block -> looked up again


# 5. Old setting name

def test_old_live_confirm_setting_is_reported(monkeypatch):
    monkeypatch.setenv("LIVE_CONFIRM_SCANS", "10")
    assert any("LIVE_CONFIRM_SCANS" in w and "LIVE_CONFIRM_POLLS" in w for w in config.deprecated_settings())
    monkeypatch.delenv("LIVE_CONFIRM_SCANS")
    assert config.deprecated_settings() == []


# 6. No logging or alerts on stale prices during an outage

def test_full_scan_during_an_outage_does_not_log_or_alert(scanner, monkeypatch):
    logged, alerted = [], []
    monkeypatch.setattr(scanner.tracker, "log_price_snapshot", lambda p: logged.append(1) or 1)

    async def notify(opps):
        alerted.append(1)

    monkeypatch.setattr(scanner, "_notify_discord", notify)
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), RuntimeError("node down")))
    run(scanner.poll_once(0))
    assert logged == [1] and alerted == [1]
    run(scanner.poll_once(15))
    assert logged == [1] and alerted == [1]
