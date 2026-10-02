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
    "CEX_WATCH_ALERT_PERCENT": 3.0,
}


@pytest.fixture(autouse=True)
def default_config(monkeypatch):
    for name, value in TEST_DEFAULTS.items():
        monkeypatch.setattr(config, name, value)
