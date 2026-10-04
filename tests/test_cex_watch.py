"""Watch-only CEX prices: show the gap to the on-chain pool and alert, never trade."""
import asyncio

import pytest

from datetime import datetime
from types import SimpleNamespace

import config
from arbitrage.scanner import ArbitrageScanner
from logging_config import console
from notifications.discord import DiscordNotifier


def PriceQuote(exchange, pair, bid, ask, timestamp=None):
    """Stand-in for a CEX quote (the scanner only reads bid, ask and timestamp)."""
    return SimpleNamespace(exchange=exchange, pair=pair, bid=bid, ask=ask, timestamp=timestamp or datetime.now())


@pytest.fixture
def scanner(tmp_path):
    s = ArbitrageScanner(db_path=str(tmp_path / "t.db"), cex_watch=True)
    yield s
    s.tracker.close()


def prices(kucoin_bid, kucoin_ask, pool_spot=0.31):
    return {
        "spectrum_erg_sigusd": pool_spot,
        "bank": {},
        "cex_watch": {"Kucoin": PriceQuote(exchange="Kucoin", pair="ERG/USDT", bid=kucoin_bid, ask=kucoin_ask)},
    }


class TestGaps:
    def test_erg_cheaper_on_cex(self, scanner):
        (gap,) = scanner._cex_watch_gaps(prices(0.299, 0.300))
        assert gap["gap_percent"] == pytest.approx((0.31 - 0.30) / 0.30 * 100)
        assert gap["alert"] and "cheaper on Kucoin" in gap["text"]

    def test_erg_dearer_on_cex(self, scanner):
        (gap,) = scanner._cex_watch_gaps(prices(0.33, 0.331))
        assert gap["gap_percent"] == pytest.approx((0.33 - 0.31) / 0.31 * 100)
        assert gap["alert"] and "dearer on Kucoin" in gap["text"]

    def test_small_gap_no_alert(self, scanner):
        (gap,) = scanner._cex_watch_gaps(prices(0.309, 0.311))
        assert not gap["alert"]

    def test_text_flags_watch_only_and_withdrawal_fee(self, scanner):
        (gap,) = scanner._cex_watch_gaps(prices(0.299, 0.300))
        assert "watch-only" in gap["text"] and "withdrawal" in gap["text"]


class TestNoTrading:
    def test_watch_prices_never_create_paths(self, scanner):
        p = prices(0.20, 0.21)  # huge gap
        assert not [o for o in scanner._find_opportunities(p) if "Kucoin" in o.path or "NonKYC" in o.path]

    def test_display_shows_watch_line(self, scanner):
        with console.capture() as cap:
            scanner._display_cex_watch(prices(0.299, 0.300))
        assert "WATCH Kucoin" in cap.get()


class TestDiscordWatch:
    def test_alert_respects_its_own_cooldown(self, monkeypatch):
        d = DiscordNotifier()
        d.enabled = True
        sent = []

        async def fake_send(content):
            sent.append(content)
            return True

        monkeypatch.setattr(d, "_send", fake_send)
        async def go():
            await d.notify_watch("Kucoin", "gap text")
            await d.notify_watch("Kucoin", "gap text")
            await d.stop()  # texts are queued; deliver them

        asyncio.run(go())
        assert len(sent) == 1
        assert "watch-only" in sent[0].lower()


def test_default_follows_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CEX_WATCH", True)
    s = ArbitrageScanner(db_path=str(tmp_path / "t.db"))
    try:
        assert s.cex_watch is True
    finally:
        s.tracker.close()
