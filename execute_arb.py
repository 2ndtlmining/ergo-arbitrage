"""On-chain arbitrage in two chained transactions: buy SigUSD on the ErgoDEX pool, redeem it at the bank.

Usage:
    python execute_arb.py --erg 10            # plan + verify both legs (dry run)
    python execute_arb.py --erg 10 --check    # also sign leg 1 and have the node validate it (no broadcast)
    python execute_arb.py --erg 10 --execute  # submit leg 1, then leg 2 straight after (only if profitable)

Leg 2 spends leg 1's output, so the node can only validate leg 2 once leg 1 is
in its mempool; before that, leg 2 is checked by the TX guard only. If leg 2
fails after leg 1 went through, the wallet holds the SigUSD from leg 1 and the
script prints the redeem command to finish.
"""
import argparse
import asyncio
import io
import sys

import aiohttp
from dotenv import load_dotenv
load_dotenv()

import config
from ergo.chain import find_box_id, node_box, wait_for_box, wallet_context
from ergo.chain_arb import build_redeem_leg, plan_pool_buy_redeem, tx_output_box
from ergo.signing import DryRun, check_on_node, guarded_sign, wallet_trees
from ergo.tx_guard import TxGuardError, verify_unsigned_tx

NODE = config.ERGO_NODE_URL
UI_FEE_TREE = "0008cd02c5f61c83056a746a19a9e449e3c9596314cc417a2ef496b7567af558518f2bc7"  # SigmaUSD UI fee
TIMEOUT = aiohttp.ClientTimeout(total=30)


async def fetch_state(ns):
    ids = [await find_box_id(nft, ns) for nft in
           (config.SPECTRUM_SIGUSD_POOL_NFT, config.SIGMAUSD_BANK_NFT, config.SIGMAUSD_ORACLE_NFT)]
    return [await node_box(ns, i) for i in ids]


async def submit(ns, signed: dict) -> str:
    async with ns.post(f"{NODE}/transactions", json=signed, timeout=TIMEOUT) as r:
        body = await r.text()
        if r.status != 200:
            raise RuntimeError(f"submit failed: HTTP {r.status} {body[:400]}")
    return signed.get("id", "?")


async def main(erg: float, check: bool, execute: bool, force: bool):
    erg_in = int(round(erg * 1e9))
    headers = {"api_key": config.ERGO_NODE_API_KEY, "Content-Type": "application/json"}
    mode = "EXECUTE" if execute else ("CHECK (no broadcast)" if check else "dry run")
    print("=" * 64)
    print(f"Pool buy -> bank redeem with {erg} ERG  [{mode}]")
    print("=" * 64)

    async with aiohttp.ClientSession(headers=headers) as ns:
        try:
            pool_box, bank_box, oracle_box = await fetch_state(ns)
        except RuntimeError as e:
            print(f"ABORTED: {e}")
            return
        wallet_boxes, height, our_tree = await wallet_context(ns)
        trees = await wallet_trees(ns, NODE)

        try:
            plan = plan_pool_buy_redeem(pool_box, bank_box, oracle_box, wallet_boxes, erg_in,
                                        height=height, our_tree=our_tree, ui_fee_tree=UI_FEE_TREE)
        except ValueError as e:
            print(f"ABORTED: {e}")
            return

        i1, i2 = plan["leg1_info"], plan["leg2_info"]
        cents = plan["sigusd_cents"]
        print(f"  Leg 1  pool swap:   {erg_in / 1e9:.4f} ERG -> {cents / 100:.2f} SigUSD "
              f"(impact {i1['price_impact_percent']:.2f}%, miner {i1['miner_fee'] / 1e9:.4f})")
        print(f"  Leg 2  bank redeem: {cents / 100:.2f} SigUSD -> {i2['user_receives'] / 1e9:.6f} ERG "
              f"(2% + UI fee {i2['ui_fee'] / 1e9:.4f}, miner {i2['miner_fee'] / 1e9:.4f})")
        profit = plan["profit_nanoerg"]
        verdict = "PROFITABLE" if plan["profit_percent"] >= config.MIN_PROFIT_PERCENT else "NOT profitable"
        print(f"  Net:   {profit / 1e9:+.6f} ERG ({plan['profit_percent']:+.2f}%)  -> {verdict} "
              f"(MIN_PROFIT_PERCENT={config.MIN_PROFIT_PERCENT}%)")
        print()

        # Leg 2 can only be guard-checked locally until leg 1's output exists
        try:
            verify_unsigned_tx(plan["leg2_tx"], [bank_box, plan["leg1_output_box"]], trees, plan["leg2_policy"])
            print("  Leg 2 TX guard OK (against leg 1's planned output)")
        except TxGuardError as e:
            print(f"  REFUSED: leg 2 fails the TX guard: {e}")
            return

        if execute and plan["profit_percent"] < config.MIN_PROFIT_PERCENT and not force:
            print("  Not executing: below MIN_PROFIT_PERCENT (use --check to validate for free).")
            return

        try:
            signed1 = await guarded_sign(ns, NODE, plan["leg1_tx"], plan["leg1_policy"], execute=execute or check)
        except DryRun:
            print("  Leg 1 verified, not signed (dry run). Use --check for a node validation, --execute to run.")
            return
        except TxGuardError as e:
            print(f"  REFUSED by TX guard (leg 1): {e}")
            return

        if not execute:
            ok, detail = await check_on_node(ns, NODE, signed1)
            print(f"  Leg 1 node check: {'VALID' if ok else 'REJECTED'}")
            if not ok:
                print(f"  {detail}")
            print("  Leg 2 is validated by the node only once leg 1 is in the mempool (execute mode).")
            print("  Not broadcast (check mode).")
            return

        try:
            tx1 = await submit(ns, signed1)
        except RuntimeError as e:
            print(f"  Leg 1 {e}. Nothing was spent; re-run.")
            return
        print(f"  Leg 1 submitted: https://explorer.ergoplatform.com/en/transactions/{tx1}")

        recover = f"python execute_bank_redeem.py --sigusd {cents / 100:.2f} --execute"
        try:
            leg1_out = tx_output_box(signed1, 1)
            await wait_for_box(ns, leg1_out["boxId"], timeout=60)
            # fresh bank/oracle for leg 2 (the old pool box is spent by leg 1)
            bank_id = await find_box_id(config.SIGMAUSD_BANK_NFT, ns)
            oracle_id = await find_box_id(config.SIGMAUSD_ORACLE_NFT, ns)
            bank_box, oracle_box = await node_box(ns, bank_id), await node_box(ns, oracle_id)
            tx2_unsigned, info2, policy2 = build_redeem_leg(bank_box, oracle_box, leg1_out, cents, height,
                                                            our_tree, UI_FEE_TREE)
            signed2 = await guarded_sign(ns, NODE, tx2_unsigned, policy2, execute=True)
            tx2 = await submit(ns, signed2)
        except (RuntimeError, TimeoutError, TxGuardError, ValueError) as e:
            print(f"  Leg 2 FAILED: {e}")
            print(f"  You now hold {cents / 100:.2f} SigUSD from leg 1. Finish with:\n    {recover}")
            return
        print(f"  Leg 2 submitted: https://explorer.ergoplatform.com/en/transactions/{tx2}")
        print(f"  Expected net: {(info2['user_receives'] - info2['miner_fee'] - erg_in - i1['miner_fee']) / 1e9:+.6f} ERG")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Two-leg on-chain arbitrage: pool buy -> bank redeem")
    parser.add_argument("--erg", type=float, required=True, help="ERG to put into leg 1")
    parser.add_argument("--check", action="store_true", help="Sign leg 1 and validate on the node, no broadcast")
    parser.add_argument("--execute", action="store_true", help="Submit both legs (only if profitable)")
    parser.add_argument("--force", action="store_true", help="Execute even below MIN_PROFIT_PERCENT (testing)")
    args = parser.parse_args()
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    asyncio.run(main(args.erg, args.check, args.execute, args.force))
