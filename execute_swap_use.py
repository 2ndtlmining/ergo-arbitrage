"""Execute a REAL ERG -> USE swap via Crux Finance free mint and monitor for completion."""
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
from exchanges.dexy import quote_dexy_mint
from ergo.signing import DryRun, execute_requested, guarded_sign
from ergo.tx_guard import SignPolicy

FEE_BUDGET = int(config.MAX_FEE_BUDGET_ERG * 1e9)  # service + miner fees allowed per TX

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Configuration
EXPLORER = "https://api.ergoplatform.com/api/v1"
CRUX_API = "https://api.cruxfinance.io"
NODE = os.getenv("ERGO_NODE_URL", "http://127.0.0.1:9053")
API_KEY = os.getenv("ERGO_NODE_API_KEY", "")

USE_TOKEN = config.USE_TOKEN_ID

# Swap amount in nanoERG (1 ERG = 1,000,000,000 nanoERG)
SWAP_ERG = 1.0
SWAP_NANO = int(SWAP_ERG * 1e9)

# Monitoring settings
CHECK_INTERVAL = 30  # seconds between checks
MAX_WAIT = 600  # 10 minutes max wait


async def get_wallet_info(ns):
    """Get wallet address, pubkey, and balance from the node."""
    # Get address
    async with ns.get(
        f"{NODE}/wallet/addresses",
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        if r.status != 200:
            raise Exception(f"Failed to get addresses: HTTP {r.status}")
        addrs = await r.json()
        address = addrs[0]

    # Get pubkey from unspent boxes
    async with ns.get(
        f"{NODE}/wallet/boxes/unspent?minConfirmations=0&minInclusionHeight=0",
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        boxes = await r.json()
        if not boxes:
            raise Exception("No unspent boxes in wallet")
        tree = boxes[0].get("box", {}).get("ergoTree", "")
        pubkey = tree[6:] if tree.startswith("0008cd") else None

    # Get balance
    async with ns.get(
        f"{NODE}/wallet/balances",
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        bal = await r.json()
        erg_balance = bal.get("balance", 0) / 1e9
        assets = bal.get("assets", {})
        use_balance = assets.get(USE_TOKEN, 0) / 1000  # 3 decimals

    return address, pubkey, erg_balance, use_balance


async def check_mint_availability(s):
    """Check if USE free mint is available via Crux Finance."""
    async with s.get(
        f"{CRUX_API}/dexy/mint_status/use?mint_type=free_mint",
        timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        if r.status != 200:
            raise Exception(f"Crux mint_status failed: HTTP {r.status}")
        return await r.json()


async def check_use_analytics(s):
    """Get current USE analytics."""
    async with s.get(
        f"{CRUX_API}/dexy/analytics/use",
        timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        if r.status != 200:
            return None
        return await r.json()


async def build_mint_tx(s, address, erg_amount_nano):
    """Build unsigned mint TX via Crux Finance API."""
    url = (
        f"{CRUX_API}/dexy/build_mint_tx/use"
        f"?user_addresses={address}"
        f"&target_address={address}"
        f"&mint_type=free_mint"
        f"&erg_amount={erg_amount_nano}"
    )
    async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
        body = await r.text()
        if r.status != 200:
            raise Exception(f"Crux build_mint_tx failed: HTTP {r.status} - {body[:500]}")
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


async def check_tx_confirmed(s, tx_id):
    """Check if a transaction is confirmed on chain."""
    async with s.get(
        f"{EXPLORER}/transactions/{tx_id}",
        timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        if r.status == 200:
            tx = await r.json()
            # If we can get the TX from explorer, it's confirmed
            confirmations = tx.get("numConfirmations", 0)
            return True, confirmations
        return False, 0


async def check_wallet_use_balance(ns):
    """Check wallet USE token balance."""
    async with ns.get(
        f"{NODE}/wallet/balances",
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        bal = await r.json()
        assets = bal.get("assets", {})
        return assets.get(USE_TOKEN, 0) / 1000  # 3 decimals


async def recover_stuck_box(ns, s, box_id, our_address):
    """Attempt to recover ERG from a stuck swap order box."""
    print()
    print("=" * 60)
    print("RECOVERY: Attempting to recover stuck funds")
    print("=" * 60)

    # Get the box from explorer
    async with s.get(
        f"{EXPLORER}/boxes/{box_id}",
        timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        if r.status != 200:
            print(f"  Could not find box on explorer: HTTP {r.status}")
            return False
        box_data = await r.json()

    if box_data.get("spentTransactionId"):
        print(f"  Box already spent in TX: {box_data['spentTransactionId']}")
        return True

    stuck_value = box_data["value"]
    print(f"  Stuck box value: {stuck_value / 1e9:.6f} ERG")

    # Get raw bytes from the node
    async with ns.get(
        f"{NODE}/utxo/byIdBinary/{box_id}",
        timeout=aiohttp.ClientTimeout(total=10),
    ) as r:
        if r.status != 200:
            print(f"  Could not get raw bytes from node: HTTP {r.status}")
            # Try from explorer
            print("  Trying alternative recovery...")
            return False
        raw_data = await r.json()
        raw_bytes = raw_data.get("bytes", "")

    if not raw_bytes:
        print("  No raw bytes available")
        return False

    miner_fee = 1100000
    refund_value = stuck_value - miner_fee

    if refund_value <= 0:
        print(f"  Box value too small to recover (need > {miner_fee/1e9:.4f} ERG for miner fee)")
        return False

    refund_tx = {
        "requests": [
            {
                "address": our_address,
                "value": refund_value,
                "assets": [],
                "registers": {},
            }
        ],
        "fee": miner_fee,
        "inputsRaw": [raw_bytes],
        "dataInputsRaw": [],
    }

    print(f"  Refunding {refund_value / 1e9:.6f} ERG back to wallet")
    async with ns.post(
        f"{NODE}/wallet/transaction/send",
        json=refund_tx,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        body = await r.text()
        if r.status == 200:
            tx_id = body.strip().strip('"')
            print(f"  REFUND SUCCESS! TX: {tx_id}")
            print(f"  Explorer: https://explorer.ergoplatform.com/en/transactions/{tx_id}")
            return True
        else:
            print(f"  Refund failed: HTTP {r.status}")
            print(f"  Error: {body[:500]}")
            return False


async def main():
    if not config.ENABLE_USE:
        print("USE is disabled (ENABLE_USE=false): the USE LP was drained and a token migration is expected.")
        return
    node_headers = {"api_key": API_KEY, "Content-Type": "application/json"}

    print("=" * 60)
    print("ERG -> USE Swap via Crux Finance Free Mint")
    print("=" * 60)
    print()

    # ========================================
    # STEP 1: Get wallet info
    # ========================================
    print("--- Step 1: Wallet Info ---")
    async with aiohttp.ClientSession(headers=node_headers) as ns:
        address, pubkey, erg_balance, use_balance = await get_wallet_info(ns)

    print(f"  Address:     {address}")
    print(f"  Pubkey:      {pubkey}")
    print(f"  ERG balance: {erg_balance:.4f} ERG")
    print(f"  USE balance: {use_balance:.3f} USE")
    print()

    # ========================================
    # STEP 2: Check mint availability
    # ========================================
    print("--- Step 2: Check Mint Availability ---")
    async with aiohttp.ClientSession() as s:
        mint_status = await check_mint_availability(s)
        analytics = await check_use_analytics(s)

    print(f"  Mint type:    free_mint")
    print(f"  Available:    {mint_status.get('is_available', False)}")
    print(f"  Max mint:     {mint_status.get('max_mint_amount', 0)} USE units")
    print(f"  Fee:          {mint_status.get('fee_amount', 0)/1e9:.6f} ERG (~${mint_status.get('fee_usd', 0):.2f})")
    if analytics:
        print(f"  USE price:    {analytics.get('stablecoin_price_erg', 0):.4f} ERG/USE")
        print(f"  USE price:    ${analytics.get('stablecoin_price_usd', 0):.4f}")
        print(f"  Bank ratio:   {analytics.get('bank_reserve_ratio', 0):.4f}")
    print()

    if not mint_status.get("is_available", False):
        print("ABORTED: Free mint is not currently available!")
        constraints = mint_status.get("constraints", [])
        for c in constraints:
            print(f"  Constraint: {c.get('type', '?')}")
        return

    # ========================================
    # STEP 3: Build unsigned TX via Crux Finance
    # ========================================
    print("--- Step 3: Build Unsigned TX ---")
    async with aiohttp.ClientSession() as s:
        try:
            result = await build_mint_tx(s, address, SWAP_NANO)
        except Exception as e:
            print(f"  ERROR: {e}")
            return

    unsigned_tx = result.get("transaction", {})
    mint_details = result.get("mint_details", {})

    use_received = mint_details.get("mint_amount", 0) / 1000  # 3 decimals
    erg_to_bank = mint_details.get("erg_to_bank", 0) / 1e9
    fee_erg = mint_details.get("fee_amount", 0) / 1e9
    fee_usd = mint_details.get("fee_usd", 0)

    print(f"  USE to receive:  {use_received:.3f} USE")
    print(f"  ERG to bank:     {erg_to_bank:.6f} ERG")
    print(f"  Protocol fee:    {fee_erg:.6f} ERG (~${fee_usd:.2f})")
    print(f"  Bank fee:        {mint_details.get('dexy_bank_fee', 0)/1e9:.6f} ERG")
    print(f"  Buyback fee:     {mint_details.get('dexy_buyback_fee', 0)/1e9:.6f} ERG")
    print()

    # Analyze the TX outputs to understand total cost
    outputs = unsigned_tx.get("outputs", [])
    inputs = unsigned_tx.get("inputs", [])

    # Find our input and output to calculate net cost
    our_tree_prefix = f"0008cd{pubkey}"
    our_input_total = 0
    our_output_total = 0
    our_use_output = 0
    our_output_box_ids = []

    for inp in inputs:
        if inp.get("ergoTree", "").startswith("0008cd") and pubkey in inp.get("ergoTree", ""):
            our_input_total += int(inp.get("value", 0))

    for out in outputs:
        if out.get("ergoTree", "") == our_tree_prefix:
            our_output_total += int(out.get("value", 0))
            for asset in out.get("assets", []):
                if asset.get("tokenId", "") == USE_TOKEN:
                    our_use_output += int(asset.get("amount", 0))

    net_cost_erg = (our_input_total - our_output_total) / 1e9
    miner_fee = 0
    for out in outputs:
        tree = out.get("ergoTree", "")
        # Miner fee output has a specific ErgoTree pattern
        if "100204a00b08cd0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798" in tree:
            miner_fee = int(out.get("value", 0))

    print("=" * 60)
    print(f"  === SWAP SUMMARY: {SWAP_ERG} ERG -> USE ===")
    print("=" * 60)
    print(f"  Swap input:      {SWAP_ERG:.4f} ERG")
    print(f"  USE received:    {our_use_output/1000:.3f} USE")
    print(f"  Net ERG cost:    {net_cost_erg:.6f} ERG")
    print(f"  Protocol fee:    {fee_erg:.6f} ERG (~${fee_usd:.2f})")
    if miner_fee:
        print(f"  Miner fee:       {miner_fee/1e9:.6f} ERG")
    print(f"  Change back:     {our_output_total/1e9:.6f} ERG")
    print(f"  Wallet balance:  {erg_balance:.4f} ERG")
    print(f"  Sufficient:      {'YES' if erg_balance >= net_cost_erg else 'NO'}")
    print(f"  TX inputs:       {len(inputs)}")
    print(f"  TX outputs:      {len(outputs)}")
    print()

    if erg_balance < net_cost_erg:
        print("ABORTED: Insufficient balance!")
        return

    # ========================================
    # STEP 4: Sign the transaction
    # ========================================
    print("--- Step 4: Signing Transaction ---")
    async with aiohttp.ClientSession(headers=node_headers) as ns:
        try:
            policy = SignPolicy(max_erg_spent=SWAP_NANO + int(mint_status.get('fee_amount', 0)) + FEE_BUDGET, min_received={USE_TOKEN: int(quote_dexy_mint(SWAP_NANO - int(mint_status.get('fee_amount', 0)), mint_status['box_state']) * (1 - config.SLIPPAGE_TOLERANCE))})
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

    # ========================================
    # STEP 5: Submit the transaction
    # ========================================
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

    # ========================================
    # STEP 6: Monitor for confirmation
    # ========================================
    print("--- Step 6: Monitoring Transaction ---")
    print(f"  Checking every {CHECK_INTERVAL}s for up to {MAX_WAIT//60} minutes...")
    print()

    start_time = time.time()
    initial_use = use_balance

    async with aiohttp.ClientSession() as s:
        async with aiohttp.ClientSession(headers=node_headers) as ns:
            while True:
                elapsed = time.time() - start_time
                if elapsed > MAX_WAIT:
                    print()
                    print(f"  TIMEOUT after {MAX_WAIT//60} minutes!")
                    print("  Transaction may still be pending in the mempool.")
                    print("  Check manually: https://explorer.ergoplatform.com/en/transactions/" + submitted_id)
                    break

                await asyncio.sleep(CHECK_INTERVAL)
                elapsed = time.time() - start_time
                mins = int(elapsed // 60)
                secs = int(elapsed % 60)

                # Check if TX is confirmed
                confirmed, confirmations = await check_tx_confirmed(s, submitted_id)

                # Check USE balance
                current_use = await check_wallet_use_balance(ns)

                if confirmed:
                    print(f"  [{mins}m{secs:02d}s] TX CONFIRMED! Confirmations: {confirmations}")
                    print(f"  USE balance: {current_use:.3f} USE (was {initial_use:.3f})")
                    gained = current_use - initial_use
                    if gained > 0:
                        print(f"  USE gained: +{gained:.3f} USE")
                    print()
                    print("  SWAP COMPLETE!")
                    break
                else:
                    print(f"  [{mins}m{secs:02d}s] Waiting... (USE balance: {current_use:.3f})")

                    # Also check if USE balance changed even before explorer confirms
                    if current_use > initial_use + 0.001:
                        print(f"  USE balance increased! Swap likely succeeded.")
                        print(f"  USE gained: +{current_use - initial_use:.3f} USE")
                        break


if __name__ == "__main__":
    asyncio.run(main())
