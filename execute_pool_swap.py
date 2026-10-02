"""Swap ERG <-> SigUSD directly against the ErgoDEX pool box (no Crux service fee).

Usage:
    python execute_pool_swap.py --sell erg --amount 1          # dry run: build + verify
    python execute_pool_swap.py --sell sigusd --amount 1.5     # dry run, selling 1.50 SigUSD
    python execute_pool_swap.py --sell erg --amount 1 --check  # also sign + node script check, NOT broadcast
    python execute_pool_swap.py --sell erg --amount 1 --execute

--check signs the TX locally and asks your node to validate it (including the
pool contract) via /transactions/check. It never broadcasts, so nothing is spent.
"""
import argparse
import asyncio
import io
import sys

import aiohttp
from dotenv import load_dotenv
load_dotenv()

import config
from ergo.chain import explorer_box_id, node_box, wallet_context
from ergo.pool_swap import build_pool_swap_tx
from ergo.signing import DryRun, check_on_node, guarded_sign
from ergo.tx_guard import SignPolicy, TxGuardError

NODE = config.ERGO_NODE_URL
SIGUSD = config.SIGUSD_TOKEN_ID
TIMEOUT = aiohttp.ClientTimeout(total=30)


async def main(sell: str, amount: float, execute: bool, check: bool):
    sell_erg = sell == "erg"
    amount_in = int(round(amount * (1e9 if sell_erg else 100)))
    node_headers = {"api_key": config.ERGO_NODE_API_KEY, "Content-Type": "application/json"}
    mode = "EXECUTE" if execute else ("CHECK (sign + validate, no broadcast)" if check else "dry run")

    print("=" * 60)
    print(f"Direct pool swap: sell {amount} {'ERG' if sell_erg else 'SigUSD'}  [{mode}]")
    print("=" * 60)

    async with aiohttp.ClientSession() as s:
        pool_id = await explorer_box_id(s, config.SPECTRUM_SIGUSD_POOL_NFT)
    async with aiohttp.ClientSession(headers=node_headers) as ns:
        try:
            pool_box = await node_box(ns, pool_id)
        except RuntimeError as e:
            print(f"ABORTED: {e}")
            return
        wallet_boxes, height, our_tree = await wallet_context(ns)

    try:
        tx, info = build_pool_swap_tx(pool_box, wallet_boxes, SIGUSD, sell_erg=sell_erg,
                                      amount_in=amount_in, height=height, our_tree=our_tree)
    except ValueError as e:
        print(f"ABORTED: {e}")
        return

    rx, ry = info["reserves_before"]
    out = info["amount_out"]
    print(f"  Pool:          {pool_id[:16]}...  {rx / 1e9:,.2f} ERG / {ry / 100:,.2f} SigUSD, fee {1000 - info['fee_num']}/1000")
    if sell_erg:
        print(f"  You send:      {amount_in / 1e9:.4f} ERG + {info['miner_fee'] / 1e9:.4f} miner fee")
        print(f"  You receive:   {out / 100:.2f} SigUSD")
    else:
        print(f"  You send:      {amount_in / 100:.2f} SigUSD")
        print(f"  You receive:   {out / 1e9:.6f} ERG (minus {info['miner_fee'] / 1e9:.4f} miner fee)")
    print(f"  Price impact:  {info['price_impact_percent']:.2f}% (includes the 0.5% pool fee)")
    print("  Service fee:   none (direct pool spend)")
    print()

    if sell_erg:
        policy = SignPolicy(max_erg_spent=amount_in + info["miner_fee"], min_received={SIGUSD: out},
                            max_service_fee=0)
    else:
        policy = SignPolicy(max_erg_spent=0, max_token_spent={SIGUSD: amount_in},
                            min_erg_received=out - info["miner_fee"], max_service_fee=0)

    async with aiohttp.ClientSession(headers=node_headers) as ns:
        try:
            signed = await guarded_sign(ns, NODE, tx, policy, execute=execute or check)
        except DryRun as e:
            print(f"  {e}  (or --check to have the node validate it without broadcasting)")
            return
        except TxGuardError as e:
            print(f"  REFUSED by TX guard: {e}")
            return

        if check and not execute:
            ok, detail = await check_on_node(ns, NODE, signed)
            print(f"  Node check: {'VALID - the pool contract accepts this swap' if ok else 'REJECTED'}")
            if not ok:
                print(f"  {detail}")
            print("  Not broadcast (check mode).")
            return

        async with ns.post(f"{NODE}/transactions", json=signed, timeout=TIMEOUT) as r:
            body = await r.text()
            if r.status != 200:
                print(f"  SUBMIT FAILED: HTTP {r.status} {body[:500]}")
                print("  (If the pool moved since the quote, nothing was spent; just re-run.)")
                return
        tx_id = signed.get("id", "?")
        print(f"  Submitted: https://explorer.ergoplatform.com/en/transactions/{tx_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Swap ERG <-> SigUSD directly against the ErgoDEX pool")
    parser.add_argument("--sell", choices=["erg", "sigusd"], required=True, help="What you are selling")
    parser.add_argument("--amount", type=float, required=True, help="Amount to sell (ERG or SigUSD)")
    parser.add_argument("--check", action="store_true", help="Sign and validate on the node without broadcasting")
    parser.add_argument("--execute", action="store_true", help="Sign and submit")
    args = parser.parse_args()
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    asyncio.run(main(args.sell, args.amount, args.execute, args.check))
