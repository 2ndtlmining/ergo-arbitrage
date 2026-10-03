# Arbitrage Flow Reference

All available swap flows, their status, and test results.

## Venues

| Venue | Type | Stablecoin | API |
|-------|------|-----------|-----|
| SigmaUSD Bank | Protocol | SigUSD | Explorer + Oracle |
| ErgoDEX/Spectrum pool | AMM Pool | SigUSD | on-chain pool box `9916d751...` (Spectrum API sunset; swaps routed via Crux) |
| Mew Finance | Batcher | SigUSD | Node TX (template) |
| Crux Finance | Dexy LP | USE | api.cruxfinance.io |
| NonKYC | CEX | USDT | api.nonkyc.io |
| Kucoin | CEX | USDT | api.kucoin.com |

## Individual Swap Legs

### ERG -> SigUSD

| # | Method | Route | Code | Tested Live | Notes |
|---|--------|-------|------|-------------|-------|
| 1 | Mew Finance batcher | ERG -> SigUSD | `archive/execute_swap_sigusd.py` (archived) | YES (1 ERG) | One-off; replaced pubkey in ErgoTree, not guarded |
| 2 | SigmaUSD Bank mint | ERG -> SigUSD | Not yet | BLOCKED | Same issue as #5 - needs bank contract TX builder. Currently RR=262% so also blocked by reserve ratio. |
| 3 | Spectrum DEX swap | ERG -> SigUSD | `arb.py swap` | LIVE | 0.5% LP fee, direct pool swap: miner fee only (~0.785 ERG Crux service only with POOL_SWAP_ROUTE=crux) |

### SigUSD -> ERG

| # | Method | Route | Code | Tested Live | Notes |
|---|--------|-------|------|-------------|-------|
| 4 | Crux Finance DEX | SigUSD -> ERG | `archive/execute_swap_sigusd_to_erg.py` | YES (1 SigUSD) | Uses /dex/swap via Spectrum pool, 0.782 ERG service fee |
| 5 | SigmaUSD Bank redeem | SigUSD -> ERG | `archive/execute_bank_redeem.py` (now `arb.py redeem`) | YES (1 SigUSD) | Direct EIP-15 contract TX. TX c7a08cda confirmed. Got 3.065 ERG net for 1 SigUSD (oracle rate 3.136, -2% protocol, -0.23% UI fee, -0.0011 ERG miner). |
| 6 | Spectrum DEX swap | SigUSD -> ERG | `arb.py swap` | LIVE | 0.5% LP fee, direct pool swap: miner fee only (~0.785 ERG Crux service only with POOL_SWAP_ROUTE=crux) |

### ERG -> USE

| # | Method | Route | Code | Tested Live | Notes |
|---|--------|-------|------|-------------|-------|
| 7 | Crux Finance free_mint | ERG -> USE (bank) | `archive/execute_swap_use.py` | YES (1 ERG) | Uses /dexy/build_mint_tx |
| 8 | Crux Finance LP swap | ERG -> USE (pool) | `archive/execute_swap_erg_to_use_lp.py` | YES (1 ERG) | Uses /dex/swap endpoint, 0.785 ERG service fee, TX e2582256 confirmed |

### USE -> ERG

| # | Method | Route | Code | Tested Live | Notes |
|---|--------|-------|------|-------------|-------|
| 9 | Crux Finance LP swap | USE -> ERG (pool) | `archive/execute_swap_use_to_erg.py` | YES (0.3 USE) | Uses /dex/swap endpoint, 0.784 ERG service fee |

### ERG <-> USDT (CEX)

| # | Method | Route | Code | Tested Live | Notes |
|---|--------|-------|------|-------------|-------|
| 10 | NonKYC | Buy/Sell ERG | Not yet | NO | Requires API key + secret |
| 11 | Kucoin | Buy/Sell ERG | Not yet | NO | Requires API key + passphrase |

## Arbitrage Paths (Full Round-Trips)

### Path A: Bank Mint -> DEX Sell
```
ERG ->(Bank mint #2)-> SigUSD ->(Spectrum #6)-> ERG
```
- **When profitable**: Bank oracle rate > Spectrum pool rate (SigUSD overpriced on DEX)
- **Blocked when**: RR < 400% (can't mint)
- **Fees**: ~2.22% bank fee (2% protocol + 0.229% UI) + 0.5% DEX fee + tx fees (direct pool swap; +~0.785 ERG only with POOL_SWAP_ROUTE=crux)
- **Time**: ~15 min
- **Code status**: Neither leg built yet
- **Current status**: BLOCKED (RR=262%)

### Path B: DEX Buy -> Bank Redeem
```
ERG ->(Spectrum #3)-> SigUSD ->(Bank redeem #5)-> ERG
```
- **When profitable**: Spectrum pool rate > Bank oracle rate (SigUSD cheap on DEX)
- **Blocked when**: never (SigUSD redeem has no RR restriction; payout is pro-rata if RR < 100%)
- **Fees**: 0.5% DEX fee + ~2.22% bank fee + tx fees (direct pool swap; +~0.785 ERG only with POOL_SWAP_ROUTE=crux)
- **Time**: ~15 min
- **Code status**: Bank redeem (#5) WORKING. Spectrum buy (#3) not yet built.
- **Current status**: Math shows -20.7% loss (SigUSD overpriced, not cheap)

### Path C: Mew Buy -> Mew Sell (SigUSD round-trip)
```
ERG ->(Mew #1)-> SigUSD ->(Mew #4)-> ERG
```
- **When profitable**: Unlikely (same venue, same rates)
- **Fees**: ~0.94 ERG batcher + 0.002 protocol + 0.0011 miner (each way)
- **Code status**: Forward leg (#1) tested, reverse leg (#4) NOT tested

### Path D: Crux LP round-trip (USE)
```
ERG ->(Crux LP #8)-> USE ->(Crux LP #9)-> ERG
```
- **When profitable**: Unlikely (same pool, same rates + 0.3% each way)
- **Fees**: 0.3% LP fee each way + service fee
- **Code status**: Neither tested via LP swap

### Path E: Crux Mint -> Crux LP Sell (USE arbitrage)
```
ERG ->(Crux mint #7)-> USE ->(Crux LP #9)-> ERG
```
- **When profitable**: When USE LP price > oracle mint price (USE overpriced in pool)
- **Blocked when**: free_mint unavailable AND arb_mint unavailable
- **Fees**: mint fee + 0.3% LP fee + ~0.786 ERG service
- **Time**: ~5 min (single chain TX)
- **Code status**: Mint (#7) TESTED, LP sell (#9) TESTED - BOTH WORK
- **Current status**: FREE MINT AVAILABLE

### Path F: Crux LP Buy -> Crux Redeem? (USE reverse)
```
ERG ->(Crux LP #8)-> USE ->(bank redeem?)-> ERG
```
- **Status**: No redeem API found. USE can only go back via LP swap.

### Path G: CEX <-> DEX
```
Buy ERG (Kucoin/NonKYC) -> withdraw -> swap on DEX
```
- **When profitable**: CEX price significantly different from DEX
- **Caveat**: Assumes SigUSD = USDT (currently NOT true, SigUSD = $1.23)
- **Fees**: CEX trading + withdrawal (3.3 ERG NonKYC / 0.73 ERG Kucoin)
- **Time**: 40-60 min (withdrawal confirmations)
- **Code status**: Price monitoring only, no execution

### Path H: CEX <-> CEX
```
Sell ERG (Exchange A) -> USDT transfer -> Buy ERG (Exchange B)
```
- **When profitable**: Significant price difference between exchanges
- **Fees**: Trading fees both sides + USDT transfer + ERG withdrawal
- **Time**: ~90 min
- **Code status**: Price monitoring only, no execution

## Fee Summary

| Venue | Trading Fee | Withdrawal | Other |
|-------|-----------|-----------|-------|
| SigmaUSD Bank | 2.0% protocol + 0.229% UI fee (min 0.001 ERG) | N/A | 0.0011 ERG tx + 0.001 ERG receipt box |
| Spectrum DEX (SigUSD) | 0.5% pool fee | N/A | 0.0011 ERG miner (direct); ~0.785 ERG service only via Crux |
| Mew Finance | ~0.94 ERG batcher | N/A | 0.002 ERG protocol + 0.0011 ERG miner |
| Crux Finance (mint) | 0.3%+0.2% bank fees | N/A | ~0.786 ERG service + 0.002 ERG miner |
| Crux Finance (LP) | 0.3% pool fee | N/A | 0.002 ERG miner |
| NonKYC | 0.2% maker/taker | 3.3 ERG | |
| Kucoin | 0.1% maker/taker | 0.73 ERG | |

## Current Prices (as of last scan)

- Oracle: 1 ERG = $0.3185
- Spectrum: 1 ERG = 0.2588 SigUSD (implied: 1 SigUSD = $1.23)
- Crux LP: 1 ERG = 0.320 USE (implied: 1 USE = $0.99)
- NonKYC: bid=$0.3121 ask=$0.3148
- Kucoin: bid=$0.3200 ask=$0.3203

## Priority Testing Order

1. **USE -> ERG via Crux LP** (#9) - enables Path E (most promising near-term arb)
2. **SigUSD -> ERG via Mew** (#4) - enables Path C round-trip
3. **ERG -> USE via Crux LP** (#8) - alternative to mint, for comparison
4. **ERG -> SigUSD via Spectrum** (#3) - needed for Path B
5. **SigUSD -> ERG via Spectrum** (#6) - needed for Path A
