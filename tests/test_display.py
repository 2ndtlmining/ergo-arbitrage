"""Display fixes: size grid capped, wallet minimum message, on-chain-only shutdown."""
import asyncio

import pytest

import config
from arbitrage.scanner import ArbitrageScanner, trade_sizes_for
from logging_config import console


class TestTradeSizes:
    def test_capped_at_max_trade_size(self):
        assert trade_sizes_for(10) == [1, 5, 10]

    def test_cap_between_grid_points_is_included(self):
        assert trade_sizes_for(30) == [1, 5, 10, 25, 30]

    def test_default_cap_keeps_full_grid(self):
        assert trade_sizes_for(100) == [1, 5, 10, 25, 50, 100]

    def test_scanner_uses_config_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 10.0)
        s = ArbitrageScanner(db_path=str(tmp_path / "t.db"))
        try:
            assert s._trade_sizes == [1, 5, 10]
        finally:
            s.tracker.close()


@pytest.fixture
def scanner(tmp_path):
    s = ArbitrageScanner(db_path=str(tmp_path / "t.db"))
    yield s
    s.tracker.close()


class TestWalletMessage:
    def _render(self, scanner, wallet):
        with console.capture() as cap:
            scanner._display_wallet_opportunities(wallet, [], {"bank": {}})
        return cap.get()

    def test_small_balance_says_below_minimum(self, scanner):
        out = self._render(scanner, {"erg": 1.0, "sigusd": 0, "use": 0})
        assert "ERG: 1.0000 (below 2 ERG minimum for path analysis)" in out
        assert "SigUSD: No balance" in out

    def test_zero_balance_says_no_balance(self, scanner):
        assert "ERG: No balance" in self._render(scanner, {"erg": 0, "sigusd": 0, "use": 0})


class TestShutdown:
    def test_cex_not_disconnected_when_disabled(self, tmp_path, monkeypatch):
        s = ArbitrageScanner(db_path=str(tmp_path / "t.db"), enable_cex=False)
        called = []

        async def fake_disconnect():
            called.append("cex")

        monkeypatch.setattr(s.nonkyc, "disconnect", fake_disconnect)
        monkeypatch.setattr(s.kucoin, "disconnect", fake_disconnect)
        asyncio.run(s.disconnect_all())
        assert called == []
