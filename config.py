import os
from dotenv import load_dotenv

load_dotenv()


# Ergo Node
ERGO_NODE_URL = os.getenv("ERGO_NODE_URL", "http://127.0.0.1:9053")
ERGO_NODE_API_KEY = os.getenv("ERGO_NODE_API_KEY", "")

# NonKYC Exchange
NONKYC_API_KEY = os.getenv("NONKYC_API_KEY", "")
NONKYC_API_SECRET = os.getenv("NONKYC_API_SECRET", "")
NONKYC_BASE_URL = "https://api.nonkyc.io/api/v2"

# Kucoin Exchange (future)
KUCOIN_API_KEY = os.getenv("KUCOIN_API_KEY", "")
KUCOIN_API_SECRET = os.getenv("KUCOIN_API_SECRET", "")
KUCOIN_API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE", "")

# Ergo explorer (public API)
ERGO_EXPLORER_API_URL = os.getenv("ERGO_EXPLORER_API_URL", "https://api.ergoplatform.com/api/v1")

# ErgoDEX/Spectrum ERG/SigUSD N2T pool (read on-chain; Spectrum's API is sunset)
SPECTRUM_SIGUSD_POOL_NFT = "9916d75132593c8b07fe18bd8d583bda1652eed7565cf41a4738ddd90fc992ec"

# Token IDs (Ergo mainnet)
ERG_TOKEN_ID = "0000000000000000000000000000000000000000000000000000000000000000"
SIGUSD_TOKEN_ID = "03faf2cb329f2e90d6d23b58d91bbb6c046aa143261cc21f52fbe2824bfcbf04"
SIGRSV_TOKEN_ID = "003bd19d0187117f130b62e1bcab0939929ff5c7709f843c5c4dd158949285d0"
SIGMAUSD_BANK_NFT = "7d672d1def471720ca5782fd6473e47e796d9ac0c138d9911346f118b2f6d9d9"
SIGMAUSD_ORACLE_NFT = "011d3364de07e5a26f0c4eef0852cddb387039a921b7154ef3cab22c6eda887f"
# USE (Dexy USD). Override in .env when the token is migrated.
USE_TOKEN_ID = os.getenv("USE_TOKEN_ID", "a55b8735ed1a99e46c2c89f8994aacdf4b1109bdcf682f1e5b34479c6e392669")

# Decimals
ERG_DECIMALS = 9
SIGUSD_DECIMALS = 2
SIGRSV_DECIMALS = 0
USE_DECIMALS = 3

# Crux Finance (USE / DexyUSD)
CRUX_API_URL = "https://api.cruxfinance.io"
DEXY_USE_LP_NFT = os.getenv("DEXY_USE_LP_NFT", "4ecaa1aac9846b1454563ae51746db95a3a40ee9f8c5f5301afbe348ae803d41")
CRUX_MINT_SERVICE_FEE = 0.79  # ERG, Crux /dexy/build_mint_tx service fee

# Service-fee addresses the TX guard lets a third-party-built TX pay (ErgoTrees).
# Crux Finance swap/mint fee, SigmaUSD UI fee. Extend via env (comma separated).
SERVICE_FEE_ERGO_TREES = {
    "0008cd03c50363c9ed382bce675ef307b387410511d1d06dfa64dfd6f2f1de95e5020b61",  # Crux Finance
    "0008cd02c5f61c83056a746a19a9e449e3c9596314cc417a2ef496b7567af558518f2bc7",  # SigmaUSD UI fee
} | {t.strip() for t in os.getenv("EXTRA_SERVICE_FEE_ERGO_TREES", "").split(",") if t.strip()}

# Ergo network fees
ERGO_TX_FEE = 0.0011  # ERG
ERGO_MIN_BOX_VALUE = 0.001  # ERG
ERG_MIN_BOX_NANO = 1_000_000  # nanoERG kept in a change box that holds tokens

# SigmaUSD Bank fees
SIGMAUSD_PROTOCOL_FEE = 0.02  # 2% (stays in bank reserve)
SIGMAUSD_FRONTEND_FEE = 0.00229  # 0.229% UI fee on bc_delta (after protocol fee)
SIGMAUSD_TOTAL_FEE = SIGMAUSD_PROTOCOL_FEE + SIGMAUSD_FRONTEND_FEE
SIGMAUSD_REDEEM_EXTRA_ERG = 0.0021  # receipt box (0.001) + miner fee (0.0011)

# Spectrum DEX fees. The real fee is read from the pool box (R4 = 995 -> 0.5%);
# this constant is only a display/fallback value.
SPECTRUM_POOL_FEE = 0.005  # 0.5% (995/1000) for SigUSD/ERG pool
SPECTRUM_EXECUTION_FEE = 0.785  # ERG service fee (via Crux Finance routing)

# How SigUSD pool legs are executed: "direct" = our own pool-box swap (execute_pool_swap.py,
# miner fee only), "crux" = Crux /dex/swap (adds SPECTRUM_EXECUTION_FEE per leg).
POOL_SWAP_ROUTE = os.getenv("POOL_SWAP_ROUTE", "direct").strip().lower()


def pool_service_fee() -> float:
    """Service fee per SigUSD pool leg for the configured route."""
    return SPECTRUM_EXECUTION_FEE if POOL_SWAP_ROUTE == "crux" else 0.0


def pool_fee_text() -> str:
    return f"-{SPECTRUM_EXECUTION_FEE} ERG Crux service fee" if POOL_SWAP_ROUTE == "crux" else "direct pool swap, no service fee"

# NonKYC fees (will be fetched dynamically, these are fallback defaults)
NONKYC_TRADING_FEE = 0.002  # 0.2% maker/taker (verify via API)
NONKYC_ERG_WITHDRAW_FEE = 3.3  # ERG (confirmed via /asset/info)

# Kucoin fees (will be fetched dynamically, these are fallback defaults)
KUCOIN_TRADING_FEE = 0.001  # 0.1% maker/taker
KUCOIN_ERG_WITHDRAW_FEE = 0.73  # ERG (confirmed via /api/v1/currencies/ERG)

# Venues. CEX paths (Kucoin/NonKYC) are off by default: on-chain only.
ENABLE_CEX = os.getenv("ENABLE_CEX", "false").strip().lower() in ("1", "true", "yes")
# USE paths are off by default: the USE LP was drained (Oct 2026) and a token migration is expected.
ENABLE_USE = os.getenv("ENABLE_USE", "false").strip().lower() in ("1", "true", "yes")
# Watch-only CEX prices (public endpoints, no API keys): show the gap to the on-chain pool
# and alert when it is worth connecting an exchange. Never trades.
CEX_WATCH = os.getenv("CEX_WATCH", "true").strip().lower() in ("1", "true", "yes")
CEX_WATCH_ALERT_PERCENT = float(os.getenv("CEX_WATCH_ALERT_PERCENT", "3.0"))
CEX_WATCH_COOLDOWN_SECONDS = int(os.getenv("CEX_WATCH_COOLDOWN_SECONDS", "3600"))

# Arbitrage settings
MIN_PROFIT_PERCENT = float(os.getenv("MIN_PROFIT_PERCENT", "0.5"))
MAX_TRADE_SIZE_ERG = float(os.getenv("MAX_TRADE_SIZE_ERG", "100"))  # hard cap on anything executed


def parse_sizes(text: str) -> list[float]:
    """Comma-separated ERG sizes; empty means the default grid."""
    sizes = [float(x) for x in text.split(",") if x.strip()]
    sizes = sizes or [10, 100, 200, 500, 1000]
    return [int(x) if x == int(x) else x for x in sorted(set(sizes))]


# Trade sizes the scanner analyses (grid columns). Sizes above MAX_TRADE_SIZE_ERG are shown
# for analysis only and never executed.
TRADE_SIZES = parse_sizes(os.getenv("TRADE_SIZES", ""))
MIN_TRADE_SIZE_ERG = float(os.getenv("MIN_TRADE_SIZE_ERG", "1"))  # lower bound of the best-size search
# Trade the smallest size that still earns this share of the best possible profit: near the
# peak, extra size adds little profit but all of its risk. 1.0 = maximise profit outright.
SIZE_PROFIT_CAPTURE = float(os.getenv("SIZE_PROFIT_CAPTURE", "0.95"))
SLIPPAGE_TOLERANCE = float(os.getenv("SLIPPAGE_TOLERANCE", "0.01"))  # 1%
MAX_FEE_BUDGET_ERG = float(os.getenv("MAX_FEE_BUDGET_ERG", "1.0"))  # max service + miner fees per signed TX
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "15"))
CHAIN_POLL_SECONDS = float(os.getenv("CHAIN_POLL_SECONDS", "2"))  # node poll for pool/bank/oracle changes

# Buffer for state changing between quote and inclusion, applied to legs whose
# price impact is computed from real reserves (AMM pool).
EXECUTION_BUFFER = float(os.getenv("EXECUTION_BUFFER", "0.003"))  # 0.3%

# Slippage recommendations based on trade size (legacy, legs without known depth)
SLIPPAGE_TIERS = {
    10: 0.005,    # 0.5% for up to 10 ERG
    50: 0.01,     # 1% for up to 50 ERG
    100: 0.02,    # 2% for up to 100 ERG
    500: 0.03,    # 3% for up to 500 ERG
}


# Discord Notifications
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID", "")
DISCORD_ENABLED = bool(DISCORD_WEBHOOK_URL)
DISCORD_MIN_PROFIT_PERCENT = float(os.getenv("DISCORD_MIN_PROFIT_PERCENT", "1.0"))
DISCORD_COOLDOWN_SECONDS = int(os.getenv("DISCORD_COOLDOWN_SECONDS", "300"))
DISCORD_CONFIRM_SCANS = int(os.getenv("DISCORD_CONFIRM_SCANS", "3"))
DISCORD_MIN_PROFIT_ERG = float(os.getenv("DISCORD_MIN_PROFIT_ERG", "0.5"))
DISCORD_TIER1_PROFIT_PERCENT = float(os.getenv("DISCORD_TIER1_PROFIT_PERCENT", "2.0"))
DISCORD_WALLET_COOLDOWN_SECONDS = int(os.getenv("DISCORD_WALLET_COOLDOWN_SECONDS", "600"))
DISCORD_SUMMARY_INTERVAL_SECONDS = int(os.getenv("DISCORD_SUMMARY_INTERVAL_SECONDS", "1800"))
PRICE_STALE_SECONDS = int(os.getenv("PRICE_STALE_SECONDS", "60"))

# --live auto-execution (pool buy -> bank redeem). Trades only when every check passes.
LIVE_CONFIRM_POLLS = int(os.getenv("LIVE_CONFIRM_POLLS", "2"))            # profitable on N chain polls in a row
LIVE_TRADE_COOLDOWN_SECONDS = int(os.getenv("LIVE_TRADE_COOLDOWN_SECONDS", "300"))
LIVE_MAX_TRADES_PER_DAY = int(os.getenv("LIVE_MAX_TRADES_PER_DAY", "10"))
LIVE_MAX_DRAWDOWN_ERG = float(os.getenv("LIVE_MAX_DRAWDOWN_ERG", "5"))     # stop if wallet value falls this much
LIVE_ERG_RESERVE = float(os.getenv("LIVE_ERG_RESERVE", "1"))               # ERG always kept in the wallet
LIVE_STOP_FILE = os.getenv("LIVE_STOP_FILE", "STOP")                      # kill switch: no trades while it exists
# Leg 2 (bank redeem) is watched until confirmed; if a bank/oracle box update drops it from the
# mempool it is rebuilt on fresh boxes and resubmitted (it spends leg 1's output, which stays ours).
LEG2_WATCH_TIMEOUT_SECONDS = int(os.getenv("LEG2_WATCH_TIMEOUT_SECONDS", "1200"))
LEG2_WATCH_INTERVAL_SECONDS = int(os.getenv("LEG2_WATCH_INTERVAL_SECONDS", "20"))
LEG2_MAX_REBUILDS = int(os.getenv("LEG2_MAX_REBUILDS", "5"))

# Tracker: non-profitable scan rows older than this are deleted at startup
SCAN_RESULTS_RETENTION_DAYS = int(os.getenv("SCAN_RESULTS_RETENTION_DAYS", "14"))


def get_recommended_slippage(trade_size_erg: float) -> float:
    for max_size, slippage in sorted(SLIPPAGE_TIERS.items()):
        if trade_size_erg <= max_size:
            return slippage
    return 0.05  # 5% for very large trades
