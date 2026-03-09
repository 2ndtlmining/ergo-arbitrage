"""Test Discord webhook notifications with fake data."""
import asyncio
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import config
from arbitrage.calculator import ArbitrageOpportunity, FeeBreakdown
from notifications.discord import DiscordNotifier


def make_opp(path, size, profit_pct, profit_erg, profit_usd, src_price, tgt_price,
             src_ex, tgt_ex, is_profitable=True, risk_ok=True, minutes=15, risk_pct=0.3):
    return ArbitrageOpportunity(
        path=f"{path} [{size} ERG]",
        input_erg=float(size),
        output_erg=float(size) + profit_erg,
        profit_erg=profit_erg,
        profit_percent=profit_pct,
        fees=FeeBreakdown(
            trading_fee=0.009,
            withdraw_fee_erg=0.0,
            network_fee_erg=0.0022,
            protocol_fee=0.0063,
            execution_fee_erg=0.002,
            slippage_cost=0.05,
        ),
        source_price=src_price,
        target_price=tgt_price,
        source_exchange=src_ex,
        target_exchange=tgt_ex,
        is_profitable=is_profitable,
        estimated_execution_minutes=minutes,
        price_risk_percent=risk_pct,
        profit_usd=profit_usd,
    )


async def main():
    print("=== Discord Notification Test ===")
    print()

    if not config.DISCORD_WEBHOOK_URL:
        print("ERROR: DISCORD_WEBHOOK_URL not set in .env")
        return

    print(f"Webhook: {config.DISCORD_WEBHOOK_URL[:50]}...")
    print(f"User ID: {config.DISCORD_USER_ID or 'not set'}")
    print()

    notifier = DiscordNotifier()
    await notifier.connect()

    # Test 1: Startup
    print("Test 1: Startup message...")
    await notifier.send_startup_message(mode="notify")
    print("  Sent!")
    await asyncio.sleep(1.5)

    # Test 2: Scan summary table (all paths, like the console grid)
    print("Test 2: Scan summary table...")
    opps = [
        make_opp("ERG ->(Bank)-> SigUSD ->(Spectrum)-> ERG", 10, 3.50, 0.350, 0.11, 0.32, 0.29,
                  "SigmaUSD Bank", "Spectrum DEX"),
        make_opp("ERG ->(Spectrum)-> SigUSD ->(Bank)-> ERG", 25, 1.20, 0.300, 0.10, 0.29, 0.32,
                  "Spectrum DEX", "SigmaUSD Bank"),
        make_opp("NonKYC<>DEX", 50, -2.10, -1.050, -0.34, 0.31, 0.29,
                  "NonKYC", "Spectrum DEX", is_profitable=False),
        make_opp("Kucoin<>DEX", 100, -1.80, -1.800, -0.58, 0.30, 0.29,
                  "Kucoin", "Spectrum DEX", is_profitable=False),
        make_opp("Buy Kucoin -> Sell NonKYC", 50, -5.30, -2.650, -0.85, 0.30, 0.31,
                  "Kucoin", "NonKYC", is_profitable=False),
        make_opp("Kucoin<>Bank", 25, 0.80, 0.200, 0.06, 0.32, 0.30,
                  "SigmaUSD Bank", "Kucoin", is_profitable=True, risk_ok=False),
        make_opp("NonKYC<>Bank", 10, 0.60, 0.060, 0.02, 0.32, 0.31,
                  "SigmaUSD Bank", "NonKYC", is_profitable=True, risk_ok=False),
    ]
    await notifier.send_scan_summary(opps, scan_number=42)
    print("  Sent!")
    await asyncio.sleep(1.5)

    # Test 3: Detailed opportunity notification (with user ping)
    print("Test 3: Opportunity detail (with ping)...")
    best = opps[0]  # The profitable one
    await notifier.notify_opportunity(best, scan_number=42)
    print("  Sent!")
    await asyncio.sleep(1.5)

    # Test 4: Shutdown summary
    print("Test 4: Shutdown summary...")
    await notifier.send_summary_message({
        "session_duration": "0:05:30",
        "opportunities_seen": 3,
        "total_potential_profit_erg": 0.85,
    })
    print("  Sent!")

    await notifier.disconnect()
    print()
    print("All tests complete! Check your Discord channel.")


if __name__ == "__main__":
    asyncio.run(main())
