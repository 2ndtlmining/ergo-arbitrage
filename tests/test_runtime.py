"""Scanner lifecycle and node health (issue #17)."""
import asyncio

import pytest

from arbitrage.scanner import ArbitrageScanner
from exchanges.ergo_node import ErgoNodeClient


class TestGracefulShutdown:
    def test_stop_runs_cleanup(self, tmp_path, monkeypatch):
        s = ArbitrageScanner(db_path=str(tmp_path / "t.db"))
        calls = []

        async def connect_all():
            calls.append("connect")

        async def scan_once():
            calls.append("scan")
            s.request_stop()

        async def disconnect_all():
            calls.append("disconnect")
            s.tracker.close()

        monkeypatch.setattr(s, "connect_all", connect_all)
        monkeypatch.setattr(s, "scan_once", scan_once)
        monkeypatch.setattr(s, "disconnect_all", disconnect_all)
        monkeypatch.setattr(s.tracker, "print_summary", lambda: calls.append("summary"))

        asyncio.run(asyncio.wait_for(s.run(), timeout=5))
        assert calls == ["connect", "scan", "summary", "disconnect"]

    def test_cancellation_still_cleans_up(self, tmp_path, monkeypatch):
        s = ArbitrageScanner(db_path=str(tmp_path / "t.db"))
        calls = []

        async def noop():
            pass

        async def scan_once():
            raise asyncio.CancelledError  # what Ctrl+C delivers inside asyncio.run

        async def disconnect_all():
            calls.append("disconnect")
            s.tracker.close()

        monkeypatch.setattr(s, "connect_all", noop)
        monkeypatch.setattr(s, "scan_once", scan_once)
        monkeypatch.setattr(s, "disconnect_all", disconnect_all)
        monkeypatch.setattr(s.tracker, "print_summary", lambda: calls.append("summary"))

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(s.run())
        assert calls == ["summary", "disconnect"]


class TestNodeHealth:
    def _client(self, monkeypatch, info, status):
        c = ErgoNodeClient()

        async def fake_get(endpoint):
            return {"/info": info, "/wallet/status": status}[endpoint]

        monkeypatch.setattr(c, "_get", fake_get)
        return c

    def test_healthy(self, monkeypatch):
        c = self._client(monkeypatch, {"fullHeight": 100, "headersHeight": 101}, {"isUnlocked": True})
        h = asyncio.run(c.get_health())
        assert h["reachable"] and h["synced"] and h["unlocked"] and h["ok_to_trade"]

    def test_not_synced(self, monkeypatch):
        c = self._client(monkeypatch, {"fullHeight": 100, "headersHeight": 110}, {"isUnlocked": True})
        h = asyncio.run(c.get_health())
        assert not h["synced"] and not h["ok_to_trade"]

    def test_locked_wallet(self, monkeypatch):
        c = self._client(monkeypatch, {"fullHeight": 100, "headersHeight": 100}, {"isUnlocked": False})
        assert not asyncio.run(c.get_health())["ok_to_trade"]

    def test_unreachable(self, monkeypatch):
        c = self._client(monkeypatch, None, None)
        h = asyncio.run(c.get_health())
        assert not h["reachable"] and not h["ok_to_trade"]
        assert asyncio.run(c.check_connection()) is False


def test_a_failure_during_startup_still_closes_the_connections(tmp_path, monkeypatch):
    """The sessions opened by connect_all are closed even when startup fails or is interrupted."""
    s = ArbitrageScanner(db_path=str(tmp_path / "t.db"))
    calls = []

    async def connect_all():
        calls.append("connect")

    def broken_startup():
        raise RuntimeError("startup broke")

    async def disconnect_all():
        calls.append("disconnect")
        s.tracker.close()

    monkeypatch.setattr(s, "connect_all", connect_all)
    monkeypatch.setattr(s, "_log_startup", broken_startup)
    monkeypatch.setattr(s, "disconnect_all", disconnect_all)
    with pytest.raises(RuntimeError):
        asyncio.run(asyncio.wait_for(s.run(), timeout=5))
    assert calls == ["connect", "disconnect"]
