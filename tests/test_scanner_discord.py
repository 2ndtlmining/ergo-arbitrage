"""Scanner -> Discord: one message per episode, health alerts, legacy flow only for CEX/USE (spec)."""
import asyncio

import pytest

import arbitrage.scanner as scanner_module
import config
from arbitrage.scanner import ArbitrageScanner
from ergo.chain_state import ChainSnapshot
from tests.test_bank_redeem_tx import BANK_BOX, ORACLE_BOX
from tests.test_chain_arb import pool_box
from tests.test_chain_scanner import HEALTHY, WALLET, Reader, snap


def losing():
    return ChainSnapshot(1_885_701, dict(pool_box(0.31, 2_000 * 10**9), boxId="pool-b"), BANK_BOX, ORACLE_BOX,
                         frozenset(), 5.0)


@pytest.fixture
def notify(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_ENABLED", True)
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/x")
    monkeypatch.setattr(config, "DISCORD_CONFIRM_SECONDS", 4)
    monkeypatch.setattr(config, "DISCORD_CLOSE_SECONDS", 4)
    monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 1000.0)
    s = ArbitrageScanner(mode="notify", db_path=str(tmp_path / "n.db"), view="dashboard")
    s.posts, s.edits = [], []
    monkeypatch.setattr(s.discord, "post", lambda embed, content="", on_id=None:
                        (s.posts.append((embed, content)), on_id and on_id(f"m{len(s.posts)}")))
    monkeypatch.setattr(s.discord, "edit", lambda get_id, embed: s.edits.append((get_id(), embed)))

    async def healthy():
        return dict(HEALTHY)

    async def wallet():
        return dict(WALLET)

    async def no_text(content):  # the legacy text sender (e.g. the 30 min summary) must not hit the network
        return True

    monkeypatch.setattr(s.ergo_node, "get_health", healthy)
    monkeypatch.setattr(s, "_fetch_wallet_balances", wallet)
    monkeypatch.setattr(s.discord, "_send", no_text)
    yield s
    s.tracker.close()


def run(coro):
    return asyncio.run(coro)


def episode_posts(s):
    return [(e, c) for e, c in s.posts if e["title"].startswith("OPEN")]


def test_one_message_per_episode_then_a_close_edit(notify, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), snap(), snap(), snap(), snap(),
                                                                losing(), losing(), losing(), losing()))
    for t in (0, 2, 4, 6, 8):
        run(notify.poll_once(t))
    assert len(episode_posts(notify)) == 1
    for t in (10, 12, 14, 16):
        run(notify.poll_once(t))
    closes = [(mid, e) for mid, e in notify.edits if e["title"].startswith("Closed")]
    opened_at = next(i for i, (e, _) in enumerate(notify.posts) if e["title"].startswith("OPEN"))
    assert len(closes) == 1 and closes[0][0] == f"m{opened_at + 1}"  # the edit targets the episode's message
    (row,) = notify.tracker.chain_episodes_since("2000-01-01")
    assert row["closed_at"] is not None


def test_tier1_open_pings(notify, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_USER_ID", "42")
    notify.discord.user_id = "42"
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    for t in (0, 2, 4):
        run(notify.poll_once(t))
    (embed, content), = episode_posts(notify)
    assert content == "<@42>"  # +3.36% >= DISCORD_TIER1_PROFIT_PERCENT 2.0


def test_chain_outage_alerts_with_ping(notify, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_HEALTH_CHAIN_SECONDS", 0)
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), RuntimeError("node down")))
    notify.health.chain_s = 0
    run(notify.poll_once(0))
    run(notify.poll_once(2))
    alerts = [e for e, c in notify.posts if e["title"].startswith("⚠️")]
    assert alerts and "node down" in alerts[0]["title"]


def test_legacy_flow_skips_on_chain_paths(notify, monkeypatch):
    sent = []

    async def legacy(opps, scan_number=0):
        sent.extend(o.path_key for o in opps)
        return len(opps)

    monkeypatch.setattr(notify.discord, "notify_opportunities", legacy)
    monkeypatch.setattr(config, "DISCORD_CONFIRM_SCANS", 1)
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    run(notify.poll_once(0))
    assert not any(k in scanner_module.LIVE_PATHS for k in sent)


def test_monitor_mode_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_ENABLED", True)
    s = ArbitrageScanner(mode="monitor", db_path=str(tmp_path / "m.db"), view="dashboard")
    posts = []
    monkeypatch.setattr(s.discord, "post", lambda *a, **k: posts.append(a))
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    for t in (0, 2, 4, 6, 8, 10, 12):
        run(s.poll_once(t))
    assert posts == []
    s.tracker.close()
