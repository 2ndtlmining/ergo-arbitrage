"""Execute a REAL ERG -> SigUSD swap via Spectrum pool (through Crux Finance API) and monitor for completion."""
import asyncio
import aiohttp
import json
import os
import sys
import io
import time

from dotenv import load_dotenv
load_dotenv()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Configuration
EXPLORER = "https://api.ergoplatform.com/api/v1"
CRUX_API = "https://api.cruxfinance.io"
NODE = os.getenv("ERGO_NODE_URL", "http://127.0.0.1:9053")
API_KEY = os.getenv("ERGO_NODE_API_KEY", "")

SIGUSD_TOKEN = "03faf2cb329f2e90d6d23b58d91bbb6c046aa143261cc21f52fbe2824bfcbf04"
ERG_TOKEN = "0000000000000000000000000000000000000000000000000000000000000000"

# Swap amount in ERG
SWAP_ERG = 1.0  # Small test amount
SWAP_RAW = int(SWAP_ERG * 1_000_000_000)  # nanoERG

# Set to True to skip confirmation prompt and execute immediately
AUTO_EXECUTE = False

# Monitoring settings
CHECK_INTERVAL = 30
MAX_WAIT = 600


async def get_wallet_info(ns):
    """Get wallet address and balances."""
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
        sigusd_balance = assets.get(SIGUSD_TOKEN, 0) / 100  # 2 decimals

    return address, erg_balance, sigusd_balance


async def get_quote(s, erg_raw_amount):
    """Get a quote for ERG -> SigUSD swap from Crux/Spectrum LP."""
    url = (
        f"{CRUX_API}/dex/quote"
        f"?given_token_id={ERG_TOKEN}"
        f"&given_token_amount={erg_raw_amount}"
        f"&requested_token_id={SIGUSD_TOKEN}"
    )
    async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            body = await r.text()
            raise Exception(f"Quote failed: HTTP {r.status} - {body[:500]}")
        return await r.json()


async def build_swap_tx(s, address, erg_raw_amount):
    """Build unsigned ERG -> SigUSD swap TX via Crux/Spectrum LP."""
    url = (
        f"{CRUX_API}/dex/swap"
        f"?user_addresses={address}"
        f"&target_address={address}"
        f"&given_token_id={ERG_TOKEN}"
        f"&given_token_amount={erg_raw_amount}"
        f"&requested_token_id={SIGUSD_TOKEN}"
        f"&fee_token=erg"
    )
    async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
        body = await r.text()
        if r.status != 200:
            raise Exception(f"Build swap TX failed: HTTP {r.status} - {body[:500]}")
        return json.loads(body)


async def sign_transaction(ns, unsigned_tx):
    """Sign an unsigned transaction using the Ergo node wallet."""
    inputs = unsigned_tx.get("inputs", [])
    data_inputs = unsigned_tx.get("dataInputs", [])

    stripped_tx = {
        "inputs": [
            {"boxId": inp["boxId"], "extension": inp.get("extension", {})}
            for inp in inputs
        ],
        "dataInputs": [
            {"boxId": di["boxId"]}
            for di in data_inputs
        ],
        "outputs": unsigned_tx["outputs"],
    }

    all_input_raw = []
    for inp in inputs:
        bid = inp["boxId"]
        async with ns.get(
            f"{NODE}/utxo/withPool/byIdBinary/{bid}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status == 200:
                data = await r.json()
                all_input_raw.append(data.get("bytes", ""))
            else:
                raise Exception(f"Cannot get raw bytes for input {bid}: HTTP {r.status}")

    all_data_raw = []
    for di in data_inputs:
        bid = di["boxId"]
        async with ns.get(
            f"{NODE}/utxo/withPool/byIdBinary/{bid}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status == 200:
                data = await r.json()
                all_data_raw.append(data.get("bytes", ""))
            else:
                raise Exception(f"Cannot get raw bytes for data input {bid}: HTTP {r.status}")

    sign_request = {
        "tx": stripped_tx,
        "inputsRaw": all_input_raw,
        "dataInputsRaw": all_data_raw,
        "secrets": {},
    }

    async with ns.post(
        f"{NODE}/wallet/transaction/sign",
        json=sign_request,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as r:
        body = await r.text()
        if r.status != 200:
            raise Exception(f"Sign failed: HTTP {r.status} - {body[:500]}")
        return json.loads(body)


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
    node_headers = {"api_key": API_KEY, "Content-Type": "application/json"}

    print("=" * 60)
    print("ERG -> SigUSD Swap via Spectrum Pool (Crux Finance API)")
    print("=" * 60)
    print()

    # Step 1: Wallet info
    print("--- Step 1: Wallet Info ---")
    async with aiohttp.ClientSession(headers=node_headers) as ns:
        address, erg_balance, sigusd_balance = await get_wallet_info(ns)

    print(f"  Address:       {address}")
    print(f"  ERG balance:   {erg_balance:.4f} ERG")
    print(f"  SigUSD balance: {sigusd_balance:.2f} SigUSD")
    print()

    if erg_balance < SWAP_ERG + 1.0:  # Need extra for service fee
        print(f"ABORTED: Need {SWAP_ERG + 1.0:.4f} ERG (swap + fees) but only have {erg_balance:.4f} ERG")
        return

    # Step 2: Get quote
    print("--- Step 2: Get Quote ---")
    async with aiohttp.ClientSession() as s:
        quote = await get_quote(s, SWAP_RAW)

    sigusd_output_raw = quote.get("requested_amount", 0)
    sigusd_output = sigusd_output_raw / 100  # 2 decimals
    price_impact = quote.get("price_impact", 0)
    lp_fee = quote.get("lp_fee_percent", 0)
    details = quote.get("details", {}).get("amm", {})
    pool_id = details.get("pool_id", "?")
    pool_type = details.get("pool_type", "?")

    print(f"  Selling:       {SWAP_ERG:.4f} ERG")
    print(f"  Receiving:     {sigusd_output:.2f} SigUSD")
    if sigusd_output > 0:
        print(f"  Rate:          1 ERG = {sigusd_output/SWAP_ERG:.4f} SigUSD")
    print(f"  Price impact:  {price_impact:.2f}%")
    print(f"  LP fee:        {lp_fee}%")
    print(f"  Pool type:     {pool_type}")
    print(f"  Pool:          {pool_id[:40]}...")
    print()

    if price_impact > 5.0:
        print(f"WARNING: Price impact {price_impact:.2f}% is very high! Pool may have low liquidity.")
        print()

    # Confirmation gate
    if not AUTO_EXECUTE:
        print("Quote received. To proceed with swap, set AUTO_EXECUTE = True and re-run.")
        return

    # Step 3: Build unsigned TX
    print("--- Step 3: Build Swap TX ---")
    async with aiohttp.ClientSession() as s:
        try:
            result = await build_swap_tx(s, address, SWAP_RAW)
        except Exception as e:
            print(f"  ERROR: {e}")
            return

    unsigned_tx = result.get("unsigned_tx", result)
    expected_output = result.get("expected_output", 0)
    swap_fee = result.get("fee_amount", 0)
    print(f"  Expected output: {expected_output / 100:.2f} SigUSD")
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
            signed_tx = await sign_transaction(ns, unsigned_tx)
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
    initial_sigusd = sigusd_balance

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

                async with ns.get(
                    f"{NODE}/wallet/balances",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    bal = await r.json()
                    current_erg = bal.get("balance", 0) / 1e9
                    current_sigusd = bal.get("assets", {}).get(SIGUSD_TOKEN, 0) / 100

                erg_change = current_erg - initial_erg
                sigusd_change = current_sigusd - initial_sigusd

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
                    print(f"  ERG:    {current_erg:.4f} (change: {erg_change:+.4f})")
                    print(f"  SigUSD: {current_sigusd:.2f} (change: {sigusd_change:+.2f})")
                    print()
                    print("  SWAP COMPLETE!")
                    break
                else:
                    print(f"  [{mins}m{secs:02d}s] Waiting... ERG={current_erg:.4f} ({erg_change:+.4f}) SigUSD={current_sigusd:.2f} ({sigusd_change:+.2f})")

                    # ERG goes down, SigUSD goes up
                    if erg_change < -0.001 and sigusd_change > 0.001:
                        print(f"  Balance changed! Swap likely succeeded.")
                        break


if __name__ == "__main__":
    asyncio.run(main())
