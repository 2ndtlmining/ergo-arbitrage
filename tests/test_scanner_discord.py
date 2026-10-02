"""Scanner -> Discord: one message per episode, health alerts, legacy flow only for CEX/USE (spec)."""
import asyncio
from types import SimpleNamespace

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


def test_poll_does_not_wait_for_a_hanging_discord(notify, monkeypatch):
    """Spec: nothing in the poll path awaits Discord (legacy texts included)."""
    async def hanging_send(content):
        await asyncio.Event().wait()

    monkeypatch.setattr(notify.discord, "_send", hanging_send)
    notify._last_summary_time = 0.0  # the 30 min text summary is due on this full scan
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    run(asyncio.wait_for(notify.poll_once(0), 5))


def test_discord_tick_runs_even_when_the_poll_fails(notify, monkeypatch):
    ticks = []

    async def boom(now):
        raise RuntimeError("poll failed")

    monkeypatch.setattr(notify, "_poll", boom)
    monkeypatch.setattr(notify, "_discord_tick", ticks.append)
    with pytest.raises(RuntimeError):
        run(notify.poll_once(0))
    assert ticks == [0]


def test_digest_error_does_not_block_trading(notify, monkeypatch):
    traded = []

    def broken(*a, **k):
        raise RuntimeError("meta table locked")

    async def execute(*a, **k):
        traded.append(True)

    monkeypatch.setattr(scanner_module, "digest_due", broken)
    monkeypatch.setattr(notify, "_execute_trades", execute)
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    run(notify.poll_once(0))
    assert traded


def test_shutdown_survives_a_failing_episode_close(notify, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    for t in (0, 2, 4):
        run(notify.poll_once(t))
    assert notify.episodes.open

    def locked(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(notify.tracker, "close_chain_episode", locked)
    notify._close_episodes_on_shutdown()  # must not raise


def test_message_id_is_saved_with_the_episode(notify, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    for t in (0, 2, 4):
        run(notify.poll_once(t))
    (row,) = notify.tracker.conn.execute("SELECT message_id FROM chain_episodes").fetchall()
    opened = next(i for i, (e, _) in enumerate(notify.posts) if e["title"].startswith("OPEN"))
    assert row["message_id"] == f"m{opened + 1}"


def test_restart_closes_the_stale_discord_message(notify):
    stale = [
        {"path": "pool→redeem", "message_id": "m9", "peak_profit_percent": 3.5, "peak_profit_erg": 1.5,
         "opened_at": "2026-10-03T08:00:00", "closed_at": "2026-10-03T08:06:40"},
        {"path": "pool→redeem", "message_id": None, "peak_profit_percent": 1.0, "peak_profit_erg": 0.6,
         "opened_at": "2026-10-03T07:00:00", "closed_at": "2026-10-03T07:00:00"},
    ]
    notify.tracker.claim_stale_chain_episodes = lambda: stale
    notify._close_stale_discord_messages()
    assert [(mid, e["title"].split(" · ")[0]) for mid, e in notify.edits] == [("m9", "Closed")]


from tests.test_bank_redeem_tx import BANK_BOX as _BANK


def open_snap():
    """Same as snap(), with a bank reserve that puts RR at ~423% (mint open, large room)."""
    s = snap()
    return ChainSnapshot(s.height, s.pool, dict(_BANK, value=2_100_000 * 10**9), s.oracle, s.pending, s.read_ms)


def mint_posts(s):
    return [(e, c) for e, c in s.posts if e["title"].startswith("Bank mint")]


def test_mint_open_posts_once_with_a_ping_then_close_edits_it(notify, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_USER_ID", "42")
    notify.mint_gate.close_s = 0  # close on the 3 confirming polls (the 10 min hold is tested in test_mint_gate)
    notify.discord.user_id = "42"
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(open_snap(), open_snap(), open_snap(), open_snap(),
                                                                snap(), snap(), snap()))
    for t in (0, 2, 4, 6):
        run(notify.poll_once(t))
    ((embed, content),) = mint_posts(notify)
    assert embed["title"].startswith("Bank mint OPEN") and content == "<@42>"
    for t in (8, 10, 12):
        run(notify.poll_once(t))
    closes = [(mid, e) for mid, e in notify.edits if e["title"].startswith("Bank mint closed")]
    posted_at = next(i for i, (e, _) in enumerate(notify.posts) if e["title"].startswith("Bank mint OPEN"))
    assert len(closes) == 1 and closes[0][0] == f"m{posted_at + 1}"
    assert any("Bank mint OPEN" in text for _, _, text in notify.state.events)


def test_dashboard_shows_the_mint_forecast_in_every_mode(tmp_path, monkeypatch):
    s = ArbitrageScanner(mode="monitor", db_path=str(tmp_path / "m.db"), view="dashboard")
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    run(s.poll_once(0))
    assert s.state.mint_text.startswith("✗ needs ERG $")
    s.tracker.close()


def test_chain_error_does_not_drive_the_mint_gate(notify, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(open_snap(), RuntimeError("node down")))
    run(notify.poll_once(0))
    for t in (2, 4, 6, 8):
        run(notify.poll_once(t))
    assert mint_posts(notify) == []      # one good read, then errors: never 3 agreeing readings


def open_the_gate(notify, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(open_snap()))
    for t in (0, 2, 4):
        run(notify.poll_once(t))
    assert notify.mint_gate.is_open


def test_shutdown_marks_an_open_mint_message_as_stopped(notify, monkeypatch):
    open_the_gate(notify, monkeypatch)
    notify._close_episodes_on_shutdown()
    stopped = [(mid, e) for mid, e in notify.edits if e["title"] == "Bank mint · bot stopped"]
    posted_at = next(i for i, (e, _) in enumerate(notify.posts) if e["title"].startswith("Bank mint OPEN"))
    assert stopped and stopped[0][0] == f"m{posted_at + 1}"


def test_open_mint_message_survives_a_crash(notify, monkeypatch):
    open_the_gate(notify, monkeypatch)
    mid = notify.tracker.get_meta("mint_gate_message")
    assert mid and mid.startswith("m")
    notify.edits.clear()
    notify._close_stale_discord_messages()                 # what the next start does
    assert [(m, e["title"]) for m, e in notify.edits if e["title"].startswith("Bank mint")] == [
        (mid, "Bank mint · bot stopped")]
    assert not notify.tracker.get_meta("mint_gate_message")



def test_one_failing_discord_check_does_not_skip_the_others(notify, monkeypatch):
    seen = []

    def broken(*a, **k):
        raise RuntimeError("episodes broke")

    monkeypatch.setattr(notify.episodes, "update", broken)
    monkeypatch.setattr(notify.health, "update", lambda now, state: seen.append("health") or [])
    monkeypatch.setattr(notify.mint_gate, "update", lambda now, bank: seen.append("mint"))
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    run(notify.poll_once(0))
    assert seen == ["health", "mint"]


def test_open_episodes_get_a_heartbeat(notify, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    for t in (0, 2, 4):
        run(notify.poll_once(t))
    (row,) = notify.tracker.chain_episodes_since("2000-01-01")
    assert row["last_seen_at"] is None                     # no edit yet (profit unchanged)
    run(notify.poll_once(70))
    (row,) = notify.tracker.chain_episodes_since("2000-01-01")
    assert row["last_seen_at"] is not None


def test_digest_during_a_chain_outage_has_no_stale_mint_line(notify, monkeypatch):
    got = []
    monkeypatch.setattr(scanner_module, "digest_due", lambda *a: True)
    monkeypatch.setattr(scanner_module, "build_digest",
                        lambda *a, **k: got.append(k.get("bank")) or SimpleNamespace(
                            hours=24, paths={}, potential_erg=0.0, trades={"count": 0, "net_erg": 0.0, "failed": 0},
                            outages=[], outage_since="", wallet=None, mint=None))
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), RuntimeError("node down")))
    run(notify.poll_once(0))
    run(notify.poll_once(20))
    assert got[0] is not None and got[-1] is None


def digest_posts(s):
    return [e for e, _ in s.posts if e["title"].startswith("Daily digest")]


def test_digest_is_marked_sent_only_once_delivered(notify, monkeypatch):
    """Review: a digest queued while Discord is down must be retried, not silently lost."""
    undelivered = []
    monkeypatch.setattr(notify.discord, "post", lambda embed, content="", on_id=None: undelivered.append(
        (embed, on_id)))
    monkeypatch.setattr(scanner_module, "digest_due", lambda tracker, now, hour: tracker.get_meta("digest_date") is None)
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    run(notify.poll_once(0))
    run(notify.poll_once(20))                                  # still in flight: not queued twice
    digests = [(e, cb) for e, cb in undelivered if e["title"].startswith("Daily digest")]
    assert len(digests) == 1 and notify.tracker.get_meta("digest_date") is None
    notify._digest_queued_at -= 601                            # the post never came back
    run(notify.poll_once(40))
    digests = [(e, cb) for e, cb in undelivered if e["title"].startswith("Daily digest")]
    assert len(digests) == 2
    digests[-1][1]("m9")                                       # Discord returned the message id
    assert notify.tracker.get_meta("digest_date") is not None


def test_once_runs_leave_other_processes_messages_alone(notify, monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "_close_stale_discord_messages", lambda: calls.append("stale"))
    monkeypatch.setattr(notify, "connect_all", lambda: asyncio.sleep(0))
    monkeypatch.setattr(notify, "disconnect_all", lambda: asyncio.sleep(0))
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    run(notify.run(once=True))
    assert calls == []
