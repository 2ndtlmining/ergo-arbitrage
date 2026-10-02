"""Leg 2 watcher: confirm, or rebuild against fresh bank/oracle boxes when it drops out of the mempool."""
import asyncio

import pytest

from ergo.arb_runner import watch_leg2


def run(coro):
    return asyncio.run(coro)


class Script:
    """get_status returns the scripted statuses in order; rebuild returns new tx ids."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.checked, self.rebuilds = [], 0

    async def get_status(self, tx_id):
        self.checked.append(tx_id)
        return self.statuses.pop(0)

    async def rebuild(self):
        self.rebuilds += 1
        return f"tx2-r{self.rebuilds}"


def test_confirmed_without_rebuild():
    s = Script(["pending", "confirmed"])
    status, tx = run(watch_leg2("tx2", s.get_status, s.rebuild, timeout=5, interval=0, max_rebuilds=3, log=lambda m: None))
    assert (status, tx, s.rebuilds) == ("confirmed", "tx2", 0)


def test_dropped_leg2_is_rebuilt_and_tracked():
    # oracle updated -> leg 2 dropped -> rebuilt -> new tx confirms
    s = Script(["pending", "dropped", "pending", "confirmed"])
    status, tx = run(watch_leg2("tx2", s.get_status, s.rebuild, timeout=5, interval=0, max_rebuilds=3, log=lambda m: None))
    assert status == "confirmed" and tx == "tx2-r1" and s.rebuilds == 1
    assert s.checked[-1] == "tx2-r1"


def test_gives_up_after_max_rebuilds():
    s = Script(["dropped"] * 10)
    status, _ = run(watch_leg2("tx2", s.get_status, s.rebuild, timeout=5, interval=0, max_rebuilds=2, log=lambda m: None))
    assert status == "gave_up" and s.rebuilds == 2


def test_rebuild_failure_is_retried_next_round():
    s = Script(["dropped", "dropped", "confirmed"])
    calls = []

    async def flaky_rebuild():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("bank box moved again")
        return "tx2-ok"

    status, tx = run(watch_leg2("tx2", s.get_status, flaky_rebuild, timeout=5, interval=0, max_rebuilds=3, log=lambda m: None))
    assert status == "confirmed" and tx == "tx2-ok"


def test_times_out_while_pending():
    s = Script(["pending"] * 10**4)
    status, _ = run(watch_leg2("tx2", s.get_status, s.rebuild, timeout=0.05, interval=0.01, max_rebuilds=3, log=lambda m: None))
    assert status == "timeout"




class _Resp:
    def __init__(self, status, payload=None):
        self.status, self._payload = status, payload or {}

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Node:
    """wallet: wallet tx json or None; leg1_visible: leg 1 output shown by /utxo/withPool (not spent in mempool)."""

    def __init__(self, wallet=None, leg1_visible=False):
        self.wallet, self.leg1_visible = wallet, leg1_visible

    def get(self, url, **kw):
        if "/wallet/transactionById" in url:
            return _Resp(200, self.wallet) if self.wallet is not None else _Resp(404)
        if "/utxo/withPool/byId/leg1box" in url:
            return _Resp(200 if self.leg1_visible else 404)
        raise AssertionError(url)


class _Explorer:
    def __init__(self, confirmed):
        self.confirmed = confirmed

    def get(self, url, **kw):
        assert url.endswith("/transactions/tx2")
        return _Resp(200 if self.confirmed else 404)


@pytest.mark.parametrize("node,explorer,expected", [
    (_Node(wallet={"numConfirmations": 1}), _Explorer(False), "confirmed"),
    (_Node(), _Explorer(True), "confirmed"),                       # wallet lagging, explorer has it
    (_Node(wallet={"numConfirmations": 0}), _Explorer(False), "pending"),  # leg 1 output hidden: spent in mempool
    (_Node(), _Explorer(False), "pending"),
    (_Node(leg1_visible=True), _Explorer(False), "dropped"),       # leg 1 output back: leg 2 left the mempool
])
def test_leg2_status_from_node(node, explorer, expected):
    from ergo.arb_runner import _leg2_status
    assert run(_leg2_status(node, "tx2", "leg1box", explorer)) == expected
