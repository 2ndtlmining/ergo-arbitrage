"""Scanner on node snapshots: prices, chain-unavailable gate (spec: chain watcher)."""
import asyncio

import pytest

import arbitrage.scanner as scanner_module
import config
from arbitrage.scanner import ArbitrageScanner
from ergo.chain_state import ChainSnapshot
from tests.test_bank_redeem_tx import BANK_BOX, ORACLE_BOX
from tests.test_chain_arb import pool_box

KEY = "Spectrum buy->Bank redeem"
HEALTHY = {"reachable": True, "synced": True, "unlocked": True, "height": 1, "headers": 1, "ok_to_trade": True}
WALLET = {"erg": 50.0, "sigusd": 0.0, "use": 0.0}


def snap(pool_id="pool-a", pending=frozenset()):
    """Profitable for pool buy -> bank redeem (pool sells SigUSD cheap)."""
    return ChainSnapshot(1_885_700, dict(pool_box(0.35, 2_000 * 10**9), boxId=pool_id), BANK_BOX, ORACLE_BOX,
                         frozenset(pending), 12.0)


class Reader:
    """Stand-in for read_snapshot: returns queued snapshots or raises queued exceptions (the last repeats)."""

    def __init__(self, *items):
        self.items = list(items)

    async def __call__(self, ns, explorer=None):
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 1000.0)
    monkeypatch.setattr(config, "LIVE_STOP_FILE", str(tmp_path / "STOP"))
    s = ArbitrageScanner(mode="live", db_path=str(tmp_path / "t.db"))
    calls = []

    async def fake_run(ns, path, erg_in, **kw):
        calls.append(path)
        from ergo.arb_runner import ArbResult
        return ArbResult("executed", erg_in=5 * 10**9, sigusd_cents=100, profit_nanoerg=10**8,
                         profit_percent=2.0, tx1="t1", tx2="t2", path=path)

    async def healthy():
        return dict(HEALTHY)

    async def wallet():
        return dict(WALLET)

    monkeypatch.setattr(scanner_module, "run_arb", fake_run)
    monkeypatch.setattr(s.ergo_node, "get_health", healthy)
    monkeypatch.setattr(s, "_fetch_wallet_balances", wallet)
    s.calls = calls
    yield s
    s.tracker.close()


def test_prices_come_from_the_node_snapshot(scanner, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    prices = asyncio.run(scanner.fetch_all_prices())
    assert prices["spectrum_pool"].fee_num == 995
    assert prices["bank"]["state"].oracle_r4 == 3_100_000_000
    assert scanner._snapshot.key[0] == "pool-a" and scanner._chain_error is None


def test_failed_read_keeps_last_state_for_display_but_blocks_trading(scanner, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), RuntimeError("node down")))
    asyncio.run(scanner.fetch_all_prices())
    prices = asyncio.run(scanner.fetch_all_prices())
    assert prices["spectrum_pool"] is not None           # last good state still shown
    assert scanner._chain_error == "node down"
    blockers = asyncio.run(scanner._global_blockers(WALLET, prices))
    assert any("chain state unavailable" in b for b in blockers)


def test_failed_read_resets_live_streaks(scanner, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), asyncio.TimeoutError()))
    prices = asyncio.run(scanner.fetch_all_prices())
    scanner._find_opportunities(prices)
    scanner._update_live_streak()
    assert scanner._live_streak[KEY] == 1
    asyncio.run(scanner.fetch_all_prices())
    scanner._update_live_streak()
    assert scanner._live_streak[KEY] == 0


def test_pending_oracle_update_blocks_trading(scanner, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(pending={"oracle"})))
    prices = asyncio.run(scanner.fetch_all_prices())
    blockers = asyncio.run(scanner._global_blockers(WALLET, prices))
    assert any("oracle update pending" in b for b in blockers)


def test_pending_pool_box_does_not_block(scanner, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(pending={"pool"})))
    prices = asyncio.run(scanner.fetch_all_prices())
    blockers = asyncio.run(scanner._global_blockers(WALLET, prices))
    assert not any("pending" in b or "chain" in b for b in blockers)
