import os
import re
from pathlib import Path

from dotenv import load_dotenv

REPO_DIR = Path(__file__).resolve().parent
load_dotenv(REPO_DIR / ".env")


def repo_path(path) -> Path:
    """A relative path resolved against the bot's folder (not the directory it was started from)."""
    p = Path(path)
    return p if p.is_absolute() else REPO_DIR / p


SETTINGS: dict = {}          # every setting read from .env / the environment -> its default
PLACEHOLDERS: list[str] = []  # settings still holding a template value such as your_api_key_here (ignored)


def is_placeholder(value: str) -> bool:
    """A value copied unchanged from .env.example, e.g. your_api_key_here or .../webhooks/your_webhook_here."""
    return bool(re.search(r"(^|/)your_", value.strip(), re.IGNORECASE))


def setting(name: str, default=""):
    """The value of a setting; blank and placeholder values count as unset (the default applies)."""
    SETTINGS[name] = default
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    if is_placeholder(value):
        if name not in PLACEHOLDERS:
            PLACEHOLDERS.append(name)
        return default
    return value


# Ergo Node
ERGO_NODE_URL = setting("ERGO_NODE_URL", "http://127.0.0.1:9053")
ERGO_NODE_API_KEY = setting("ERGO_NODE_API_KEY", "")

# NonKYC Exchange
NONKYC_API_KEY = setting("NONKYC_API_KEY", "")
NONKYC_API_SECRET = setting("NONKYC_API_SECRET", "")
NONKYC_BASE_URL = "https://api.nonkyc.io/api/v2"

# Kucoin Exchange (future)
KUCOIN_API_KEY = setting("KUCOIN_API_KEY", "")
KUCOIN_API_SECRET = setting("KUCOIN_API_SECRET", "")
KUCOIN_API_PASSPHRASE = setting("KUCOIN_API_PASSPHRASE", "")

# Ergo explorer (public API)
ERGO_EXPLORER_API_URL = setting("ERGO_EXPLORER_API_URL", "https://api.ergoplatform.com/api/v1")

# ErgoDEX/Spectrum ERG/SigUSD N2T pool (read on-chain; Spectrum's API is sunset)
SPECTRUM_SIGUSD_POOL_NFT = "9916d75132593c8b07fe18bd8d583bda1652eed7565cf41a4738ddd90fc992ec"

# Token IDs (Ergo mainnet)
SIGUSD_TOKEN_ID = "03faf2cb329f2e90d6d23b58d91bbb6c046aa143261cc21f52fbe2824bfcbf04"
SIGRSV_TOKEN_ID = "003bd19d0187117f130b62e1bcab0939929ff5c7709f843c5c4dd158949285d0"
SIGMAUSD_BANK_NFT = "7d672d1def471720ca5782fd6473e47e796d9ac0c138d9911346f118b2f6d9d9"
SIGMAUSD_ORACLE_NFT = "011d3364de07e5a26f0c4eef0852cddb387039a921b7154ef3cab22c6eda887f"
# USE (Dexy USD). Override in .env when the token is migrated.
USE_TOKEN_ID = setting("USE_TOKEN_ID", "a55b8735ed1a99e46c2c89f8994aacdf4b1109bdcf682f1e5b34479c6e392669")

# Decimals
ERG_DECIMALS = 9
SIGUSD_DECIMALS = 2
USE_DECIMALS = 3

# Crux Finance (USE / DexyUSD)
CRUX_API_URL = "https://api.cruxfinance.io"
DEXY_USE_LP_NFT = setting("DEXY_USE_LP_NFT", "4ecaa1aac9846b1454563ae51746db95a3a40ee9f8c5f5301afbe348ae803d41")
CRUX_MINT_SERVICE_FEE = 0.79  # ERG, Crux /dexy/build_mint_tx service fee

# Service-fee addresses the TX guard lets a third-party-built TX pay (ErgoTrees).
# Crux Finance swap/mint fee, SigmaUSD UI fee. Extend via env (comma separated).
SERVICE_FEE_ERGO_TREES = {
    "0008cd03c50363c9ed382bce675ef307b387410511d1d06dfa64dfd6f2f1de95e5020b61",  # Crux Finance
    "0008cd02c5f61c83056a746a19a9e449e3c9596314cc417a2ef496b7567af558518f2bc7",  # SigmaUSD UI fee
} | {t.strip() for t in setting("EXTRA_SERVICE_FEE_ERGO_TREES", "").split(",") if t.strip()}

# Ergo network fees
ERGO_TX_FEE = 0.0011  # ERG
ERG_MIN_BOX_NANO = 1_000_000  # nanoERG kept in a change box that holds tokens

# SigmaUSD Bank fees
SIGMAUSD_PROTOCOL_FEE = 0.02  # 2% (stays in bank reserve)
SIGMAUSD_FRONTEND_FEE = 0.00229  # 0.229% UI fee on bc_delta (after protocol fee)
SIGMAUSD_REDEEM_EXTRA_ERG = 0.0021  # receipt box (0.001) + miner fee (0.0011)

# Spectrum DEX fees. The real fee is read from the pool box (R4 = 995 -> 0.5%);
# this constant is only a display/fallback value.
SPECTRUM_POOL_FEE = 0.005  # 0.5% (995/1000) for SigUSD/ERG pool
SPECTRUM_EXECUTION_FEE = 0.785  # ERG service fee (via Crux Finance routing)

# How SigUSD pool legs are executed: "direct" = our own pool-box swap (arb.py swap / ergo/pool_swap.py,
# miner fee only), "crux" = Crux /dex/swap (adds SPECTRUM_EXECUTION_FEE per leg).
POOL_SWAP_ROUTE = setting("POOL_SWAP_ROUTE", "direct").strip().lower()


def pool_service_fee() -> float:
    """Service fee per SigUSD pool leg for the configured route."""
    return SPECTRUM_EXECUTION_FEE if POOL_SWAP_ROUTE == "crux" else 0.0


def pool_fee_text() -> str:
    return f"-{SPECTRUM_EXECUTION_FEE} ERG Crux service fee" if POOL_SWAP_ROUTE == "crux" else "direct pool swap, no service fee"

# NonKYC fees (will be fetched dynamically, these are fallback defaults)
NONKYC_TRADING_FEE = 0.002  # 0.2% maker/taker (verify via API)
NONKYC_ERG_WITHDRAW_FEE = 3.1  # ERG, published at /api/v2/asset/getbyticker/ERG (2026-10-03); refreshed live

# Kucoin fees (will be fetched dynamically, these are fallback defaults)
KUCOIN_TRADING_FEE = 0.001  # 0.1% maker/taker
KUCOIN_ERG_WITHDRAW_FEE = 2.0  # ERG, published at /api/v1/currencies/ERG (2026-10-03, was 0.73); refreshed live

# Gate.io and MEXC: watch-only price sources (issue #40). Taker fees are refreshed live from their
# public market data; ERG withdrawal fees are not public there, so these defaults come from
# third-party fee listings (2026-10-03) and should be checked on the exchange's withdrawal page.
GATE_TRADING_FEE = float(setting("GATE_TRADING_FEE", "0.002"))
GATE_ERG_WITHDRAW_FEE = float(setting("GATE_ERG_WITHDRAW_FEE", "0.403"))
MEXC_TRADING_FEE = float(setting("MEXC_TRADING_FEE", "0.0008"))
MEXC_ERG_WITHDRAW_FEE = float(setting("MEXC_ERG_WITHDRAW_FEE", "0.1"))
# SafeTrade (safe.trade): off by default, Cloudflare often blocks scripted clients. Fees unknown until
# its API answers: the withdrawal fee is then read from it; until then no net spread is computed.
SAFETRADE_ENABLED = setting("SAFETRADE_ENABLED", "false").strip().lower() in ("1", "true", "yes")
SAFETRADE_TRADING_FEE = float(setting("SAFETRADE_TRADING_FEE", "0.002"))
_safetrade_withdraw = setting("SAFETRADE_ERG_WITHDRAW_FEE", None)
SAFETRADE_ERG_WITHDRAW_FEE = float(_safetrade_withdraw) if _safetrade_withdraw else None
# Moving the USDT back to the buying exchange after a cross-exchange round (e.g. a TRC-20 transfer)
CEX_USDT_TRANSFER_FEE = float(setting("CEX_USDT_TRANSFER_FEE", "1.0"))

# Venues. CEX paths (Kucoin/NonKYC) are off by default: on-chain only.
ENABLE_CEX = setting("ENABLE_CEX", "false").strip().lower() in ("1", "true", "yes")
# USE paths are off by default: the USE LP was drained (Oct 2026) and a token migration is expected.
ENABLE_USE = setting("ENABLE_USE", "false").strip().lower() in ("1", "true", "yes")
# Watch-only CEX prices (public endpoints, no API keys): show the gap to the on-chain pool
# and alert when it is worth connecting an exchange. Never trades.
CEX_WATCH = setting("CEX_WATCH", "true").strip().lower() in ("1", "true", "yes")
CEX_WATCH_ALERT_PERCENT = float(setting("CEX_WATCH_ALERT_PERCENT", "3.0"))
CEX_WATCH_COOLDOWN_SECONDS = int(setting("CEX_WATCH_COOLDOWN_SECONDS", "3600"))

# Arbitrage settings
MIN_PROFIT_PERCENT = float(setting("MIN_PROFIT_PERCENT", "0.5"))
MAX_TRADE_SIZE_ERG = float(setting("MAX_TRADE_SIZE_ERG", "100"))  # hard cap on anything executed


def parse_sizes(text: str) -> list[float]:
    """Comma-separated ERG sizes; empty means the default grid."""
    sizes = [float(x) for x in text.split(",") if x.strip()]
    sizes = sizes or [10, 100, 200, 500, 1000]
    return [int(x) if x == int(x) else x for x in sorted(set(sizes))]


# Trade sizes the scanner analyses (grid columns). Sizes above MAX_TRADE_SIZE_ERG are shown
# for analysis only and never executed.
TRADE_SIZES = parse_sizes(setting("TRADE_SIZES", ""))
# By default only sizes the wallet can fund are priced (plus the funded size itself); true keeps the
# whole grid, e.g. to compare against history.
TRADE_SIZES_UNFUNDED = setting("TRADE_SIZES_UNFUNDED", "false").strip().lower() in ("1", "true", "yes")
MIN_TRADE_SIZE_ERG = float(setting("MIN_TRADE_SIZE_ERG", "1"))  # lower bound of the best-size search
# Trade the smallest size that still earns this share of the best possible profit: near the
# peak, extra size adds little profit but all of its risk. 1.0 = maximise profit outright.
SIZE_PROFIT_CAPTURE = float(setting("SIZE_PROFIT_CAPTURE", "0.95"))
SLIPPAGE_TOLERANCE = float(setting("SLIPPAGE_TOLERANCE", "0.01"))  # 1%
MAX_FEE_BUDGET_ERG = float(setting("MAX_FEE_BUDGET_ERG", "1.0"))  # max service + miner fees per signed TX
SCAN_INTERVAL_SECONDS = int(setting("SCAN_INTERVAL_SECONDS", "15"))
CHAIN_POLL_SECONDS = float(setting("CHAIN_POLL_SECONDS", "2"))  # node poll for pool/bank/oracle changes

# Buffer for state changing between quote and inclusion, applied to legs whose
# price impact is computed from real reserves (AMM pool).
EXECUTION_BUFFER = float(setting("EXECUTION_BUFFER", "0.003"))  # 0.3%

# Slippage recommendations based on trade size (legacy, legs without known depth)
SLIPPAGE_TIERS = {
    10: 0.005,    # 0.5% for up to 10 ERG
    50: 0.01,     # 1% for up to 50 ERG
    100: 0.02,    # 2% for up to 100 ERG
    500: 0.03,    # 3% for up to 500 ERG
}


# Discord Notifications
DISCORD_WEBHOOK_URL = setting("DISCORD_WEBHOOK_URL", "")
DISCORD_USER_ID = setting("DISCORD_USER_ID", "")
DISCORD_ENABLED = bool(DISCORD_WEBHOOK_URL)
DISCORD_MIN_PROFIT_PERCENT = float(setting("DISCORD_MIN_PROFIT_PERCENT", "1.0"))
DISCORD_COOLDOWN_SECONDS = int(setting("DISCORD_COOLDOWN_SECONDS", "300"))
DISCORD_CONFIRM_SCANS = int(setting("DISCORD_CONFIRM_SCANS", "3"))
DISCORD_MIN_PROFIT_ERG = float(setting("DISCORD_MIN_PROFIT_ERG", "0.5"))
DISCORD_TIER1_PROFIT_PERCENT = float(setting("DISCORD_TIER1_PROFIT_PERCENT", "2.0"))
DISCORD_WALLET_COOLDOWN_SECONDS = int(setting("DISCORD_WALLET_COOLDOWN_SECONDS", "600"))
DISCORD_SUMMARY_INTERVAL_SECONDS = int(setting("DISCORD_SUMMARY_INTERVAL_SECONDS", "1800"))
DISCORD_CONFIRM_SECONDS = float(setting("DISCORD_CONFIRM_SECONDS", "10"))   # held this long before a message opens
DISCORD_CLOSE_SECONDS = float(setting("DISCORD_CLOSE_SECONDS", "10"))       # gone this long before it closes
DISCORD_EDIT_SECONDS = float(setting("DISCORD_EDIT_SECONDS", "30"))         # at most one edit per this
DISCORD_HEALTH_CHAIN_SECONDS = float(setting("DISCORD_HEALTH_CHAIN_SECONDS", "120"))
DISCORD_HEALTH_VENUE_SECONDS = float(setting("DISCORD_HEALTH_VENUE_SECONDS", "300"))
DISCORD_HEALTH_ORACLE_SECONDS = float(setting("DISCORD_HEALTH_ORACLE_SECONDS", "600"))
DISCORD_HEALTH_REPEAT_SECONDS = float(setting("DISCORD_HEALTH_REPEAT_SECONDS", "1800"))
DISCORD_HEALTH_LIVE_SECONDS = float(setting("DISCORD_HEALTH_LIVE_SECONDS", "600"))   # STOP file holding --live
DISCORD_DIGEST_HOUR = int(setting("DISCORD_DIGEST_HOUR", "8"))              # local hour, -1 = off
MINT_GATE_CONFIRM_POLLS = int(setting("MINT_GATE_CONFIRM_POLLS", "3"))       # agreeing polls before an alert
MINT_GATE_MIN_ROOM_ERG = float(setting("MINT_GATE_MIN_ROOM_ERG", str(MIN_TRADE_SIZE_ERG)))  # smaller = closed
MINT_GATE_PING_COOLDOWN_SECONDS = float(setting("MINT_GATE_PING_COOLDOWN_SECONDS", "3600"))
MINT_GATE_CLOSE_SECONDS = float(setting("MINT_GATE_CLOSE_SECONDS", "600"))  # shut this long before "closed"
PRICE_STALE_SECONDS = int(setting("PRICE_STALE_SECONDS", "60"))

# --live auto-execution (pool buy -> bank redeem). Trades only when every check passes.
RENAMED_SETTINGS = {"LIVE_CONFIRM_SCANS": "LIVE_CONFIRM_POLLS"}


def deprecated_settings() -> list[str]:
    """Warnings for settings in the environment that are no longer read."""
    return [f"{old} is no longer used; set {new} instead (counts ~{CHAIN_POLL_SECONDS:g} s chain polls)"
            for old, new in RENAMED_SETTINGS.items() if os.getenv(old) is not None]


LIVE_CONFIRM_POLLS = int(setting("LIVE_CONFIRM_POLLS", "2"))            # profitable on N chain polls in a row
LIVE_TRADE_COOLDOWN_SECONDS = int(setting("LIVE_TRADE_COOLDOWN_SECONDS", "300"))
LIVE_MAX_TRADES_PER_DAY = int(setting("LIVE_MAX_TRADES_PER_DAY", "10"))
LIVE_MAX_DRAWDOWN_ERG = float(setting("LIVE_MAX_DRAWDOWN_ERG", "5"))     # stop if wallet value falls this much
LIVE_ERG_RESERVE = float(setting("LIVE_ERG_RESERVE", "1"))               # ERG always kept in the wallet
LIVE_STOP_FILE = setting("LIVE_STOP_FILE", "STOP")                      # kill switch: no trades while it exists (relative = in the bot folder)
CHAIN_STALL_SECONDS = float(setting("CHAIN_STALL_SECONDS", "1200"))       # no new block for this long = node stalled
# Leg 2 (bank redeem) is watched until confirmed; if a bank/oracle box update drops it from the
# mempool it is rebuilt on fresh boxes and resubmitted (it spends leg 1's output, which stays ours).
LEG2_WATCH_TIMEOUT_SECONDS = int(setting("LEG2_WATCH_TIMEOUT_SECONDS", "1200"))
LEG2_WATCH_INTERVAL_SECONDS = int(setting("LEG2_WATCH_INTERVAL_SECONDS", "20"))
LEG2_MAX_REBUILDS = int(setting("LEG2_MAX_REBUILDS", "5"))

# Tracker: non-profitable scan rows older than this are deleted at startup
SCAN_RESULTS_RETENTION_DAYS = int(setting("SCAN_RESULTS_RETENTION_DAYS", "14"))


HARD_MAX_TRADE_ERG = 1000.0   # no setting can lift the trade cap above this


def live_config_errors() -> list[str]:
    """Settings that are unsafe for signing real trades; --live and arb.py --execute refuse to start on any."""
    rules = [
        (0 < SLIPPAGE_TOLERANCE <= 0.05,
         f"SLIPPAGE_TOLERANCE={SLIPPAGE_TOLERANCE:g}: must be above 0 and at most 0.05 (it sets leg 2's price floor)"),
        (MIN_PROFIT_PERCENT >= 0.1, f"MIN_PROFIT_PERCENT={MIN_PROFIT_PERCENT:g}: must be at least 0.1"),
        (0 < MAX_TRADE_SIZE_ERG <= HARD_MAX_TRADE_ERG,
         f"MAX_TRADE_SIZE_ERG={MAX_TRADE_SIZE_ERG:g}: must be above 0 and at most {HARD_MAX_TRADE_ERG:g}"),
        (LIVE_ERG_RESERVE >= 0.01, f"LIVE_ERG_RESERVE={LIVE_ERG_RESERVE:g}: must be at least 0.01"),
        (LIVE_CONFIRM_POLLS >= 2, f"LIVE_CONFIRM_POLLS={LIVE_CONFIRM_POLLS}: must be at least 2"),
        (LIVE_TRADE_COOLDOWN_SECONDS >= 30, f"LIVE_TRADE_COOLDOWN_SECONDS={LIVE_TRADE_COOLDOWN_SECONDS}: must be at least 30"),
        (0 < LIVE_MAX_DRAWDOWN_ERG <= MAX_TRADE_SIZE_ERG,
         f"LIVE_MAX_DRAWDOWN_ERG={LIVE_MAX_DRAWDOWN_ERG:g}: must be above 0 and at most MAX_TRADE_SIZE_ERG "
         f"({MAX_TRADE_SIZE_ERG:g})"),
        (0.002 <= MAX_FEE_BUDGET_ERG <= 2, f"MAX_FEE_BUDGET_ERG={MAX_FEE_BUDGET_ERG:g}: must be between 0.002 and 2"),
        (0 <= EXECUTION_BUFFER <= 0.05, f"EXECUTION_BUFFER={EXECUTION_BUFFER:g}: must be between 0 and 0.05"),
    ]
    return [message for ok, message in rules if not ok]


def get_recommended_slippage(trade_size_erg: float) -> float:
    for max_size, slippage in sorted(SLIPPAGE_TIERS.items()):
        if trade_size_erg <= max_size:
            return slippage
    return 0.05  # 5% for very large trades
