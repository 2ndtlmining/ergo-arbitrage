"""Execute a REAL SigUSD -> ERG swap via Crux Finance DEX (Spectrum pool) and monitor."""
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

# Swap amount (SigUSD has 2 decimals)
SWAP_SIGUSD = 1.0
SWAP_RAW = int(SWAP_SIGUSD * 100)  # 2 decimals

CHECK_INTERVAL = 30
MAX_WAIT = 600


async def get_wallet_info(ns):
    """Get wallet address and balances."""
    async with ns.get(f"{NODE}/wallet/addresses", timeout=aiohttp.ClientTimeout(total=10)) as r:
        addrs = await r.json()
        address = addrs[0]

    async with ns.get(f"{NODE}/wallet/balances", timeout=aiohttp.ClientTimeout(total=10)) as r:
        bal = await r.json()
        erg_balance = bal.get("balance", 0) / 1e9
        sigusd_balance = bal.get("assets", {}).get(SIGUSD_TOKEN, 0) / 100

    return address, erg_balance, sigusd_balance


async def get_quote(s, sigusd_raw):
    """Get quote for SigUSD -> ERG."""
    url = (
        f"{CRUX_API}/dex/quote"
        f"?given_token_id={SIGUSD_TOKEN}"
        f"&given_token_amount={sigusd_raw}"
        f"&requested_token_id={ERG_TOKEN}"
    )
    async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            body = await r.text()
            raise Exception(f"Quote failed: HTTP {r.status} - {body[:500]}")
        return await r.json()


async def build_swap_tx(s, address, sigusd_raw):
    """Build unsigned SigUSD -> ERG swap TX via Crux DEX."""
    url = (
        f"{CRUX_API}/dex/swap"
        f"?user_addresses={address}"
        f"&target_address={address}"
        f"&given_token_id={SIGUSD_TOKEN}"
        f"&given_token_amount={sigusd_raw}"
        f"&requested_token_id={ERG_TOKEN}"
        f"&fee_token=erg"
    )
    async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
        body = await r.text()
        if r.status != 200:
            raise Exception(f"Build swap TX failed: HTTP {r.status} - {body[:500]}")
        return json.loads(body)


async def sign_transaction(ns, unsigned_tx):
    """Sign using Ergo node wallet. Uses withPool endpoint to find all boxes."""
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
        async with ns.get(f"{NODE}/utxo/withPool/byIdBinary/{bid}", timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json()
                all_input_raw.append(data.get("bytes", ""))
            else:
                raise Exception(f"Cannot get raw bytes for input {bid}: HTTP {r.status}")

    all_data_raw = []
    for di in data_inputs:
        bid = di["boxId"]
        async with ns.get(f"{NODE}/utxo/withPool/byIdBinary/{bid}", timeout=aiohttp.ClientTimeout(total=10)) as r:
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

    async with ns.post(f"{NODE}/wallet/transaction/sign", json=sign_request, timeout=aiohttp.ClientTimeout(total=30)) as r:
        body = await r.text()
        if r.status != 200:
            raise Exception(f"Sign failed: HTTP {r.status} - {body[:500]}")
        return json.loads(body)


async def submit_transaction(ns, signed_tx):
    """Submit signed transaction."""
    async with ns.post(f"{NODE}/transactions", json=signed_tx, timeout=aiohttp.ClientTimeout(total=15)) as r:
        body = await r.text()
        if r.status != 200:
            raise Exception(f"Submit failed: HTTP {r.status} - {body[:500]}")
        return body.strip().strip('"')


async def main():
    node_headers = {"api_key": API_KEY, "Content-Type": "application/json"}

    print("=" * 60)
    print(f"SigUSD -> ERG Swap via Crux Finance DEX ({SWAP_SIGUSD} SigUSD)")
    print("=" * 60)
    print()

    # Step 1: Wallet
    print("--- Step 1: Wallet Info ---")
    async with aiohttp.ClientSession(headers=node_headers) as ns:
        address, erg_balance, sigusd_balance = await get_wallet_info(ns)
    print(f"  Address:       {address}")
    print(f"  ERG balance:   {erg_balance:.4f} ERG")
    print(f"  SigUSD balance:{sigusd_balance:.2f} SigUSD")
    print()

    if sigusd_balance < SWAP_SIGUSD:
        print(f"ABORTED: Need {SWAP_SIGUSD:.2f} SigUSD but only have {sigusd_balance:.2f}")
        return

    # Step 2: Quote
    print("--- Step 2: Get Quote ---")
    async with aiohttp.ClientSession() as s:
        quote = await get_quote(s, SWAP_RAW)

    erg_output_nano = quote.get("requested_amount", 0)
    erg_output = erg_output_nano / 1e9
    price_impact = quote.get("price_impact", 0)
    lp_fee = quote.get("lp_fee_percent", 0)
    source = quote.get("source", "?")
    pool_type = quote.get("details", {}).get("amm", {}).get("pool_type", "?")

    print(f"  Selling:       {SWAP_SIGUSD:.2f} SigUSD")
    print(f"  Receiving:     {erg_output:.6f} ERG")
    print(f"  Rate:          1 SigUSD = {erg_output / SWAP_SIGUSD:.4f} ERG")
    print(f"  Price impact:  {price_impact:.2f}%")
    print(f"  LP fee:        {lp_fee}%")
    print(f"  Source:        {source} ({pool_type})")
    print()

    # Step 3: Build TX
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
    print(f"  Expected output: {expected_output / 1e9:.6f} ERG")
    print(f"  Service fee:     {swap_fee / 1e9:.6f} ERG")

    inputs = unsigned_tx.get("inputs", [])
    outputs = unsigned_tx.get("outputs", [])
    print(f"  TX inputs:       {len(inputs)}")
    print(f"  TX outputs:      {len(outputs)}")
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

                async with ns.get(f"{NODE}/wallet/balances", timeout=aiohttp.ClientTimeout(total=10)) as r:
                    bal = await r.json()
                    current_erg = bal.get("balance", 0) / 1e9
                    current_sigusd = bal.get("assets", {}).get(SIGUSD_TOKEN, 0) / 100

                erg_change = current_erg - initial_erg
                sigusd_change = current_sigusd - initial_sigusd

                async with s.get(f"{EXPLORER}/transactions/{submitted_id}", timeout=aiohttp.ClientTimeout(total=15)) as r:
                    confirmed = r.status == 200
                    confs = 0
                    if confirmed:
                        tx_data = await r.json()
                        confs = tx_data.get("numConfirmations", 0)

                if confirmed and confs > 0:
                    print(f"  [{mins}m{secs:02d}s] TX CONFIRMED! Confirmations: {confs}")
                    print(f"  ERG:    {current_erg:.4f} (change: {erg_change:+.4f})")
                    print(f"  SigUSD: {current_sigusd:.2f} (change: {sigusd_change:+.2f})")
                    print()
                    print("  SWAP COMPLETE!")
                    break
                else:
                    print(f"  [{mins}m{secs:02d}s] Waiting... ERG={current_erg:.4f} ({erg_change:+.4f}) SigUSD={current_sigusd:.2f} ({sigusd_change:+.2f})")

                    if erg_change > 0.1 and sigusd_change < -0.5:
                        print(f"  Balance changed! Swap likely succeeded.")
                        break


if __name__ == "__main__":
    asyncio.run(main())
