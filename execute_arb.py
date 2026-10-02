"""On-chain arbitrage in two chained transactions: buy SigUSD on the ErgoDEX pool, redeem it at the bank.

Usage:
    python execute_arb.py --erg 10            # plan + verify both legs (dry run)
    python execute_arb.py --erg 10 --check    # also sign leg 1 and have the node validate it (no broadcast)
    python execute_arb.py --erg 10 --execute  # submit leg 1, then leg 2 straight after (only if profitable)

Leg 2 spends leg 1's output, so the node can only validate leg 2 once leg 1 is
in its mempool; before that, leg 2 is checked by the TX guard only. If leg 2
fails after leg 1 went through, the wallet holds the SigUSD from leg 1 and the
script prints the redeem command to finish. The same logic runs in the scanner's
--live mode (ergo/arb_runner.py).
"""
import argparse
import asyncio
import io
import sys

import aiohttp
from dotenv import load_dotenv
load_dotenv()

import config
from ergo.arb_runner import PATHS, run_arb


async def main(erg: float, check: bool, execute: bool, force: bool, path: str):
    mode = "EXECUTE" if execute else ("CHECK (no broadcast)" if check else "dry run")
    print("=" * 64)
    print(f"{PATHS[path][0]} with {erg} ERG  [{mode}]")
    print("=" * 64)
    headers = {"api_key": config.ERGO_NODE_API_KEY, "Content-Type": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as ns:
        await run_arb(ns, path, int(round(erg * 1e9)), check=check, execute=execute, force=force)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Two-leg on-chain arbitrage: pool buy -> bank redeem")
    parser.add_argument("--erg", type=float, required=True, help="ERG to put into leg 1")
    parser.add_argument("--check", action="store_true", help="Sign leg 1 and validate on the node, no broadcast")
    parser.add_argument("--execute", action="store_true", help="Submit both legs (only if profitable)")
    parser.add_argument("--force", action="store_true", help="Execute even below MIN_PROFIT_PERCENT (testing)")
    parser.add_argument("--path", choices=["redeem", "mint"], default="redeem",
                        help="redeem: pool buy -> bank redeem; mint: bank mint -> pool sell")
    args = parser.parse_args()
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    asyncio.run(main(args.erg, args.check, args.execute, args.force, args.path))
