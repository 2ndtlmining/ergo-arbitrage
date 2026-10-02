import sys
from pathlib import Path

import pytest

# Add project root to path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: E402

# Settings a local .env may override; tests run against the documented defaults
TEST_DEFAULTS = {
    "MAX_TRADE_SIZE_ERG": 100.0,
    "TRADE_SIZES": [1, 5, 10, 25, 50, 100],
    "MIN_TRADE_SIZE_ERG": 1.0,
    "MIN_PROFIT_PERCENT": 0.5,
    "SLIPPAGE_TOLERANCE": 0.01,
    "EXECUTION_BUFFER": 0.003,
    "MAX_FEE_BUDGET_ERG": 1.0,
    "ENABLE_CEX": False,
    "ENABLE_USE": False,
    "POOL_SWAP_ROUTE": "direct",
    "CEX_WATCH": False,
    "DISCORD_ENABLED": False,
    "LIVE_CONFIRM_POLLS": 2,
    "CHAIN_POLL_SECONDS": 2,
    "SCAN_INTERVAL_SECONDS": 15,
    "LIVE_TRADE_COOLDOWN_SECONDS": 300,
    "LIVE_MAX_TRADES_PER_DAY": 10,
    "LIVE_MAX_DRAWDOWN_ERG": 5.0,
    "LIVE_ERG_RESERVE": 1.0,
    "LEG2_WATCH_TIMEOUT_SECONDS": 1200,
    "LEG2_WATCH_INTERVAL_SECONDS": 20,
    "LEG2_MAX_REBUILDS": 5,
    "CEX_WATCH_ALERT_PERCENT": 3.0,
    "DISCORD_CONFIRM_SECONDS": 10,
    "DISCORD_CLOSE_SECONDS": 10,
    "DISCORD_EDIT_SECONDS": 30,
    "DISCORD_DIGEST_HOUR": 8,
    "DISCORD_MIN_PROFIT_PERCENT": 1.0,
    "DISCORD_MIN_PROFIT_ERG": 0.5,
    "DISCORD_TIER1_PROFIT_PERCENT": 2.0,
    "MINT_GATE_CONFIRM_POLLS": 3,
    "MINT_GATE_MIN_ROOM_ERG": 1.0,
    "MINT_GATE_PING_COOLDOWN_SECONDS": 3600,
}


@pytest.fixture(autouse=True)
def default_config(monkeypatch):
    for name, value in TEST_DEFAULTS.items():
        monkeypatch.setattr(config, name, value)


@pytest.fixture(autouse=True)
def fresh_box_id_cache():
    """ergo/chain_state.py caches confirmed box ids per NFT; tests start without it."""
    from ergo import chain_state
    chain_state._BOX_IDS.clear()
    yield
    chain_state._BOX_IDS.clear()
