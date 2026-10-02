"""Mempool-aware contract boxes from the node (spec: chain watcher)."""
import asyncio

import pytest

import config
from ergo.chain_state import ChainSnapshot, latest_box, read_snapshot
from tests.fake_node import FakeSession

NFT = config.SPECTRUM_SIGUSD_POOL_NFT
UNCONF = f"/transactions/unconfirmed/outputs/byTokenId/{NFT}"
INDEX = f"/blockchain/box/unspent/byTokenId/{NFT}?offset=0&limit=1"


def box(box_id, value=10**12):
    return {"boxId": box_id, "value": value, "ergoTree": "00", "assets": [], "additionalRegisters": {}}


def run(coro):
    return asyncio.run(coro)


class TestLatestBox:
    def test_pending_output_unspent_in_pool_wins(self):
        ns = FakeSession({UNCONF: (200, [box("p1")]),
                          "/utxo/withPool/byId/p1": (200, box("p1")),
                          INDEX: (200, [box("c1")]),
                          "/utxo/withPool/byId/c1": (200, box("c1"))})
        b, pending = run(latest_box(ns, NFT))
        assert b["boxId"] == "p1" and pending is True

    def test_spent_pending_output_is_skipped(self):
        # p1 already spent by a second mempool tx that created p2
        ns = FakeSession({UNCONF: (200, [box("p1"), box("p2")]),
                          "/utxo/withPool/byId/p1": (404, None),
                          "/utxo/withPool/byId/p2": (200, box("p2"))})
        b, pending = run(latest_box(ns, NFT))
        assert b["boxId"] == "p2" and pending

    def test_no_pending_uses_confirmed_index_box(self):
        ns = FakeSession({UNCONF: (200, []), INDEX: (200, [box("c1")]),
                          "/utxo/withPool/byId/c1": (200, box("c1"))})
        b, pending = run(latest_box(ns, NFT))
        assert b["boxId"] == "c1" and pending is False

    def test_old_node_without_unconfirmed_endpoint(self):
        ns = FakeSession({UNCONF: (400, {"error": 400}), INDEX: (200, [box("c1")]),
                          "/utxo/withPool/byId/c1": (200, box("c1"))})
        b, pending = run(latest_box(ns, NFT))
        assert b["boxId"] == "c1" and not pending

    def test_node_without_index_uses_explorer_for_the_id(self):
        ns = FakeSession({UNCONF: (200, []), INDEX: (404, None),
                          "/utxo/withPool/byId/c9": (200, box("c9"))})
        explorer = FakeSession({f"/boxes/unspent/byTokenId/{NFT}?limit=1": (200, {"items": [box("c9")]})})
        b, pending = run(latest_box(ns, NFT, explorer=explorer))
        assert b["boxId"] == "c9" and not pending

    def test_nothing_found_raises(self, monkeypatch):
        import ergo.chain
        monkeypatch.setattr(ergo.chain, "EXPLORER_ATTEMPTS", 1)  # skip the explorer retry sleeps
        ns = FakeSession({UNCONF: (200, []), INDEX: (200, [])})
        explorer = FakeSession({f"/boxes/unspent/byTokenId/{NFT}?limit=1": (200, {"items": []})})
        with pytest.raises(RuntimeError):
            run(latest_box(ns, NFT, explorer=explorer))


def snapshot_node(pending_pool=False):
    routes = {"/info": (200, {"fullHeight": 1_885_700})}
    for name, nft in (("pool", config.SPECTRUM_SIGUSD_POOL_NFT), ("bank", config.SIGMAUSD_BANK_NFT),
                      ("oracle", config.SIGMAUSD_ORACLE_NFT)):
        pend = pending_pool and name == "pool"
        routes[f"/transactions/unconfirmed/outputs/byTokenId/{nft}"] = (200, [box(f"{name}-p")] if pend else [])
        routes[f"/utxo/withPool/byId/{name}-p"] = (200, box(f"{name}-p"))
        routes[f"/blockchain/box/unspent/byTokenId/{nft}?offset=0&limit=1"] = (200, [box(f"{name}-c")])
        routes[f"/utxo/withPool/byId/{name}-c"] = (200, box(f"{name}-c"))
        routes[f"/utxo/byId/{name}-c"] = (200, box(f"{name}-c"))
    return FakeSession(routes)


class TestSnapshot:
    def test_reads_all_three_and_height(self):
        snap = run(read_snapshot(snapshot_node()))
        assert snap.key == ("pool-c", "bank-c", "oracle-c")
        assert snap.height == 1_885_700 and snap.pending == frozenset() and snap.read_ms >= 0

    def test_pending_pool_changes_the_key(self):
        a = run(read_snapshot(snapshot_node()))
        b = run(read_snapshot(snapshot_node(pending_pool=True)))
        assert b.key != a.key and b.key[0] == "pool-p" and b.pending == frozenset({"pool"})

    def test_failure_propagates(self):
        ns = snapshot_node()
        ns.routes["/info"] = asyncio.TimeoutError()
        with pytest.raises(asyncio.TimeoutError):
            run(read_snapshot(ns))


ORA = config.SIGMAUSD_ORACLE_NFT


def oracle_node(pending: bool):
    return FakeSession({f"/transactions/unconfirmed/outputs/byTokenId/{ORA}": (200, [box("o-p")] if pending else []),
                        "/utxo/withPool/byId/o-p": (200, box("o-p")),
                        f"/blockchain/box/unspent/byTokenId/{ORA}?offset=0&limit=1": (200, [box("o-c")]),
                        "/utxo/withPool/byId/o-c": (404, None),  # spent in the mempool by the update
                        "/utxo/byId/o-c": (200, box("o-c"))})


def test_pending_oracle_update_keeps_confirmed_box_and_flags_it():
    """The oracle is only a data input; mempool data inputs are unverified, so price and build on the
    confirmed box and report the pending update (the live gate waits for it)."""
    b, pending = run(latest_box(oracle_node(pending=True), ORA))
    assert b["boxId"] == "o-c" and pending is True


def test_oracle_without_update_is_confirmed():
    b, pending = run(latest_box(oracle_node(pending=False), ORA))
    assert b["boxId"] == "o-c" and pending is False
