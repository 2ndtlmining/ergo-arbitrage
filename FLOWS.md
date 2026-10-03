# Arbitrage Flow Reference

Every swap leg and round trip the bot knows, the code that runs it and what has been tested on
mainnet. The README is authoritative for how to run things; this page is the per-leg reference.
Fees are the `config.py` values.

## Venues

| Venue | Type | Pair | How the bot reaches it |
|-------|------|------|------------------------|
| SigmaUSD bank | Protocol (EIP-15) | ERG/SigUSD | Bank and oracle boxes read from your node; mint/redeem transactions built by the bot (`ergo/sigmausd_tx.py`) |
| ErgoDEX (Spectrum) pool | AMM pool | ERG/SigUSD | Pool box `9916d751...` read from your node; swaps built by the bot (`ergo/pool_swap.py`); Spectrum's API is sunset |
| Crux Finance / Dexy | Dexy LP and mint | ERG/USE | api.cruxfinance.io. **Off** (`ENABLE_USE=false`): the USE LP was drained (Oct 2026) and a token migration is expected |
| Kucoin, NonKYC, Gate, MEXC, SafeTrade | CEX | ERG/USDT | Public order books, **watch-only** (no trading code) |

## Swap legs

| Leg | Route | Code | Mainnet status | Fees |
|-----|-------|------|----------------|------|
| ERG -> SigUSD | Pool swap | `arb.py swap --sell erg` | signed and validated by the node (`--check`); outputs match Crux's quotes to the unit | 0.5% pool fee (read from the pool box) + 0.0011 ERG miner |
| SigUSD -> ERG | Pool swap | `arb.py swap --sell sigusd` | same | same |
| SigUSD -> ERG | Bank redeem | `arb.py redeem` | executed (first test: TX `c7a08cda`, 3.065 ERG for 1 SigUSD) | 2% protocol + 0.229% UI fee + 0.0011 ERG miner + 0.001 ERG receipt box |
| ERG -> SigUSD | Bank mint | `arb.py arb --path mint` (leg 1) | built and unit-tested; not executed: minting has been closed (reserve ratio below 400%) | 2% protocol + 0.229% UI fee + 0.0011 ERG miner |
| ERG <-> USE | Crux mint / LP swap | `archive/` scripts only | executed once each (2026-03), now disabled | Crux ~0.79 ERG mint fee / 0.3% LP + ~0.785 ERG service fee |
| ERG <-> USDT | CEX | none | watch-only | taker fee + ERG withdrawal fee (Kucoin and NonKYC published live; Gate 0.403, MEXC 0.1 ERG, unverified) |

With `POOL_SWAP_ROUTE=crux` the pool legs go through Crux's `/dex/swap` instead and pay its ~0.785 ERG
service fee per leg; the default `direct` route builds the swap itself and pays only the miner fee.

## Round trips

### Pool buy -> bank redeem (`redeem`)
```
ERG --(pool swap)--> SigUSD --(bank redeem)--> ERG
```
- **Profitable when** the pool gives more SigUSD per ERG than the oracle-priced bank takes back, by more than ~2.7% (0.5% pool + 2.229% bank) plus miner fees.
- **Never blocked** by the reserve ratio: SigUSD redeem is always allowed (pro-rata payout below 100%).
- **Runs** by hand with `python arb.py arb --path redeem --check | --execute`, and by `main.py --live` when every check passes. Leg 2 is followed until confirmed and rebuilt on fresh boxes if it drops.

### Bank mint -> pool sell (`mint`)
```
ERG --(bank mint)--> SigUSD --(pool swap)--> ERG
```
- **Profitable when** the bank sells SigUSD cheaper than the pool buys it, by more than the same fees.
- **Blocked** while the post-mint reserve ratio would fall below 400%. The dashboard Bank line shows the ERG price at which minting reopens, and Discord posts **Bank mint OPEN** when it does.
- **Runs** by hand with `python arb.py arb --path mint --check | --execute`, and by `main.py --live`.

### USE: Crux mint -> LP sell
```
ERG --(Crux mint)--> USE --(Crux LP)--> ERG
```
Priced only with `ENABLE_USE=true`; never executed by the bot. Off until the USE migration.

### CEX <-> pool and CEX <-> CEX
Watch-only. The dashboard's Exchanges panel and the `CEX watch-only` Discord message show each
exchange's gap to the pool and the best cross-exchange spread after both taker fees, the ERG
withdrawal fee and `CEX_USDT_TRANSFER_FEE`. The CEX <-> pool comparison assumes 1 SigUSD = 1 USDT,
which is usually not true (SigUSD trades off its peg), so it is a signal, not a trade.

## Retired routes

Mew Finance batcher swaps (ERG -> SigUSD, ~0.55 ERG per swap) and Crux-routed SigUSD pool swaps were
tested in March 2026 with one-off scripts, now in `archive/`. Both were replaced by the direct pool
swap, which pays only the miner fee.
