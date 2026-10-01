"""Execute a REAL 1 ERG -> SigUSD swap via Mew Finance batcher."""
import asyncio
import aiohttp
import json
import os
import sys
import io

from dotenv import load_dotenv
load_dotenv()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

TX_ID = "3da95cf36fd9e7a27f5383a9afd67d34f66e94c44364560af994143f698268cc"
EXPLORER = "https://api.ergoplatform.com/api/v1"
NODE = os.getenv("ERGO_NODE_URL", "http://127.0.0.1:9053")
API_KEY = os.getenv("ERGO_NODE_API_KEY", "")

USER_PUBKEY = "037a3274856b36096f041d983c57be309a5347259d6c10ce96ae52d7bf264c86c2"


async def main():
    node_headers = {"api_key": API_KEY, "Content-Type": "application/json"}

    async with aiohttp.ClientSession() as s:
        # 1. Get original swap TX
        async with s.get(
            f"{EXPLORER}/transactions/{TX_ID}",
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            tx = await r.json()

        swap_order = tx["outputs"][0]
        swap_ergo_tree = swap_order["ergoTree"]
        swap_regs = swap_order.get("additionalRegisters", {})
        batcher_address = tx["outputs"][1].get("address", "")
        batcher_fee_nano = tx["outputs"][1]["value"]
        proto_address = tx["outputs"][3].get("address", "")
        proto_fee_nano = tx["outputs"][3]["value"]

        print(f"Original swap: {swap_order['value']/1e9:.4f} ERG")
        print(f"Batcher: {batcher_address} ({batcher_fee_nano/1e9:.4f} ERG)")
        print(f"Protocol: {proto_address} ({proto_fee_nano/1e9:.4f} ERG)")
        print()

    # 2. Get our pubkey from node
    async with aiohttp.ClientSession(headers=node_headers) as ns:
        async with ns.get(f"{NODE}/wallet/boxes/unspent?minConfirmations=0&minInclusionHeight=0",
                          timeout=aiohttp.ClientTimeout(total=10)) as r:
            boxes = await r.json()
            our_tree = boxes[0].get("box", {}).get("ergoTree", "")
            our_pubkey = our_tree[6:]  # Skip "0008cd"

        print(f"Our pubkey: {our_pubkey}")
        print()

        # 3. Build new ErgoTree with our pubkey
        new_ergo_tree = swap_ergo_tree.replace(USER_PUBKEY, our_pubkey)
        print(f"ErgoTree pubkey replaced: {swap_ergo_tree != new_ergo_tree}")

        # 4. Convert ErgoTree to address
        async with ns.get(
            f"{NODE}/utils/ergoTreeToAddress/{new_ergo_tree}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status == 200:
                result = await r.json()
                swap_address = result.get("address", "")
                print(f"Swap order address: {swap_address[:60]}...")
            else:
                body = await r.text()
                print(f"Failed to convert ErgoTree: HTTP {r.status} - {body[:300]}")
                return
        print()

        # 5. Build the swap transaction
        swap_erg = 1.0
        min_box = 0.001
        swap_value_nano = int((swap_erg + min_box) * 1e9)
        miner_fee_nano = 1100000

        total_needed = (swap_value_nano + batcher_fee_nano + proto_fee_nano + miner_fee_nano) / 1e9

        # Check balance
        async with ns.get(f"{NODE}/wallet/balances", timeout=aiohttp.ClientTimeout(total=10)) as r:
            bal = await r.json()
            erg_balance = bal.get("balance", 0) / 1e9

        print(f"=== LIVE SWAP: {swap_erg} ERG -> SigUSD ===")
        print(f"  Swap order value:  {swap_value_nano/1e9:.4f} ERG")
        print(f"  Batcher fee:       {batcher_fee_nano/1e9:.4f} ERG")
        print(f"  Protocol fee:      {proto_fee_nano/1e9:.4f} ERG")
        print(f"  Miner fee:         {miner_fee_nano/1e9:.4f} ERG")
        print(f"  Total cost:        {total_needed:.4f} ERG")
        print(f"  Wallet balance:    {erg_balance:.4f} ERG")
        print(f"  Enough:            {'YES' if erg_balance >= total_needed else 'NO'}")
        print()

        if erg_balance < total_needed:
            print("ABORTED: Insufficient balance!")
            return

        tx_request = {
            "requests": [
                {
                    "address": swap_address,
                    "value": swap_value_nano,
                    "assets": [],
                    "registers": {
                        "R4": swap_regs.get("R4", {}).get("serializedValue", "0e034d6577"),
                    },
                },
                {
                    "address": batcher_address,
                    "value": batcher_fee_nano,
                    "assets": [],
                    "registers": {},
                },
                {
                    "address": proto_address,
                    "value": proto_fee_nano,
                    "assets": [],
                    "registers": {},
                },
            ],
            "fee": miner_fee_nano,
            "inputsRaw": [],
            "dataInputsRaw": [],
        }

        # SEND for real
        print("=== SUBMITTING TRANSACTION ===")
        async with ns.post(
            f"{NODE}/wallet/transaction/send",
            json=tx_request,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            body = await r.text()
            if r.status == 200:
                tx_id = body.strip().strip('"')
                print(f"  SUCCESS! Transaction submitted!")
                print(f"  TX ID: {tx_id}")
                print(f"  Explorer: https://explorer.ergoplatform.com/en/transactions/{tx_id}")
                print()
                print(f"  The Mew batcher should pick this up in ~1-5 minutes.")
                print(f"  You will receive SigUSD in your wallet.")
            else:
                print(f"  FAILED: HTTP {r.status}")
                print(f"  Error: {body[:500]}")


if __name__ == "__main__":
    asyncio.run(main())
