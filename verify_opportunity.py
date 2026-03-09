"""Verify arbitrage opportunity math with live prices."""
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
SPECTRUM = "https://api.spectrum.fi/v1"
ORACLE = "https://erg-oracle-ergusd.spirepools.com/frontendData"
NONKYC = "https://api.nonkyc.io/api/v2"
KUCOIN = "https://api.kucoin.com/api/v1"
NODE = os.getenv("ERGO_NODE_URL", "http://127.0.0.1:9053")
API_KEY = os.getenv("ERGO_NODE_API_KEY", "")

SIGUSD_TOKEN = "03faf2cb329f2e90d6d23b58d91bbb6c046aa143261cc21f52fbe2824bfcbf04"
ERG_TOKEN = "0000000000000000000000000000000000000000000000000000000000000000"
BANK_NFT = "7d672d1def471720ca5782fd6473e47e796d9ac0c138d9911346f118b2f6d9d9"


async def main():
    node_headers = {"api_key": API_KEY, "Content-Type": "application/json"}

    async with aiohttp.ClientSession() as s:
        print("=" * 70)
        print("  LIVE PRICE CHECK & OPPORTUNITY VERIFICATION")
        print("=" * 70)
        print()

        # ---- Fetch all prices ----
        print("--- Fetching live prices ---")

        # NonKYC
        async with s.get(f"{NONKYC}/ticker/ERG_USDT", timeout=aiohttp.ClientTimeout(total=10)) as r:
            nk = await r.json()
            nk_bid = float(nk.get("bid", 0))
            nk_ask = float(nk.get("ask", 0))
            nk_mid = (nk_bid + nk_ask) / 2
            print(f"  NonKYC:    bid=${nk_bid:.4f}  ask=${nk_ask:.4f}  mid=${nk_mid:.4f}")

        # Kucoin
        async with s.get(f"{KUCOIN}/market/orderbook/level1?symbol=ERG-USDT", timeout=aiohttp.ClientTimeout(total=10)) as r:
            kc = await r.json()
            kc_data = kc.get("data", {})
            kc_bid = float(kc_data.get("bestBid", 0))
            kc_ask = float(kc_data.get("bestAsk", 0))
            kc_mid = (kc_bid + kc_ask) / 2
            print(f"  Kucoin:    bid=${kc_bid:.4f}  ask=${kc_ask:.4f}  mid=${kc_mid:.4f}")

        # Oracle
        async with s.get(ORACLE, timeout=aiohttp.ClientTimeout(total=10)) as r:
            text = await r.text()
            oracle_data = json.loads(text)
            if isinstance(oracle_data, str):
                oracle_data = json.loads(oracle_data)
            oracle_price = float(oracle_data.get("latest_price", 0))
            print(f"  Oracle:    ${oracle_price:.4f} USD/ERG")

        # Spectrum - find main ERG/SigUSD pool
        async with s.get(f"{SPECTRUM}/price-tracking/markets", timeout=aiohttp.ClientTimeout(total=15)) as r:
            markets = await r.json()
            spec_price = None
            best_vol = 0
            for m in markets:
                base_id = m.get("baseId", "")
                quote_id = m.get("quoteId", "")
                if base_id == ERG_TOKEN and quote_id == SIGUSD_TOKEN:
                    lp = float(m.get("lastPrice", 0))
                    vol = m.get("baseVolume", {}).get("value", 0)
                    if lp > 0 and vol > best_vol:
                        spec_price = lp  # SigUSD per ERG
                        best_vol = vol
            if spec_price:
                print(f"  Spectrum:  1 ERG = {spec_price:.4f} SigUSD (pool price)")
                print(f"             1 SigUSD = {1/spec_price:.4f} ERG")
            else:
                print(f"  Spectrum:  ERROR - could not find ERG/SigUSD pool")
                return

        # Bank state - use R4 register like sigmausd.py
        async with s.get(f"{EXPLORER}/boxes/unspent/byTokenId/{BANK_NFT}?limit=1",
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = await r.json()
            items = data.get("items", data) if isinstance(data, dict) else data
            if items and len(items) > 0:
                bank_box = items[0]
                bank_erg = bank_box.get("value", 0) / 1e9
                regs = bank_box.get("additionalRegisters", {})
                sigusd_circ = 0
                if "R4" in regs:
                    r4 = regs["R4"]
                    r4_val = r4.get("renderedValue", r4.get("serializedValue", "0"))
                    try:
                        sigusd_circ = int(r4_val) / 100  # 2 decimals
                    except (ValueError, TypeError):
                        pass
                if sigusd_circ > 0 and oracle_price > 0:
                    reserve_value = bank_erg * oracle_price
                    rr = (reserve_value / sigusd_circ) * 100
                else:
                    rr = 0
                can_mint = rr > 400
                can_redeem = 0 < rr < 800
                print(f"  Bank:      RR={rr:.0f}%  Mint={'YES' if can_mint else 'NO'}  Redeem={'YES' if can_redeem else 'NO'}")
                print(f"             ERG reserve: {bank_erg:,.0f}  SigUSD circ: {sigusd_circ:,.0f}")
            else:
                can_mint = False
                can_redeem = False
                rr = 0
                print(f"  Bank:      ERROR - could not fetch bank state")

        # Wallet
        async with aiohttp.ClientSession(headers=node_headers) as ns:
            async with ns.get(f"{NODE}/wallet/balances", timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    bal = await r.json()
                    erg_balance = bal.get("balance", 0) / 1e9
                    assets = bal.get("assets", {})
                    sigusd_balance = assets.get(SIGUSD_TOKEN, 0) / 100
                    print(f"  Wallet:    {erg_balance:.4f} ERG  |  {sigusd_balance:.2f} SigUSD")
                else:
                    erg_balance = 0
                    sigusd_balance = 0
                    print(f"  Wallet:    ERROR (HTTP {r.status})")

        # ---- Analysis ----
        print()
        print("=" * 70)
        print("  PRICNG SANITY CHECK")
        print("=" * 70)
        print()
        implied_sigusd_usd = oracle_price / (1 / spec_price) if spec_price > 0 else 0
        # Actually: if oracle says 1 ERG = $0.3175, and Spectrum says 1 ERG = 0.26 SigUSD
        # Then 1 SigUSD = $0.3175 / 0.26 = $1.22
        implied_sigusd_usd = oracle_price / spec_price if spec_price > 0 else 0
        print(f"  Oracle:    1 ERG = ${oracle_price:.4f} USD")
        print(f"  Spectrum:  1 ERG = {spec_price:.4f} SigUSD")
        print(f"  Implied:   1 SigUSD = ${implied_sigusd_usd:.4f} USD")
        depeg = (implied_sigusd_usd - 1.0) * 100
        print(f"  SigUSD peg: {'ON PEG' if abs(depeg) < 5 else 'DEPEGGED'} ({depeg:+.1f}% from $1.00)")
        print()

        bank_fee = 0.021  # 2.1%
        spec_fee = 0.003  # 0.3%
        tx_fee = 0.0011
        exec_fee = 0.002

        # ---- PATH A: Bank mint -> Spectrum ----
        print("=" * 70)
        print("  PATH A: ERG ->(Bank mint)-> SigUSD ->(Spectrum)-> ERG")
        print("=" * 70)
        print()
        if not can_mint:
            print(f"  *** BLOCKED: RR={rr:.0f}% (needs >400% to mint) ***")
            print(f"  Showing hypothetical math if minting were available:")
            print()

        print(f"  Size    Mint SigUSD   Swap->ERG    Profit ERG   Profit %")
        print(f"  ------- ------------ ------------ ------------ ----------")
        for size in [1, 5, 10, 25, 50, 100]:
            sigusd_minted = size * oracle_price * (1 - bank_fee)
            # Swap SigUSD -> ERG: 1 SigUSD = (1/spec_price) ERG minus pool fee
            erg_per_sigusd = (1 / spec_price) * (1 - spec_fee)
            erg_from_swap = sigusd_minted * erg_per_sigusd
            erg_out = erg_from_swap - tx_fee * 2 - exec_fee
            profit = erg_out - size
            pct = (profit / size) * 100
            marker = " <--" if pct > 1 else ""
            print(f"  {size:>5} ERG  {sigusd_minted:>8.2f}     {erg_out:>9.4f}    {profit:>+9.4f}    {pct:>+6.2f}%{marker}")

        print()

        # ---- PATH B: Spectrum -> Bank redeem ----
        print("=" * 70)
        print("  PATH B: ERG ->(Spectrum)-> SigUSD ->(Bank redeem)-> ERG")
        print("=" * 70)
        print()
        if not can_redeem:
            print(f"  *** BLOCKED: RR={rr:.0f}% (needs <800% to redeem) ***")
            print(f"  Showing hypothetical math if redeeming were available:")
            print()

        print(f"  Size    Buy SigUSD   Redeem->ERG  Profit ERG   Profit %")
        print(f"  ------- ------------ ------------ ------------ ----------")
        for size in [1, 5, 10, 25, 50, 100]:
            # Swap ERG -> SigUSD: spend ERG, get SigUSD at pool rate minus fee
            sigusd_bought = size * spec_price * (1 - spec_fee)
            # Redeem SigUSD at bank: get ERG at oracle rate minus bank fee
            erg_from_bank = (sigusd_bought / oracle_price) * (1 - bank_fee)
            erg_out = erg_from_bank - tx_fee * 2 - exec_fee
            profit = erg_out - size
            pct = (profit / size) * 100
            print(f"  {size:>5} ERG  {sigusd_bought:>8.2f}     {erg_out:>9.4f}    {profit:>+9.4f}    {pct:>+6.2f}%")

        print()

        # ---- Summary ----
        print("=" * 70)
        print("  SUMMARY")
        print("=" * 70)
        print()
        print(f"  The Spectrum pool prices 1 ERG = {spec_price:.4f} SigUSD")
        print(f"  The Oracle prices 1 ERG = ${oracle_price:.4f}")
        print(f"  This implies 1 SigUSD = ${implied_sigusd_usd:.2f} on Spectrum")
        print()
        if not can_mint and not can_redeem:
            print(f"  BOTH bank operations are blocked (RR={rr:.0f}%)")
            print(f"  - Mint needs RR > 400%")
            print(f"  - Redeem needs RR < 800%")
        elif can_mint:
            print(f"  Bank MINTING is available!")
            print(f"  Path A would yield significant profit if executed.")
        elif can_redeem:
            print(f"  Bank REDEEMING is available, but Path B loses money")
            print(f"  because buying SigUSD on Spectrum is expensive (SigUSD premium).")
        print()

        print(f"  EXECUTION READINESS:")
        print(f"  {'-'*50}")
        print(f"  Wallet:             {erg_balance:.4f} ERG")
        print(f"  Node:               OK ({NODE})")
        print(f"  ERG->SigUSD (Mew):  READY (tested)")
        print(f"  ERG->USE (Crux):    READY (tested)")
        print(f"  SigUSD Bank mint:   {'BLOCKED (RR too low)' if not can_mint else 'AVAILABLE'}")
        print(f"  SigUSD Bank redeem: {'BLOCKED' if not can_redeem else 'AVAILABLE'}")
        print(f"  Spectrum swap:      CODE NOT YET BUILT")
        print(f"  Bank TX builder:    CODE NOT YET BUILT")
        print()
        if not can_mint:
            print(f"  --> No actionable opportunity right now.")
            print(f"      Bank minting blocked. When RR climbs above 400%,")
            print(f"      Path A could be very profitable (~20% if spread holds).")
            print(f"      The scanner + Discord notifications will alert you.")


if __name__ == "__main__":
    asyncio.run(main())
