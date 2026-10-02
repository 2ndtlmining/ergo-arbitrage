"""main.py flags and logging setup (spec: dashboard)."""
import logging
import logging.handlers

import pytest

import config
from main import apply_overrides, build_parser, view_of


def test_default_view_is_the_dashboard():
    assert view_of(build_parser().parse_args([])) == "dashboard"
    assert view_of(build_parser().parse_args(["--plain"])) == "plain"
    assert view_of(build_parser().parse_args(["--json", "--live"])) == "json"


def test_plain_and_json_are_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--plain", "--json"])


def test_flags_parse():
    a = build_parser().parse_args(["--once", "--interval", "30", "--max-trade-erg", "5", "--log-level", "debug",
                                   "--db", "x.db", "--no-wallet"])
    assert a.once and a.interval == 30 and a.max_trade_erg == 5.0 and a.log_level == "debug"
    assert a.db == "x.db" and a.no_wallet


def test_overrides_apply_to_config():
    apply_overrides(build_parser().parse_args(["--interval", "30", "--max-trade-erg", "5"]))
    assert config.SCAN_INTERVAL_SECONDS == 30 and config.MAX_TRADE_SIZE_ERG == 5.0


def test_no_overrides_leave_config_alone():
    before = (config.SCAN_INTERVAL_SECONDS, config.MAX_TRADE_SIZE_ERG)
    apply_overrides(build_parser().parse_args([]))
    assert (config.SCAN_INTERVAL_SECONDS, config.MAX_TRADE_SIZE_ERG) == before


def test_log_file_rotates_daily_and_keeps_two_weeks(tmp_path):
    from logging_config import setup_logging
    logger = setup_logging("info", console_handler=False, log_path=str(tmp_path / "arbitrage.log"))
    files = [h for h in logger.handlers if isinstance(h, logging.handlers.TimedRotatingFileHandler)]
    assert len(files) == 1 and files[0].backupCount == 14 and files[0].when == "MIDNIGHT"
    assert files[0].level == logging.INFO
    assert not any(type(h).__name__ == "RichHandler" for h in logger.handlers)
    setup_logging("info", console_handler=False, log_path=str(tmp_path / "arbitrage.log"))
    assert len(logging.getLogger("ergo_arb").handlers) == 1  # no duplicates on a second call
    for h in list(logger.handlers):
        h.close()
        logger.removeHandler(h)


def test_event_handler_puts_warnings_in_the_dashboard():
    from arbitrage.dashboard_state import DashboardState
    from logging_config import EventLogHandler
    state = DashboardState()
    logger = logging.getLogger("ergo_arb.test_events")
    handler = EventLogHandler(state)
    logger.addHandler(handler)
    try:
        logger.info("quiet")
        logger.warning("Chain state unavailable: timeout")
    finally:
        logger.removeHandler(handler)
    assert [(lvl, t) for _, lvl, t in state.events] == [("warn", "Chain state unavailable: timeout")]


def test_output_is_forced_to_utf8():
    """Piped output on Windows defaults to cp1252, which cannot encode the dashboard's symbols (●, ✓)."""
    import io
    from main import ensure_utf8
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    ensure_utf8(stream)
    assert stream.encoding.lower() == "utf-8"
    stream.write("● ✓")
    stream.flush()
