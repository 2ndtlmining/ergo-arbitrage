"""Fixes from the dashboard branch review (fail-safe live view, clean output, safe overrides and logging)."""
import asyncio
import copy
import logging

import pytest
from rich.console import Console

import arbitrage.scanner as scanner_module
import config
from tests.test_scanner_views import make  # noqa: F401  (fixture)
from tests.test_chain_scanner import HEALTHY, KEY, Reader, snap


# 1. Node health blockers reach the Live panel (and the log)

def test_unhealthy_node_shows_as_blocked_not_armed(make, monkeypatch):
    s = make("dashboard", mode="live")

    async def locked():
        return dict(HEALTHY, unlocked=False, ok_to_trade=False)

    monkeypatch.setattr(s.ergo_node, "get_health", locked)
    for t in (0, 2, 4):
        asyncio.run(s.poll_once(t))
    assert s.state.live == "blocked"
    assert any("node not ready" in d for d in s.state.live_detail)


def test_not_trading_reasons_reach_the_log_file_in_quiet_views(make, monkeypatch, caplog, tmp_path):
    open(config.LIVE_STOP_FILE, "w").close()
    s = make("dashboard", mode="live")
    with caplog.at_level(logging.INFO, logger="ergo_arb"):
        for t in (0, 2, 4, 6):
            asyncio.run(s.poll_once(t))
    blocked = [r.getMessage() for r in caplog.records if "not trading" in r.getMessage()]
    assert blocked and any("STOP file" in m for m in blocked)
    assert len(blocked) <= 4  # logged when the reasons change, not on every poll


# 2. Every live blocker is visible, global ones first

def test_live_panel_shows_every_blocker():
    from arbitrage.dashboard_state import DashboardState
    from arbitrage.dashboard_view import render
    st = DashboardState("live")
    st.set_live("blocked", ["STOP file present (STOP)", "cooldown 120s", "max 10 trades per day reached",
                            "profitable 1/2 polls in a row"])
    c = Console(record=True, width=160, height=50, color_system=None)
    c.print(render(st))
    out = c.export_text()
    for reason in ("STOP file present", "cooldown 120s", "max 10 trades", "profitable 1/2 polls"):
        assert reason in out, reason


def test_global_blockers_are_listed_before_path_blockers(make):
    open(config.LIVE_STOP_FILE, "w").close()
    s = make("dashboard", mode="live")
    asyncio.run(s.poll_once(0))
    assert s.state.live_detail[0].startswith("STOP file present")


# 3. The TX guard summary goes through the caller's log, not print()

def test_guarded_sign_reports_through_log(capsys):
    from ergo.signing import DryRun, guarded_sign
    from tests.test_signing import FIXTURE, NODE, POLICY, FakeNode
    lines = []
    with pytest.raises(DryRun):
        asyncio.run(guarded_sign(FakeNode(FIXTURE["input_boxes"]), NODE, copy.deepcopy(FIXTURE["unsigned_tx"]), POLICY, execute=False,
                                 log=lines.append))
    assert any("TX guard OK" in m for m in lines)
    assert capsys.readouterr().out == ""


# 4. Trade log lines with stray markup never break the trade path

@pytest.mark.parametrize("view", ["plain", "dashboard"])
def test_trade_log_survives_markup_in_node_errors(make, view, caplog):
    s = make(view, mode="live")
    message = "  Leg 2 FAILED: submit failed: HTTP 400 {\"detail\": \"[/detail] bad box\"}"
    with caplog.at_level(logging.INFO, logger="ergo_arb"):
        s._trade_log(message)
    assert any("[/detail] bad box" in r.getMessage() for r in caplog.records)
    assert any("[/detail] bad box" in t for _, _, t in s.state.events)


# 5. Overrides: 0 and negatives are rejected, not ignored

@pytest.mark.parametrize("flag", ["--max-trade-erg", "--interval"])
@pytest.mark.parametrize("value", ["0", "-5"])
def test_non_positive_overrides_are_rejected(flag, value):
    from main import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args([flag, value])


def test_small_positive_trade_cap_is_applied():
    from main import apply_overrides, build_parser
    apply_overrides(build_parser().parse_args(["--max-trade-erg", "0.5"]))
    assert config.MAX_TRADE_SIZE_ERG == 0.5


# 6. A failed log rotation (file locked by another process / OneDrive) keeps logging

def test_failed_rotation_keeps_writing(tmp_path, monkeypatch):
    from logging_config import SafeTimedRotatingFileHandler
    path = tmp_path / "arbitrage.log"
    h = SafeTimedRotatingFileHandler(str(path), when="midnight", backupCount=14, encoding="utf-8")
    try:
        def locked(src, dst):
            raise PermissionError("file in use")

        monkeypatch.setattr(h, "rotate", locked)
        h.rolloverAt = 0  # rollover due now
        logger = logging.getLogger("ergo_arb.test_rotation")
        logger.addHandler(h)
        logger.warning("first")
        logger.warning("second")
        logger.removeHandler(h)
        h.flush()
        assert h.rolloverAt > 0
        text = path.read_text(encoding="utf-8")
        assert "first" in text and "second" in text
    finally:
        h.close()
