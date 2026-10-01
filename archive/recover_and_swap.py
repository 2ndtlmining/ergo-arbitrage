"""Recover stuck swap order ERG and build new swap with current contract."""
import asyncio
import aiohttp
import json
import os
import sys
import io

from dotenv import load_dotenv
load_dotenv()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

EXPLORER = "https://api.ergoplatform.com/api/v1"
NODE = os.getenv("ERGO_NODE_URL", "http://127.0.0.1:9053")
API_KEY = os.getenv("ERGO_NODE_API_KEY", "")

# Our stuck swap order box
STUCK_BOX_ID = "8a17681e3d9be078e3ee9856dde255b6cd6d80e480e0341c77e8b97492df8c2e"

# Recent working swap TX to get current contract template
RECENT_SWAP_TX = "2c25ed8af833728d245e23f99a64f37dda50d68711324fff304bae64d0b27afe"

# Pubkeys
OUR_PUBKEY = "02cec08d7fff81321b9207fc9ca006784c983c7a0d4b835e4349ba069b0c476afa"
RECENT_SWAP_PUBKEY = "03413945b4b8599af4c51d6e776464088f51f75bf93d8a6048c190ba0f09a63885"


async def main():
    node_headers = {"api_key": API_KEY, "Content-Type": "application/json"}

    async with aiohttp.ClientSession() as s:
        # ============================================
        # STEP 1: Recover stuck ERG
        # ============================================
        print("=" * 60)
        print("STEP 1: Recovering stuck swap order (1.001 ERG)")
        print("=" * 60)

        # Get the stuck box raw bytes from the node
        async with aiohttp.ClientSession(headers=node_headers) as ns:
            # First get our wallet address
            async with ns.get(f"{NODE}/wallet/addresses", timeout=aiohttp.ClientTimeout(total=10)) as r:
                addrs = await r.json()
                our_address = addrs[0]
                print(f"Our address: {our_address}")

            # Get the stuck box serialized bytes from explorer
            async with s.get(
                f"{EXPLORER}/boxes/{STUCK_BOX_ID}",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                stuck_box = await r.json()
                stuck_value = stuck_box["value"]
                print(f"Stuck box value: {stuck_value / 1e9:.4f} ERG")
                print(f"Stuck box spent: {stuck_box.get('spentTransactionId', 'NO')}")

            if stuck_box.get("spentTransactionId"):
                print("Box already spent - skip recovery")
            else:
                # Try to get raw bytes of the stuck box from the node
                async with ns.get(
                    f"{NODE}/utxo/byId/{STUCK_BOX_ID}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 200:
                        box_data = await r.json()
                        print(f"Box found on node")
                    else:
                        print(f"Box not found on node (HTTP {r.status})")

                # Get serialized box bytes
                async with ns.get(
                    f"{NODE}/utxo/byIdBinary/{STUCK_BOX_ID}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 200:
                        raw_data = await r.json()
                        raw_bytes = raw_data.get("bytes", "")
                        print(f"Got raw box bytes: {len(raw_bytes)} hex chars")
                    else:
                        body = await r.text()
                        print(f"Could not get raw bytes: HTTP {r.status} - {body[:200]}")
                        raw_bytes = None

                if raw_bytes:
                    miner_fee = 1100000
                    refund_value = stuck_value - miner_fee

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

                    print(f"Attempting refund: {refund_value / 1e9:.4f} ERG back to wallet")
                    async with ns.post(
                        f"{NODE}/wallet/transaction/send",
                        json=refund_tx,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as r:
                        body = await r.text()
                        if r.status == 200:
                            print(f"  REFUND SUCCESS! TX: {body.strip()}")
                        else:
                            print(f"  Refund failed: HTTP {r.status}")
                            print(f"  Error: {body[:500]}")

            print()

            # ============================================
            # STEP 2: Build new swap with current contract
            # ============================================
            print("=" * 60)
            print("STEP 2: Building new swap with current contract")
            print("=" * 60)

            # Get the current contract template from recent working swap
            async with s.get(
                f"{EXPLORER}/transactions/{RECENT_SWAP_TX}",
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                recent_tx = await r.json()

            # Find swap order output (has R4)
            swap_out = None
            for out in recent_tx["outputs"]:
                if out.get("additionalRegisters", {}).get("R4"):
                    swap_out = out
                    break

            if not swap_out:
                print("Could not find swap order in recent TX")
                return

            current_tree = swap_out["ergoTree"]
            print(f"Current contract: {current_tree[:40]}... ({len(current_tree)} chars)")

            # Replace pubkey
            new_tree = current_tree.replace(RECENT_SWAP_PUBKEY, OUR_PUBKEY)
            print(f"Pubkey replaced: {current_tree != new_tree}")

            # Convert to address
            async with ns.get(
                f"{NODE}/utils/ergoTreeToAddress/{new_tree}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    result = await r.json()
                    swap_address = result.get("address", "")
                    print(f"Swap address: {swap_address[:60]}...")
                else:
                    body = await r.text()
                    print(f"Address conversion failed: {body[:300]}")
                    return

            # Get fee info from recent TX
            batcher_address = None
            batcher_fee = 0
            proto_address = None
            proto_fee = 0
            for out in recent_tx["outputs"]:
                addr = out.get("address", "")
                if addr.startswith("9hMAkrPjRnT3"):  # Mew batcher
                    batcher_address = addr
                    batcher_fee = out["value"]
                elif addr.startswith("2iHkR7CWvD1R"):  # Protocol
                    proto_address = addr
                    proto_fee = out["value"]

            print(f"Batcher fee: {batcher_fee / 1e9:.4f} ERG")
            print(f"Protocol fee: {proto_fee / 1e9:.4f} ERG")
            print()

            # Build swap
            swap_erg = 1.0
            min_box = 0.001
            swap_value_nano = int((swap_erg + min_box) * 1e9)
            miner_fee_nano = 1100000

            total_needed = (swap_value_nano + batcher_fee + proto_fee + miner_fee_nano) / 1e9

            # Check balance
            async with ns.get(f"{NODE}/wallet/balances", timeout=aiohttp.ClientTimeout(total=10)) as r:
                bal = await r.json()
                erg_balance = bal.get("balance", 0) / 1e9

            print(f"=== NEW SWAP: {swap_erg} ERG -> SigUSD ===")
            print(f"  Swap order:    {swap_value_nano / 1e9:.4f} ERG")
            print(f"  Batcher fee:   {batcher_fee / 1e9:.4f} ERG")
            print(f"  Protocol fee:  {proto_fee / 1e9:.4f} ERG")
            print(f"  Miner fee:     {miner_fee_nano / 1e9:.4f} ERG")
            print(f"  Total cost:    {total_needed:.4f} ERG")
            print(f"  Balance:       {erg_balance:.4f} ERG")
            print(f"  Enough:        {'YES' if erg_balance >= total_needed else 'NO'}")
            print()

            if erg_balance < total_needed:
                print("ABORTED: Insufficient balance!")
                return

            # Get R4 from recent swap
            r4_value = swap_out.get("additionalRegisters", {}).get("R4", {}).get("serializedValue", "0e034d6577")

            tx_request = {
                "requests": [
                    {
                        "address": swap_address,
                        "value": swap_value_nano,
                        "assets": [],
                        "registers": {"R4": r4_value},
                    },
                    {
                        "address": batcher_address,
                        "value": batcher_fee,
                        "assets": [],
                        "registers": {},
                    },
                    {
                        "address": proto_address,
                        "value": proto_fee,
                        "assets": [],
                        "registers": {},
                    },
                ],
                "fee": miner_fee_nano,
                "inputsRaw": [],
                "dataInputsRaw": [],
            }

            print("=== SUBMITTING NEW SWAP ===")
            async with ns.post(
                f"{NODE}/wallet/transaction/send",
                json=tx_request,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                body = await r.text()
                if r.status == 200:
                    tx_id = body.strip().strip('"')
                    print(f"  SUCCESS! TX: {tx_id}")
                    print(f"  Explorer: https://explorer.ergoplatform.com/en/transactions/{tx_id}")
                    print(f"  Mew batcher should pick this up in ~1-5 minutes")
                else:
                    print(f"  FAILED: HTTP {r.status}")
                    print(f"  Error: {body[:500]}")


if __name__ == "__main__":
    asyncio.run(main())
