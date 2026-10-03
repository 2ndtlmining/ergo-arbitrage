"""Display fixes: size grid capped, wallet minimum message, on-chain-only shutdown."""
import asyncio

import pytest

import config
from arbitrage.scanner import ArbitrageScanner
from logging_config import console


class TestTradeSizes:
    def test_parse_sizes(self):
        assert config.parse_sizes("10,100, 200,500,1000") == [10, 100, 200, 500, 1000]
        assert config.parse_sizes("") == [10, 100, 200, 500, 1000]  # documented default

    def test_grid_not_capped_by_max_trade_size(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "TRADE_SIZES", [10, 100, 1000])
        monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 10.0)
        s = ArbitrageScanner(db_path=str(tmp_path / "t.db"))
        try:
            assert s._trade_sizes == [10, 100, 1000]
        finally:
            s.tracker.close()

    def test_sizes_above_cap_marked_analysis_only(self, tmp_path, monkeypatch):
        from tests.test_scanner_paths import ORACLE_R4, make_prices
        from exchanges.sigmausd import BankState
        monkeypatch.setattr(config, "TRADE_SIZES", [10, 100])
        monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 10.0)
        monkeypatch.setattr(config, "TRADE_SIZES_UNFUNDED", True)   # unfunded sizes are only priced on request
        s = ArbitrageScanner(db_path=str(tmp_path / "t.db"))
        try:
            state = BankState(bank_erg_nano=3_000_000 * 10**9, sigusd_circ_cents=10_000_000, oracle_r4=ORACLE_R4)
            prices = make_prices(state, 0.31)
            with console.capture() as cap:
                s._display_opportunities(s._find_opportunities(prices), prices)
            out = cap.get()
            assert "100*" in out and "10*" not in out
            assert "above MAX_TRADE_SIZE_ERG" in out
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
