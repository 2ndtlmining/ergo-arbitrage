"""Execute a SigmaUSD Bank redeem (SigUSD -> ERG) via direct contract interaction."""
import asyncio
import aiohttp
import json
import os
import sys
import io
import time

from dotenv import load_dotenv
load_dotenv()

from exchanges.sigmausd import BankState, PROTOCOL_FEE_PERCENT, ui_fee_nanoerg, quote_redeem_sigusd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

EXPLORER = "https://api.ergoplatform.com/api/v1"
NODE = os.getenv("ERGO_NODE_URL", "http://127.0.0.1:9053")
API_KEY = os.getenv("ERGO_NODE_API_KEY", "")

BANK_NFT = "7d672d1def471720ca5782fd6473e47e796d9ac0c138d9911346f118b2f6d9d9"
SIGUSD_TOKEN = "03faf2cb329f2e90d6d23b58d91bbb6c046aa143261cc21f52fbe2824bfcbf04"
SIGRSV_TOKEN = "003bd19d0187117f130b62e1bcab0939929ff5c7709f843c5c4dd158949285d0"
ORACLE_NFT = "011d3364de07e5a26f0c4eef0852cddb387039a921b7154ef3cab22c6eda887f"
UI_FEE_ADDRESS = "9g2FAxryPZ98qH6W5E3qyVYj2kyqgUE7HjfA1CdBT25FJWewpav"
MINER_FEE = 1100000  # 0.0011 ERG (min 0.001 ERG per box)
RECEIPT_BOX_VALUE = 1000000  # 0.001 ERG minimum for receipt box
FEE_ERGO_TREE = ("1005040004000e36100204a00b08cd0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
                 "ea02d192a39a8cc7a701730073011001020402d19683030193a38cc7b2a57300000193c2b2a573010074730273"
                 "03830108cdeeac93b1a57304")

REDEEM_SIGUSD = 1.0
REDEEM_CENTS = int(REDEEM_SIGUSD * 100)

CHECK_INTERVAL = 30
MAX_WAIT = 600


def encode_slong(val):
    """Encode integer as Ergo SLong (type 0x05 + ZigZag VLQ)."""
    zz = (val * 2) if val >= 0 else ((-val) * 2 - 1)
    vlq = []
    while zz > 0x7f:
        vlq.append(0x80 | (zz & 0x7f))
        zz >>= 7
    vlq.append(zz & 0x7f)
    return "05" + "".join(f"{b:02x}" for b in vlq)


async def main():
    node_headers = {"api_key": API_KEY, "Content-Type": "application/json"}

    print("=" * 60)
    print(f"SigmaUSD Bank Redeem: {REDEEM_SIGUSD} SigUSD -> ERG")
    print("=" * 60)
    print()

    # --- Fetch all state ---
    print("--- Step 1: Fetch State ---")
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{EXPLORER}/boxes/unspent/byTokenId/{BANK_NFT}?limit=1", timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json()
            bank_box = (data.get("items", data) if isinstance(data, dict) else data)[0]

        async with s.get(f"{EXPLORER}/boxes/unspent/byTokenId/{ORACLE_NFT}?limit=1", timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json()
            oracle_box = (data.get("items", data) if isinstance(data, dict) else data)[0]

    # Parse bank
    bank_erg = bank_box["value"]
    bank_sigusd = bank_sigrsv = 0
    for a in bank_box.get("assets", []):
        if a["tokenId"] == SIGUSD_TOKEN:
            bank_sigusd = a["amount"]
        elif a["tokenId"] == SIGRSV_TOKEN:
            bank_sigrsv = a["amount"]

    regs = bank_box.get("additionalRegisters", {})
    sigusd_circ = int(regs["R4"].get("renderedValue", "0"))
    sigrsv_circ = int(regs["R5"].get("renderedValue", "0"))
    oracle_rate = int(oracle_box["additionalRegisters"]["R4"].get("renderedValue", "0"))

    rr = (bank_erg / oracle_rate) / (sigusd_circ / 100) * 100 if sigusd_circ > 0 else 0

    print(f"  Bank ERG:    {bank_erg / 1e9:.4f}")
    print(f"  SigUSD circ: {sigusd_circ / 100:.2f}")
    print(f"  Oracle:      1 SigUSD = {oracle_rate / 1e9:.6f} ERG")
    print(f"  RR:          {rr:.0f}% (SigUSD redeem has no RR limit)")
    print()

    # --- Wallet ---
    print("--- Step 2: Wallet ---")
    async with aiohttp.ClientSession(headers=node_headers) as ns:
        async with ns.get(f"{NODE}/wallet/balances", timeout=aiohttp.ClientTimeout(total=10)) as r:
            bal = await r.json()
            erg_balance = bal.get("balance", 0) / 1e9
            sigusd_balance = bal.get("assets", {}).get(SIGUSD_TOKEN, 0) / 100

        async with ns.get(f"{NODE}/wallet/boxes/unspent?minConfirmations=0&minInclusionHeight=0", timeout=aiohttp.ClientTimeout(total=10)) as r:
            wallet_boxes = await r.json()

        async with ns.get(f"{NODE}/info", timeout=aiohttp.ClientTimeout(total=10)) as r:
            current_height = (await r.json()).get("fullHeight", 0)

    print(f"  ERG:     {erg_balance:.4f}")
    print(f"  SigUSD:  {sigusd_balance:.2f}")
    print(f"  Height:  {current_height}")
    print()

    if sigusd_balance < REDEEM_SIGUSD:
        print(f"ABORTED: Need {REDEEM_SIGUSD:.2f} SigUSD, have {sigusd_balance:.2f}")
        return

    # Find user's SigUSD box
    user_box = None
    for wb in wallet_boxes:
        box = wb.get("box", {})
        for a in box.get("assets", []):
            if a.get("tokenId") == SIGUSD_TOKEN:
                user_box = box
                break
        if user_box:
            break

    our_ergo_tree = wallet_boxes[0]["box"]["ergoTree"]

    # --- Calculate (must match contract integer math exactly) ---
    print("--- Step 3: Calculate ---")
    # Shared contract-exact math (exchanges/sigmausd.py)
    state = BankState(bank_erg_nano=bank_erg, sigusd_circ_cents=sigusd_circ, oracle_r4=oracle_rate)
    sc_nominal_price = state.nominal_price()
    br_delta_expected = sc_nominal_price * (-REDEEM_CENTS)
    actual_fee = abs(br_delta_expected) * PROTOCOL_FEE_PERCENT // 100
    bc_delta = -(br_delta_expected + actual_fee)  # positive = ERG leaving bank
    ui_fee = ui_fee_nanoerg(bc_delta)
    user_receives = quote_redeem_sigusd(state, REDEEM_CENTS)  # miner fee deducted from user's input box value
    assert user_receives == bc_delta - ui_fee

    gross_erg = sc_nominal_price * REDEEM_CENTS
    print(f"  Gross:    {gross_erg / 1e9:.6f} ERG")
    print(f"  -2% fee:  {actual_fee / 1e9:.6f}")
    print(f"  bc_delta: {bc_delta} nanoERG")
    print(f"  -UI fee:  {ui_fee / 1e9:.6f}")
    print(f"  Net:      {user_receives / 1e9:.6f} ERG")
    print()

    # --- Build TX ---
    print("--- Step 4: Build TX ---")
    new_bank_erg = bank_erg - bc_delta
    new_bank_sigusd = bank_sigusd + REDEEM_CENTS
    new_sigusd_circ = sigusd_circ - REDEEM_CENTS

    # User change
    user_sigusd_raw = 0
    user_other_assets = []
    for a in user_box.get("assets", []):
        if a["tokenId"] == SIGUSD_TOKEN:
            user_sigusd_raw = a["amount"]
        else:
            user_other_assets.append(a)

    user_change_sigusd = user_sigusd_raw - REDEEM_CENTS
    user_change_erg = user_box["value"] - MINER_FEE - RECEIPT_BOX_VALUE
    user_total_erg = user_receives + user_change_erg

    payout_assets = []
    if user_change_sigusd > 0:
        payout_assets.append({"tokenId": SIGUSD_TOKEN, "amount": user_change_sigusd})
    payout_assets.extend(user_other_assets)

    # Get raw bytes
    async with aiohttp.ClientSession(headers=node_headers) as ns:
        async with ns.get(f"{NODE}/utxo/withPool/byIdBinary/{bank_box['boxId']}", timeout=aiohttp.ClientTimeout(total=10)) as r:
            bank_raw = (await r.json()).get("bytes", "")
        async with ns.get(f"{NODE}/utxo/withPool/byIdBinary/{oracle_box['boxId']}", timeout=aiohttp.ClientTimeout(total=10)) as r:
            oracle_raw = (await r.json()).get("bytes", "")
        async with ns.get(f"{NODE}/utxo/withPool/byIdBinary/{user_box['boxId']}", timeout=aiohttp.ClientTimeout(total=10)) as r:
            user_raw = (await r.json()).get("bytes", "")

        # Get UI fee ergoTree (P2PK = 0008cd + pubkey)
        async with ns.get(f"{NODE}/utils/addressToRaw/{UI_FEE_ADDRESS}", timeout=aiohttp.ClientTimeout(total=10)) as r:
            ui_pubkey = (await r.json()).get("raw", "")
            ui_ergo_tree = "0008cd" + ui_pubkey

    # Use the original bank ErgoTree as-is
    bank_tree_out = bank_box["ergoTree"]

    print(f"  Bank tree: {bank_tree_out[:30]}...")
    print(f"  New bank ERG:   {new_bank_erg / 1e9:.4f}")
    print(f"  User total ERG: {user_total_erg / 1e9:.6f}")

    # Build sign request
    sign_request = {
        "tx": {
            "inputs": [
                {"boxId": bank_box["boxId"], "extension": {}},
                {"boxId": user_box["boxId"], "extension": {}},
            ],
            "dataInputs": [
                {"boxId": oracle_box["boxId"]},
            ],
            "outputs": [
                # Output 0: Updated bank box
                {
                    "value": new_bank_erg,
                    "ergoTree": bank_tree_out,
                    "assets": [
                        {"tokenId": SIGUSD_TOKEN, "amount": new_bank_sigusd},
                        {"tokenId": SIGRSV_TOKEN, "amount": bank_sigrsv},
                        {"tokenId": BANK_NFT, "amount": 1},
                    ],
                    "additionalRegisters": {
                        "R4": encode_slong(new_sigusd_circ),
                        "R5": encode_slong(sigrsv_circ),
                    },
                    "creationHeight": current_height,
                },
                # Output 1: Receipt box (required by EIP-15 contract)
                {
                    "value": RECEIPT_BOX_VALUE,
                    "ergoTree": our_ergo_tree,
                    "assets": [],
                    "additionalRegisters": {
                        "R4": encode_slong(-REDEEM_CENTS),   # scCircDelta (negative = redeem)
                        "R5": encode_slong(-bc_delta),        # bcReserveDelta (negative = ERG leaves bank)
                    },
                    "creationHeight": current_height,
                },
                # Output 2: User payout
                {
                    "value": user_total_erg,
                    "ergoTree": our_ergo_tree,
                    "assets": payout_assets,
                    "additionalRegisters": {},
                    "creationHeight": current_height,
                },
                # Output 3: UI fee
                {
                    "value": ui_fee,
                    "ergoTree": ui_ergo_tree,
                    "assets": [],
                    "additionalRegisters": {},
                    "creationHeight": current_height,
                },
                # Output 4: Miner fee (explicit output in Ergo)
                {
                    "value": MINER_FEE,
                    "ergoTree": FEE_ERGO_TREE,
                    "assets": [],
                    "additionalRegisters": {},
                    "creationHeight": current_height,
                },
            ],
        },
        "inputsRaw": [bank_raw, user_raw],
        "dataInputsRaw": [oracle_raw],
        "secrets": {},
    }

    # Verify contract math match
    expected_bc_delta_with_fee = br_delta_expected + actual_fee
    print(f"  bcReserveDelta match: {(-bc_delta) == expected_bc_delta_with_fee}")

    # --- Sign & Submit ---
    print()
    print("--- Step 5: Sign & Submit ---")
    async with aiohttp.ClientSession(headers=node_headers) as ns:
        async with ns.post(f"{NODE}/wallet/transaction/sign", json=sign_request, timeout=aiohttp.ClientTimeout(total=30)) as r:
            body = await r.text()
            if r.status != 200:
                print(f"  SIGN FAILED: HTTP {r.status}")
                print(f"  {body[:800]}")
                return
            signed_tx = json.loads(body)
            tx_id = signed_tx.get("id", "?")
            print(f"  Signed: {tx_id}")

        async with ns.post(f"{NODE}/transactions", json=signed_tx, timeout=aiohttp.ClientTimeout(total=30)) as r:
            body = await r.text()
            if r.status != 200:
                print(f"  SUBMIT FAILED: HTTP {r.status}")
                print(f"  {body[:800]}")
                return
            print(f"  Submitted!")
            print(f"  https://explorer.ergoplatform.com/en/transactions/{tx_id}")

    # --- Monitor ---
    print()
    print("--- Step 6: Monitoring ---")
    start_time = time.time()

    async with aiohttp.ClientSession() as s:
        async with aiohttp.ClientSession(headers=node_headers) as ns:
            while True:
                elapsed = time.time() - start_time
                if elapsed > MAX_WAIT:
                    print(f"\n  TIMEOUT. Check explorer.")
                    break

                await asyncio.sleep(CHECK_INTERVAL)
                elapsed = time.time() - start_time
                mins = int(elapsed // 60)
                secs = int(elapsed % 60)

                async with ns.get(f"{NODE}/wallet/balances", timeout=aiohttp.ClientTimeout(total=10)) as r:
                    bal = await r.json()
                    cur_erg = bal.get("balance", 0) / 1e9
                    cur_sigusd = bal.get("assets", {}).get(SIGUSD_TOKEN, 0) / 100

                async with s.get(f"{EXPLORER}/transactions/{tx_id}", timeout=aiohttp.ClientTimeout(total=30)) as r:
                    confirmed = r.status == 200
                    confs = (await r.json()).get("numConfirmations", 0) if confirmed else 0

                erg_chg = cur_erg - erg_balance
                sigusd_chg = cur_sigusd - sigusd_balance

                if confirmed and confs > 0:
                    print(f"  [{mins}m{secs:02d}s] CONFIRMED! ({confs} confs)")
                    print(f"  ERG:    {cur_erg:.4f} ({erg_chg:+.4f})")
                    print(f"  SigUSD: {cur_sigusd:.2f} ({sigusd_chg:+.2f})")
                    print()
                    print("  REDEEM COMPLETE!")
                    break
                else:
                    print(f"  [{mins}m{secs:02d}s] Waiting... ERG={cur_erg:.4f} ({erg_chg:+.4f}) SigUSD={cur_sigusd:.2f} ({sigusd_chg:+.2f})")
                    if erg_chg > 0.5 and sigusd_chg < -0.5:
                        print("  Balance changed! Likely succeeded.")
                        break


if __name__ == "__main__":
    asyncio.run(main())
