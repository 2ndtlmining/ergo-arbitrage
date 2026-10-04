"""Speed: no hidden plain-view rendering, one wallet analysis per scan (#75); fewer node calls per poll (#76)."""
import asyncio

import pytest

import config
from arbitrage.scanner import ArbitrageScanner
from ergo.chain_state import read_snapshot
from tests.test_chain_state import box, snapshot_node
from tests.test_optimizer import discount_prices


def run(coro):
    return asyncio.run(coro)


# ---------- #75 ----------

@pytest.fixture
def scan(tmp_path, monkeypatch):
    made = []

    def make(view, discord=False):
        s = ArbitrageScanner(mode="notify", db_path=str(tmp_path / f"{view}.db"), view=view)
        made.append(s)
        calls = {"display": 0, "analysis": 0}
        prices = discount_prices(pool_erg=2_000)

        async def fetch():
            return prices

        async def nothing(*a, **k):
            return None

        async def wallet():
            return {"erg": 50.0, "sigusd": 0.0, "use": 0.0}

        def display(*a, **k):
            calls["display"] += 1

        real_analysis = s._build_wallet_analysis

        def analysis(*a, **k):
            calls["analysis"] += 1
            return real_analysis(*a, **k)

        monkeypatch.setattr(s, "fetch_all_prices", fetch)
        monkeypatch.setattr(s, "_refresh_node_health", nothing)
        monkeypatch.setattr(s, "_fetch_wallet_balances", wallet)
        for name in ("_display_prices", "_display_opportunities", "_display_cex_watch"):
            monkeypatch.setattr(s, name, display)
        monkeypatch.setattr(s, "_build_wallet_analysis", analysis)
        monkeypatch.setattr(config, "DISCORD_ENABLED", discord)
        if discord:
            monkeypatch.setattr(s.discord, "send_wallet_analysis", nothing)
            monkeypatch.setattr(s.discord, "send_scan_summary", nothing)
            monkeypatch.setattr(s, "_notify_discord", nothing)
            monkeypatch.setattr(s, "_notify_cex_watch", nothing)
            monkeypatch.setattr(s, "_should_send_wallet_analysis", lambda opps: True)
        return s, calls

    yield make
    for s in made:
        s.tracker.close()


def test_dashboard_view_skips_the_plain_tables(scan):
    s, calls = scan("dashboard")
    run(s.scan_once())
    assert calls == {"display": 0, "analysis": 0}


def test_plain_view_still_prints_them(scan):
    s, calls = scan("plain")
    run(s.scan_once())
    assert calls["display"] >= 2 and calls["analysis"] == 1


def test_wallet_analysis_is_built_once_for_plain_and_discord(scan):
    s, calls = scan("plain", discord=True)
    run(s.scan_once())
    assert calls["analysis"] == 1


def test_dashboard_with_discord_builds_it_only_for_discord(scan):
    s, calls = scan("dashboard", discord=True)
    run(s.scan_once())
    assert calls == {"display": 0, "analysis": 1}


def test_dashboard_refreshes_once_a_second():
    import inspect
    import main
    assert "refresh_per_second=1" in inspect.getsource(main.run)


# ---------- #76 ----------

def node_calls(ns):
    return len(ns.calls)


def test_an_idle_poll_makes_four_node_calls():
    ns = snapshot_node()
    first = run(read_snapshot(ns))
    ns.calls.clear()
    again = run(read_snapshot(ns))
    assert node_calls(ns) == 4                     # 3 mempool lookups + /info, no box reads
    assert again.key == first.key


def test_a_new_block_reads_the_boxes_again():
    ns = snapshot_node()
    run(read_snapshot(ns))
    ns.routes["/info"] = (200, {"fullHeight": 1_885_701})
    nft = config.SPECTRUM_SIGUSD_POOL_NFT
    ns.routes["/utxo/withPool/byId/pool-c"] = (404, None)          # the pool box was spent in that block
    ns.routes[f"/blockchain/box/unspent/byTokenId/{nft}?offset=0&limit=1"] = (200, [box("pool-c2")])
    ns.routes["/utxo/withPool/byId/pool-c2"] = (200, box("pool-c2"))
    snap = run(read_snapshot(ns))
    assert snap.key[0] == "pool-c2"


def test_a_pending_transaction_is_picked_up_at_once():
    ns = snapshot_node()
    run(read_snapshot(ns))
    nft = config.SPECTRUM_SIGUSD_POOL_NFT
    ns.routes[f"/transactions/unconfirmed/outputs/byTokenId/{nft}"] = (200, [box("pool-p")])
    snap = run(read_snapshot(ns))
    assert snap.key[0] == "pool-p" and snap.pending == frozenset({"pool"})


def test_a_different_block_at_the_same_height_reads_again():
    ns = snapshot_node()
    ns.routes["/info"] = (200, {"fullHeight": 1_885_700, "bestFullHeaderId": "aa"})
    run(read_snapshot(ns))
    ns.routes["/info"] = (200, {"fullHeight": 1_885_700, "bestFullHeaderId": "bb"})   # a reorg
    ns.calls.clear()
    run(read_snapshot(ns))
    assert node_calls(ns) > 4


def test_a_pending_oracle_update_keeps_reusing_the_confirmed_boxes():
    ns = snapshot_node()
    run(read_snapshot(ns))
    ora = config.SIGMAUSD_ORACLE_NFT
    ns.routes[f"/transactions/unconfirmed/outputs/byTokenId/{ora}"] = (200, [box("oracle-p")])
    ns.calls.clear()
    snap = run(read_snapshot(ns))
    assert snap.key[2] == "oracle-c" and snap.pending == frozenset({"oracle"})
    assert not any("/utxo/byId/" in c for c in ns.calls)        # the confirmed oracle box came from the cache


def test_live_gate_reads_wallet_and_health_in_parallel(tmp_path, monkeypatch):
    s = ArbitrageScanner(mode="live", db_path=str(tmp_path / "t.db"))
    running, peak = [0], [0]

    async def slow(result):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        await asyncio.sleep(0.05)
        running[0] -= 1
        return result

    monkeypatch.setattr(s, "_fetch_wallet_balances", lambda: slow({"erg": 1.0, "sigusd": 0, "use": 0}))
    monkeypatch.setattr(s.ergo_node, "get_health", lambda: slow({"ok_to_trade": True}))
    wallet, health = run(s._wallet_and_health())
    assert peak[0] == 2 and wallet["erg"] == 1.0 and health["ok_to_trade"]
    s.tracker.close()
