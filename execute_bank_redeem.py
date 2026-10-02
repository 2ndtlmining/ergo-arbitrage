"""Execute a SigmaUSD Bank redeem (SigUSD -> ERG) via direct contract interaction.

Usage:
    python execute_bank_redeem.py --sigusd 1.0            # dry run: build + verify only
    python execute_bank_redeem.py --sigusd 1.0 --execute  # sign and submit
"""
import argparse
import asyncio
import io
import sys
import time

import aiohttp
from dotenv import load_dotenv
load_dotenv()

import config
from ergo.chain import explorer_box_id, node_box, wallet_context
from ergo.signing import DryRun, guarded_sign
from ergo.sigmausd_tx import build_redeem_tx
from ergo.tx_guard import SignPolicy, TxGuardError

NODE = config.ERGO_NODE_URL
EXPLORER = config.ERGO_EXPLORER_API_URL
SIGUSD_TOKEN = config.SIGUSD_TOKEN_ID
UI_FEE_TREE = "0008cd02c5f61c83056a746a19a9e449e3c9596314cc417a2ef496b7567af558518f2bc7"  # 9g2FAxry...

CHECK_INTERVAL = 30
MAX_WAIT = 600
TIMEOUT = aiohttp.ClientTimeout(total=30)


async def main(redeem_sigusd: float, execute: bool):
    cents = int(round(redeem_sigusd * 100))
    node_headers = {"api_key": config.ERGO_NODE_API_KEY, "Content-Type": "application/json"}

    print("=" * 60)
    print(f"SigmaUSD Bank Redeem: {redeem_sigusd:.2f} SigUSD -> ERG {'(EXECUTE)' if execute else '(dry run)'}")
    print("=" * 60)
    print()

    print("--- Step 1: Fetch State ---")
    async with aiohttp.ClientSession() as s:
        bank_id = await explorer_box_id(s, config.SIGMAUSD_BANK_NFT)
        oracle_id = await explorer_box_id(s, config.SIGMAUSD_ORACLE_NFT)

    async with aiohttp.ClientSession(headers=node_headers) as ns:
        try:
            bank_box = await node_box(ns, bank_id)
            oracle_box = await node_box(ns, oracle_id)
        except RuntimeError as e:
            print(f"ABORTED: {e}")
            return

        wallet_boxes, height, our_tree = await wallet_context(ns)

    try:
        unsigned_tx, info = build_redeem_tx(
            bank_box, oracle_box, wallet_boxes, cents, height=height, our_tree=our_tree, ui_fee_tree=UI_FEE_TREE
        )
    except ValueError as e:
        print(f"ABORTED: {e}")
        return

    state = info["state"]
    print(f"  Bank ERG:    {state.bank_erg_nano / 1e9:,.4f}")
    print(f"  SigUSD circ: {state.sigusd_circ_cents / 100:,.2f}")
    print(f"  Oracle:      1 USD = {state.oracle_r4 / 1e9:.6f} ERG")
    print(f"  RR:          {state.reserve_ratio:.0f}% (SigUSD redeem has no RR limit)")
    print(f"  Height:      {height}")
    print()

    print("--- Step 2: Calculate (contract-exact) ---")
    gross = state.nominal_price() * cents
    print(f"  Gross:     {gross / 1e9:.6f} ERG")
    print(f"  -2% fee:   {info['protocol_fee'] / 1e9:.6f}")
    print(f"  -UI fee:   {info['ui_fee'] / 1e9:.6f}")
    print(f"  -miner:    {info['miner_fee'] / 1e9:.6f}")
    print(f"  Net:       {(info['user_receives'] - info['miner_fee']) / 1e9:.6f} ERG")
    print(f"  Inputs:    bank + {len(info['user_boxes'])} wallet box(es)")
    print()

    print("--- Step 3: Verify & Sign ---")
    policy = SignPolicy(
        max_erg_spent=0,
        max_token_spent={SIGUSD_TOKEN: cents},
        min_erg_received=info["user_receives"] - info["miner_fee"],
        max_service_fee=info["ui_fee"],
        service_fee_trees=frozenset({UI_FEE_TREE}),
    )
    async with aiohttp.ClientSession(headers=node_headers) as ns:
        try:
            signed_tx = await guarded_sign(ns, NODE, unsigned_tx, policy, execute=execute)
        except DryRun as e:
            print(f"  {e}")
            return
        except TxGuardError as e:
            print(f"  REFUSED by TX guard: {e}")
            return
        tx_id = signed_tx.get("id", "?")
        print(f"  Signed: {tx_id}")

        async with ns.post(f"{NODE}/transactions", json=signed_tx, timeout=TIMEOUT) as r:
            body = await r.text()
            if r.status != 200:
                print(f"  SUBMIT FAILED: HTTP {r.status}")
                print(f"  {body[:800]}")
                return
        print("  Submitted!")
        print(f"  https://explorer.ergoplatform.com/en/transactions/{tx_id}")

    print()
    print("--- Step 4: Monitoring ---")
    start_time = time.time()
    async with aiohttp.ClientSession() as s:
        while time.time() - start_time < MAX_WAIT:
            await asyncio.sleep(CHECK_INTERVAL)
            elapsed = int(time.time() - start_time)
            async with s.get(f"{EXPLORER}/transactions/{tx_id}", timeout=TIMEOUT) as r:
                confirmed = r.status == 200 and (await r.json()).get("numConfirmations", 0) > 0
            if confirmed:
                print(f"  [{elapsed // 60}m{elapsed % 60:02d}s] CONFIRMED. REDEEM COMPLETE!")
                return
            print(f"  [{elapsed // 60}m{elapsed % 60:02d}s] Waiting for confirmation...")
        print("  TIMEOUT. Check explorer.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Redeem SigUSD at the SigmaUSD bank")
    parser.add_argument("--sigusd", type=float, default=1.0, help="SigUSD amount to redeem (default 1.0)")
    parser.add_argument("--execute", action="store_true", help="Sign and submit (otherwise dry run)")
    args = parser.parse_args()
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    asyncio.run(main(args.sigusd, args.execute))
