"""Execute a REAL ERG -> USE swap via Crux Finance LP pool and monitor for completion."""
import asyncio
import aiohttp
import json
import os
import sys
import io
import time

from dotenv import load_dotenv
load_dotenv()

import config
from ergo.signing import DryRun, execute_requested, guarded_sign
from ergo.tx_guard import SignPolicy
from exchanges.crux import parse_quote

FEE_BUDGET = int(config.MAX_FEE_BUDGET_ERG * 1e9)  # service + miner fees allowed per TX

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Configuration
EXPLORER = "https://api.ergoplatform.com/api/v1"
CRUX_API = "https://api.cruxfinance.io"
NODE = os.getenv("ERGO_NODE_URL", "http://127.0.0.1:9053")
API_KEY = os.getenv("ERGO_NODE_API_KEY", "")

USE_TOKEN = config.USE_TOKEN_ID
ERG_TOKEN = "0000000000000000000000000000000000000000000000000000000000000000"

# Swap amount in ERG (will be converted to nanoERG with 9 decimals)
SWAP_ERG = 1.0  # Small test amount
SWAP_RAW = int(SWAP_ERG * 1_000_000_000)  # 9 decimals -> nanoERG


# Monitoring settings
CHECK_INTERVAL = 30
MAX_WAIT = 600


async def get_wallet_info(ns):
    """Get wallet address, pubkey, and balances."""
    async with ns.get(
        f"{NODE}/wallet/addresses",
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        if r.status != 200:
            raise Exception(f"Failed to get addresses: HTTP {r.status}")
        addrs = await r.json()
        address = addrs[0]

    async with ns.get(
        f"{NODE}/wallet/balances",
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        bal = await r.json()
        erg_balance = bal.get("balance", 0) / 1e9
        assets = bal.get("assets", {})
        use_balance = assets.get(USE_TOKEN, 0) / 1000

    return address, erg_balance, use_balance


async def get_quote(s, erg_raw_amount):
    """Get a quote for ERG -> USE swap from Crux LP."""
    url = (
        f"{CRUX_API}/dex/quote"
        f"?given_token_id={ERG_TOKEN}"
        f"&given_token_amount={erg_raw_amount}"
        f"&requested_token_id={USE_TOKEN}"
    )
    async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            body = await r.text()
            raise Exception(f"Quote failed: HTTP {r.status} - {body[:500]}")
        return await r.json()


async def build_swap_tx(s, address, erg_raw_amount):
    """Build unsigned ERG -> USE swap TX via Crux LP."""
    url = (
        f"{CRUX_API}/dex/swap"
        f"?user_addresses={address}"
        f"&target_address={address}"
        f"&given_token_id={ERG_TOKEN}"
        f"&given_token_amount={erg_raw_amount}"
        f"&requested_token_id={USE_TOKEN}"
        f"&fee_token=erg"
    )
    async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
        body = await r.text()
        if r.status != 200:
            raise Exception(f"Build swap TX failed: HTTP {r.status} - {body[:500]}")
        return json.loads(body)


async def sign_transaction(ns, unsigned_tx, policy):
    """Verify the Crux-built TX with tx_guard, then sign it (only with --execute)."""
    return await guarded_sign(ns, NODE, unsigned_tx, policy, execute=execute_requested())


async def submit_transaction(ns, signed_tx):
    """Submit a signed transaction to the network."""
    async with ns.post(
        f"{NODE}/transactions",
        json=signed_tx,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        body = await r.text()
        if r.status != 200:
            raise Exception(f"Submit failed: HTTP {r.status} - {body[:500]}")
        return body.strip().strip('"')


async def main():
    if not config.ENABLE_USE:
        print("USE is disabled (ENABLE_USE=false): the USE LP was drained and a token migration is expected.")
        return
    node_headers = {"api_key": API_KEY, "Content-Type": "application/json"}

    print("=" * 60)
    print("ERG -> USE Swap via Crux Finance LP Pool")
    print("=" * 60)
    print()

    # Step 1: Wallet info
    print("--- Step 1: Wallet Info ---")
    async with aiohttp.ClientSession(headers=node_headers) as ns:
        address, erg_balance, use_balance = await get_wallet_info(ns)

    print(f"  Address:     {address}")
    print(f"  ERG balance: {erg_balance:.4f} ERG")
    print(f"  USE balance: {use_balance:.3f} USE")
    print()

    if erg_balance < SWAP_ERG:
        print(f"ABORTED: Need {SWAP_ERG:.4f} ERG but only have {erg_balance:.4f} ERG")
        return

    # Step 2: Get quote
    print("--- Step 2: Get Quote ---")
    async with aiohttp.ClientSession() as s:
        quote = await get_quote(s, SWAP_RAW)

    parsed = parse_quote(quote)
    use_output_raw = parsed.output
    use_output = use_output_raw / 1000  # 3 decimals
    price_impact = parsed.price_impact
    lp_fee = parsed.lp_fee_percent
    pool_id = quote.get("details", {}).get("amm", {}).get("pool_id", "?")

    print(f"  Selling:       {SWAP_ERG:.4f} ERG")
    print(f"  Receiving:     {use_output:.3f} USE")
    if use_output > 0:
        print(f"  Rate:          1 ERG = {use_output/SWAP_ERG:.3f} USE")
    print(f"  Price impact:  {price_impact:.2f}%")
    print(f"  LP fee:        {lp_fee}%")
    print(f"  Pool:          {pool_id[:40]}...")
    print()

    # Confirmation gate
    if not execute_requested():
        print("DRY RUN: building and verifying the TX without signing (pass --execute to sign).")

    # Step 3: Build unsigned TX
    print("--- Step 3: Build Swap TX ---")
    async with aiohttp.ClientSession() as s:
        try:
            result = await build_swap_tx(s, address, SWAP_RAW)
        except Exception as e:
            print(f"  ERROR: {e}")
            return

    # Crux /dex/swap returns {unsigned_tx, pool_id, expected_output, price_impact, fee_amount, fee_token}
    unsigned_tx = result.get("unsigned_tx", result)
    expected_output = (result.get("expected_output") or 0)
    swap_fee = (result.get("fee_amount") or 0)
    print(f"  Expected output: {expected_output / 1000:.3f} USE")
    print(f"  Service fee:     {swap_fee / 1e9:.6f} ERG")

    inputs = unsigned_tx.get("inputs", [])
    outputs = unsigned_tx.get("outputs", [])
    print(f"  TX inputs:   {len(inputs)}")
    print(f"  TX outputs:  {len(outputs)}")
    print()

    # Step 4: Sign
    print("--- Step 4: Signing Transaction ---")
    async with aiohttp.ClientSession(headers=node_headers) as ns:
        try:
            policy = SignPolicy(max_erg_spent=SWAP_RAW + FEE_BUDGET, min_received={USE_TOKEN: int(use_output_raw * (1 - config.SLIPPAGE_TOLERANCE))})
            signed_tx = await sign_transaction(ns, unsigned_tx, policy)
        except DryRun as e:
            print(f"  {e}")
            return
        except Exception as e:
            print(f"  ERROR signing: {e}")
            return

    signed_tx_id = signed_tx.get("id", "?")
    print(f"  Signed TX ID: {signed_tx_id}")
    print()

    # Step 5: Submit
    print("--- Step 5: Submitting Transaction ---")
    async with aiohttp.ClientSession(headers=node_headers) as ns:
        try:
            submitted_id = await submit_transaction(ns, signed_tx)
        except Exception as e:
            print(f"  ERROR submitting: {e}")
            return

    print(f"  SUCCESS! Transaction submitted!")
    print(f"  TX ID: {submitted_id}")
    print(f"  Explorer: https://explorer.ergoplatform.com/en/transactions/{submitted_id}")
    print()

    # Step 6: Monitor
    print("--- Step 6: Monitoring ---")
    print(f"  Checking every {CHECK_INTERVAL}s for up to {MAX_WAIT // 60} minutes...")

    start_time = time.time()
    initial_erg = erg_balance
    initial_use = use_balance

    async with aiohttp.ClientSession() as s:
        async with aiohttp.ClientSession(headers=node_headers) as ns:
            while True:
                elapsed = time.time() - start_time
                if elapsed > MAX_WAIT:
                    print(f"\n  TIMEOUT after {MAX_WAIT // 60} minutes!")
                    print(f"  Check: https://explorer.ergoplatform.com/en/transactions/{submitted_id}")
                    break

                await asyncio.sleep(CHECK_INTERVAL)
                elapsed = time.time() - start_time
                mins = int(elapsed // 60)
                secs = int(elapsed % 60)

                # Check balances
                async with ns.get(
                    f"{NODE}/wallet/balances",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    bal = await r.json()
                    current_erg = bal.get("balance", 0) / 1e9
                    current_use = bal.get("assets", {}).get(USE_TOKEN, 0) / 1000

                erg_change = current_erg - initial_erg
                use_change = current_use - initial_use

                # Check explorer for confirmation
                async with s.get(
                    f"{EXPLORER}/transactions/{submitted_id}",
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    confirmed = r.status == 200
                    if confirmed:
                        tx_data = await r.json()
                        confs = tx_data.get("numConfirmations", 0)
                    else:
                        confs = 0

                if confirmed and confs > 0:
                    print(f"  [{mins}m{secs:02d}s] TX CONFIRMED! Confirmations: {confs}")
                    print(f"  ERG: {current_erg:.4f} (change: {erg_change:+.4f})")
                    print(f"  USE: {current_use:.3f} (change: {use_change:+.3f})")
                    print()
                    print("  SWAP COMPLETE!")
                    break
                else:
                    print(f"  [{mins}m{secs:02d}s] Waiting... ERG={current_erg:.4f} ({erg_change:+.4f}) USE={current_use:.3f} ({use_change:+.3f})")

                    # For ERG->USE: ERG goes down, USE goes up
                    if erg_change < -0.001 and use_change > 0.001:
                        print(f"  Balance changed! Swap likely succeeded.")
                        break


if __name__ == "__main__":
    asyncio.run(main())
