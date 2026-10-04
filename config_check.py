"""`python arb.py config`: the settings the bot runs with, where each comes from, and mistakes in .env.

Secrets are masked. Lists keys in .env that the bot does not read (with the closest real name),
values still holding a .env.example placeholder (ignored) and renamed settings.
"""
import difflib
import os

from dotenv import dotenv_values

import config

# Every setting, grouped as in the README "Settings reference" (a test keeps this complete).
GROUPS = {
    "Node": ["ERGO_NODE_URL", "ERGO_NODE_API_KEY", "ERGO_EXPLORER_API_URL", "CHAIN_POLL_SECONDS",
             "CHAIN_STALL_SECONDS"],
    "Venues": ["ENABLE_CEX", "ENABLE_USE", "POOL_SWAP_ROUTE", "USE_TOKEN_ID", "DEXY_USE_LP_NFT",
               "EXTRA_SERVICE_FEE_ERGO_TREES"],
    "Sizing and profit": ["MIN_PROFIT_PERCENT", "MAX_TRADE_SIZE_ERG", "TRADE_SIZES", "TRADE_SIZES_UNFUNDED",
                          "MIN_TRADE_SIZE_ERG", "SIZE_PROFIT_CAPTURE", "SLIPPAGE_TOLERANCE", "EXECUTION_BUFFER",
                          "MAX_FEE_BUDGET_ERG", "SCAN_INTERVAL_SECONDS", "PRICE_STALE_SECONDS"],
    "Live": ["LIVE_CONFIRM_POLLS", "LIVE_TRADE_COOLDOWN_SECONDS", "LIVE_MAX_TRADES_PER_DAY", "LIVE_MAX_DRAWDOWN_ERG",
             "LIVE_ERG_RESERVE", "LIVE_MAX_PROFIT_PERCENT", "LIVE_MAX_ORACLE_DEVIATION_PERCENT", "LIVE_STOP_FILE", "LEG2_WATCH_TIMEOUT_SECONDS", "LEG2_WATCH_INTERVAL_SECONDS",
             "LEG2_MAX_REBUILDS"],
    "Discord": ["DISCORD_WEBHOOK_URL", "DISCORD_USER_ID", "DISCORD_MIN_PROFIT_PERCENT", "DISCORD_MIN_PROFIT_ERG",
                "DISCORD_TIER1_PROFIT_PERCENT", "DISCORD_CONFIRM_SECONDS", "DISCORD_CLOSE_SECONDS",
                "DISCORD_EDIT_SECONDS", "DISCORD_HEALTH_CHAIN_SECONDS", "DISCORD_HEALTH_VENUE_SECONDS",
                "DISCORD_HEALTH_ORACLE_SECONDS", "DISCORD_HEALTH_REPEAT_SECONDS", "DISCORD_HEALTH_LIVE_SECONDS", "DISCORD_DIGEST_HOUR",
                "DISCORD_CONFIRM_SCANS", "DISCORD_COOLDOWN_SECONDS", "DISCORD_WALLET_COOLDOWN_SECONDS",
                "DISCORD_SUMMARY_INTERVAL_SECONDS"],
    "Mint gate": ["MINT_GATE_CONFIRM_POLLS", "MINT_GATE_MIN_ROOM_ERG", "MINT_GATE_PING_COOLDOWN_SECONDS",
                  "MINT_GATE_CLOSE_SECONDS"],
    "CEX watch": ["CEX_WATCH", "CEX_WATCH_ALERT_PERCENT", "CEX_WATCH_COOLDOWN_SECONDS", "GATE_TRADING_FEE",
                  "GATE_ERG_WITHDRAW_FEE", "MEXC_TRADING_FEE", "MEXC_ERG_WITHDRAW_FEE", "SAFETRADE_ENABLED",
                  "SAFETRADE_TRADING_FEE", "SAFETRADE_ERG_WITHDRAW_FEE", "CEX_USDT_TRANSFER_FEE"],
    "Wallet tool": ["SEND_ALLOWED_ADDRESSES", "SEND_MAX_SIGUSD"],
    "Housekeeping": ["SCAN_RESULTS_RETENTION_DAYS"],
}

SECRETS = {"ERGO_NODE_API_KEY", "DISCORD_WEBHOOK_URL"}


def mask(value: str) -> str:
    return "****" + value[-4:] if len(value) >= 12 else "****"


def env_file_values() -> dict:
    path = config.REPO_DIR / ".env"
    return dict(dotenv_values(path)) if path.exists() else {}


def unknown_settings(file_values: dict | None = None) -> list[tuple[str, str | None]]:
    """Keys in .env the bot does not read, each with the closest real setting name (or None)."""
    file_values = env_file_values() if file_values is None else file_values
    known = set(config.SETTINGS) | set(config.RENAMED_SETTINGS)
    return [(key, (difflib.get_close_matches(key, config.SETTINGS, n=1, cutoff=0.75) or [None])[0])
            for key in file_values if key not in known]


def report(file_values: dict | None = None, environ=None) -> list[str]:
    file_values = env_file_values() if file_values is None else file_values
    environ = os.environ if environ is None else environ
    lines = [f"Settings from {config.REPO_DIR / '.env'} (environment variables win over .env)"]
    placeholders = []
    for group, names in GROUPS.items():
        lines.append(f"\n{group}")
        for name in names:
            raw = environ.get(name)
            if raw is None or not raw.strip() or config.is_placeholder(raw):
                if raw and config.is_placeholder(raw):
                    placeholders.append(name)
                default = config.SETTINGS.get(name)
                value, source = ("" if default is None else str(default)), "default"
            else:
                value = mask(raw) if name in SECRETS else raw
                source = ".env" if file_values.get(name) == raw else "environment"
            lines.append(f"  {name:<34} {value or '(not set)':<36} {source}")

    problems = []
    for key, close in unknown_settings(file_values):
        problems.append(f"  unknown   {key} is not a setting" + (f"; did you mean {close}?" if close else ""))
    for name in placeholders:
        problems.append(f"  ignored   {name} still holds the .env.example placeholder; set a real value or leave it blank")
    for old, new in config.RENAMED_SETTINGS.items():
        if old in environ or old in file_values:
            problems.append(f"  renamed   {old} is no longer read; use {new}")
    lines.append("")
    lines += ["Problems:"] + problems if problems else ["No unknown, placeholder or deprecated settings."]
    return lines
