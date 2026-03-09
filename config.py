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

# Spectrum Finance (ErgoDEX)
SPECTRUM_API_URL = "https://api.spectrum.fi/v1"

# Token IDs (Ergo mainnet)
ERG_TOKEN_ID = "0000000000000000000000000000000000000000000000000000000000000000"
SIGUSD_TOKEN_ID = "03faf2cb329f2e90d6d23b58d91bbb6c046aa143261cc21f52fbe2824bfcbf04"
SIGRSV_TOKEN_ID = "003bd19d0187117f130b62e1bcab0939929ff5c7709f843c5c4dd158949285d0"
SIGMAUSD_BANK_NFT = "7d672d1def471720ca5782fd6473e47e796d9ac0c138d9911346f118b2f6d9d9"
USE_TOKEN_ID = "a55b8735ed1a99e46c2c89f8994aacdf4b1109bdcf682f1e5b34479c6e392669"

# Decimals
ERG_DECIMALS = 9
SIGUSD_DECIMALS = 2
SIGRSV_DECIMALS = 0
USE_DECIMALS = 3

# Crux Finance (USE / DexyUSD)
CRUX_API_URL = "https://api.cruxfinance.io"

# Ergo network fees
ERGO_TX_FEE = 0.0011  # ERG
ERGO_MIN_BOX_VALUE = 0.001  # ERG

# SigmaUSD Bank fees
SIGMAUSD_PROTOCOL_FEE = 0.02  # 2% (stays in bank reserve)
SIGMAUSD_FRONTEND_FEE = 0.00229  # 0.229% UI fee on bc_delta (after protocol fee)
SIGMAUSD_TOTAL_FEE = SIGMAUSD_PROTOCOL_FEE + SIGMAUSD_FRONTEND_FEE
SIGMAUSD_REDEEM_EXTRA_ERG = 0.0021  # receipt box (0.001) + miner fee (0.0011)

# Spectrum DEX fees (SigUSD/ERG pool is 0.5%, confirmed via Crux API)
SPECTRUM_POOL_FEE = 0.005  # 0.5% (995/1000) for SigUSD/ERG pool
SPECTRUM_EXECUTION_FEE = 0.785  # ERG service fee (via Crux Finance routing)

# NonKYC fees (will be fetched dynamically, these are fallback defaults)
NONKYC_TRADING_FEE = 0.002  # 0.2% maker/taker (verify via API)
NONKYC_ERG_WITHDRAW_FEE = 3.3  # ERG (confirmed via /asset/info)

# Kucoin fees (will be fetched dynamically, these are fallback defaults)
KUCOIN_TRADING_FEE = 0.001  # 0.1% maker/taker
KUCOIN_ERG_WITHDRAW_FEE = 0.73  # ERG (confirmed via /api/v1/currencies/ERG)

# Arbitrage settings
MIN_PROFIT_PERCENT = float(os.getenv("MIN_PROFIT_PERCENT", "0.5"))
MAX_TRADE_SIZE_ERG = float(os.getenv("MAX_TRADE_SIZE_ERG", "100"))
SLIPPAGE_TOLERANCE = float(os.getenv("SLIPPAGE_TOLERANCE", "0.01"))  # 1%
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "15"))

# Slippage recommendations based on trade size
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


def get_recommended_slippage(trade_size_erg: float) -> float:
    for max_size, slippage in sorted(SLIPPAGE_TIERS.items()):
        if trade_size_erg <= max_size:
            return slippage
    return 0.05  # 5% for very large trades
