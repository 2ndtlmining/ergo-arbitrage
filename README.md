# Ergo Arbitrage Monitor

A Python application that monitors price differences across centralized exchanges (CEX), decentralized exchanges (DEX), and the SigmaUSD bank on the Ergo blockchain. The goal is simple: **end up with more ERG**.

## How It Works

The app continuously scans prices from multiple sources and calculates whether an arbitrage opportunity exists after accounting for **all fees, slippage, and transaction costs**. No trade is recommended unless the math shows a net profit in ERG.

### Price Sources

| Source | Type | Pairs | Notes |
|--------|------|-------|-------|
| **NonKYC** | CEX | ERG/USDT | Order book based, HMAC-SHA256 auth, 10 req/s |
| **Kucoin** | CEX | ERG/USDT | HMAC-SHA256 v2 auth, 25 req/s |
| **Spectrum Finance** | DEX (AMM) | ERG/SigUSD | On-chain liquidity pool, 0.5% pool fee |
| **SigmaUSD Bank** | Protocol | ERG/SigUSD | Oracle-priced, ~2.2% combined fee, reserve ratio restrictions |
| **Crux Finance** | DEX (Atomic LP) | ERG/USE | LP pool swap + FreeMint/ArbMint for USE |
| **Oracle** | Price feed | ERG/USD | erg-oracle-ergusd.spirepools.com |

### Ergo Node

The app connects to your Ergo node to:
- Check wallet balances (ERG, SigUSD, USE tokens)
- Sign and submit transactions (Phase 2 - execution)
- Interact with smart contracts for DEX swaps and bank operations

## Modes

```bash
# Monitor mode (console only, no notifications)
python main.py

# Notification mode (console + Discord alerts, no trades)
python main.py --notify

# Live mode (console + Discord + auto-execute) - NOT YET WIRED
python main.py --live
```

## Arbitrage Strategies

### Strategy 1: SigmaUSD Bank Mint -> DEX Sell

When the bank's oracle price values ERG higher than the DEX pool price:

```
  Oracle:   1 ERG = $0.32 (SigUSD via bank)
  Spectrum: 1 ERG = $0.29 (SigUSD via pool)

  Step 1: Send 10 ERG to SigmaUSD Bank, mint SigUSD
          Receive: 10 * $0.32 * (1 - 2.2% fee) = 3.13 SigUSD
  Step 2: Swap 3.13 SigUSD -> ERG on Spectrum
          Receive: 3.13 / $0.29 * (1 - 0.5% pool) - 0.785 ERG service = ~9.9 ERG
  Result:  10 ERG -> ~9.9 ERG (fees eat the spread in this example)

  RESTRICTION: Bank minting requires reserve ratio > 400%
```

### Strategy 2: DEX Buy -> SigmaUSD Bank Redeem

When the DEX pool prices ERG higher than the bank's oracle rate:

```
  Spectrum: 1 ERG = $0.35 (SigUSD)
  Oracle:   1 ERG = $0.32

  Step 1: Swap 10 ERG -> SigUSD on Spectrum (-0.5% pool, -0.785 ERG service)
  Step 2: Redeem SigUSD at Bank (-2.2% bank fee, -0.002 ERG receipt)
  Result:  Depends on spread vs fees

  NOTE: SigUSD redeem has no reserve-ratio restriction (pro-rata payout if RR < 100%)
```

### Strategy 3: CEX <> DEX (Cross-Venue)

When NonKYC/Kucoin prices ERG differently than Spectrum. **Assumes SigUSD ~ 1 USDT** (currently NOT true - SigUSD is depegged at ~$1.23).

### Strategy 4: CEX <> CEX

When ERG is priced differently on NonKYC vs Kucoin. Buy on the cheaper exchange, sell on the more expensive one.

### Strategy 5: USE (DexyUSD) Mint -> LP Sell

When USE can be minted at oracle rate and sold via Crux LP at a higher effective rate:

```
  Step 1: Mint USE via Crux Finance (oracle rate, ~0.79 ERG flat fee)
  Step 2: Sell USE -> ERG via Crux LP (0.3% pool fee, ~0.785 ERG service fee)
  Result:  Profit if LP rate > mint rate + fees
```

## Fee Accounting

Every opportunity calculation includes ALL of these costs:

| Fee | Amount | Applies To |
|-----|--------|-----------|
| NonKYC trading fee | 0.2% | CEX buy/sell |
| NonKYC ERG withdrawal | 3.3 ERG | Moving ERG off NonKYC |
| Kucoin trading fee | 0.1% | CEX buy/sell |
| Kucoin ERG withdrawal | 0.73 ERG | Moving ERG off Kucoin |
| Spectrum pool fee | 0.5% | SigUSD/ERG pool swaps |
| Spectrum service fee | ~0.785 ERG flat | Via Crux routing to Spectrum |
| Mew Finance fee | ~0.55 ERG flat | 0.54 batcher + 0.002 protocol + 0.007 overhead + 0.001 miner |
| Crux Finance LP (USE) | 0.3% pool fee + ~0.785 ERG | LP swap service fee |
| Crux Finance mint (USE) | ~0.79 ERG flat | Mint transaction fee |
| SigmaUSD protocol fee | 2.0% | Bank mint/redeem (stays in reserve) |
| SigmaUSD frontend fee | 0.229% | Bank mint/redeem |
| Ergo transaction fee | ~0.0011 ERG | Every on-chain tx |
| Slippage (estimated) | 0.5-3% | DEX swaps (size dependent) |

### Slippage Tiers

| Trade Size | Estimated Slippage |
|-----------|-------------------|
| Up to 10 ERG | 0.5% |
| Up to 50 ERG | 1.0% |
| Up to 100 ERG | 2.0% |
| Up to 500 ERG | 3.0% |

## Discord Notifications

The `--notify` mode sends alerts to a Discord channel via webhook. It includes anti-spam protections:

### Notification Flow

1. **Streak confirmation**: An opportunity must appear profitable for `DISCORD_CONFIRM_SCANS` consecutive scans (default 3 = 45 seconds) before any notification is sent. Filters out price blips and API glitches.

2. **Minimum thresholds**: Must meet BOTH `DISCORD_MIN_PROFIT_PERCENT` (default 1.0%) AND `DISCORD_MIN_PROFIT_ERG` (default 0.5 ERG absolute profit). A 1 ERG trade at "+2%" is only +0.02 ERG - not actionable.

3. **Tier system**:
   - **Tier 1 (ping)**: >= 2% profit AND >= 0.5 ERG AND no SigUSD=USDT assumption. Sends `@user` mention.
   - **Tier 2 (silent)**: Passes thresholds but below Tier 1. Message without ping.

4. **Per-path cooldown**: Same path won't notify again for `DISCORD_COOLDOWN_SECONDS` (default 300s).

5. **Price staleness guard**: If any price source used by a path hasn't been fetched in the last `PRICE_STALE_SECONDS` (default 60s), that path's notification is skipped.

6. **Wallet analysis**: Sent every `DISCORD_WALLET_COOLDOWN_SECONDS` (default 600s = 10 min), OR when a NEW opportunity just hit confirmation threshold. Not every scan.

7. **Summary heartbeat**: Compact grid of all paths sent every `DISCORD_SUMMARY_INTERVAL_SECONDS` (default 1800s = 30 min).

### Discord Configuration

```env
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_USER_ID=123456789              # Your Discord user ID for @mentions
DISCORD_MIN_PROFIT_PERCENT=1.0         # Min % to notify (default 1.0)
DISCORD_MIN_PROFIT_ERG=0.5             # Min absolute ERG profit (default 0.5)
DISCORD_TIER1_PROFIT_PERCENT=2.0       # Min % for Tier 1 ping (default 2.0)
DISCORD_COOLDOWN_SECONDS=300           # Per-path cooldown (default 300)
DISCORD_CONFIRM_SCANS=3                # Consecutive scans before alerting (default 3)
DISCORD_WALLET_COOLDOWN_SECONDS=600    # Wallet analysis interval (default 600)
DISCORD_SUMMARY_INTERVAL_SECONDS=1800  # Summary heartbeat interval (default 1800)
PRICE_STALE_SECONDS=60                 # Max price age before skipping (default 60)
```

## Setup

### Prerequisites

- Python 3.11+
- An Ergo node with wallet (can be on a local VM)
- NonKYC and/or Kucoin exchange accounts with API keys
- (Optional) Discord webhook URL for notifications

### Installation

```bash
cd ergo_arbitrage
pip install -r requirements.txt
```

### Configuration

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# Ergo Node (your VM)
ERGO_NODE_URL=http://YOUR_NODE_IP:9053
ERGO_NODE_API_KEY=your_api_key

# NonKYC Exchange
NONKYC_API_KEY=your_api_key
NONKYC_API_SECRET=your_api_secret

# Kucoin Exchange
KUCOIN_API_KEY=your_api_key
KUCOIN_API_SECRET=your_api_secret
KUCOIN_API_PASSPHRASE=your_passphrase

# Discord Notifications (optional)
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_USER_ID=your_discord_user_id

# Arbitrage Settings
MIN_PROFIT_PERCENT=0.5
MAX_TRADE_SIZE_ERG=100
SCAN_INTERVAL_SECONDS=15
```

### Running

```bash
# Monitor mode (console only)
python main.py

# Notification mode (console + Discord)
python main.py --notify

# Live mode (NOT YET IMPLEMENTED - placeholder only)
python main.py --live

# Run tests
python -m pytest tests/ -v
```

## Project Structure

```
ergo_arbitrage/
├── .env                    # API keys (gitignored)
├── .env.example            # Template for .env
├── config.py               # Settings, fee constants, token IDs
├── main.py                 # Entry point (monitor/notify/live)
├── logging_config.py       # Rich console + file logging
├── FLOWS.md                # Detailed swap flow reference
├── exchanges/
│   ├── base.py             # Abstract interfaces (CEXBase, DEXBase, RateLimiter)
│   ├── nonkyc.py           # NonKYC exchange (REST + HMAC auth)
│   ├── kucoin.py           # Kucoin exchange (REST + HMAC v2 auth)
│   ├── spectrum.py         # Spectrum Finance DEX (AMM pools)
│   ├── sigmausd.py         # SigmaUSD Bank (oracle + reserve ratio)
│   └── ergo_node.py        # Ergo node wallet client
├── arbitrage/
│   ├── scanner.py          # Main scan loop, grid display, streak tracking
│   └── calculator.py       # Profit math with full fee breakdown
├── notifications/
│   └── discord.py          # Discord webhook (tiers, cooldown, staleness guard)
├── tracker/
│   └── profit_tracker.py   # SQLite DB: snapshots, opportunities, trades, stats
├── tests/
│   ├── conftest.py         # Pytest fixtures
│   ├── test_calculator.py  # Unit tests for arbitrage math
│   ├── test_pool_math.py   # Unit tests for AMM swap calculations
│   ├── test_api_live.py    # Live API integration tests
│   └── test_dex_node.py    # Node interaction tests
└── execute_*.py            # Standalone swap execution scripts (tested live)
```

### Wallet tool (`python arb.py ...`)

One command for everyday actions. Every action explains each step, is a **dry run by
default**, `--check` signs and has your node validate it without broadcasting, and
`--execute` sends it and then reports block by block until it confirms.

```bash
python arb.py balance                                   # ERG, SigUSD, value, pending, bank RR, SigUSD peg
python arb.py quote                                     # best arb size per path right now (+ break-even)
python arb.py quote  --sell sigusd --amount 10          # ...and pool vs bank for an amount
python arb.py swap   --sell erg    --amount 5           # direct pool swap, no service fee
python arb.py swap   --sell sigusd --amount all --execute
python arb.py redeem --sigusd all --execute             # SigUSD -> ERG at the bank
python arb.py send   --to 9f... --erg 1.5 --execute     # also --sigusd; the guard only allows that payee and amount
python arb.py arb    --check                            # two-leg pool buy -> bank redeem at the best size
python arb.py arb    --erg 10 --path mint --check       # fixed size; bank mint -> pool sell
```

**Trade size.** `arb.py arb` (without `--erg`, or `--erg best`) and live mode size the trade
on the exact pool, bank and oracle boxes the transactions will spend (`arbitrage/sizing.py`,
integer contract math identical to the transaction builders). The search runs over whole
SigUSD cents, because profit in ERG is a sawtooth: extra ERG buys nothing until the next cent.
Of all sizes between `MIN_TRADE_SIZE_ERG` and the cap (`MAX_TRADE_SIZE_ERG`, wallet minus
`LIVE_ERG_RESERVE`) that keep profit at or above `MIN_PROFIT_PERCENT`, it takes the smallest one
earning `SIZE_PROFIT_CAPTURE` (0.95) of the best profit: near the peak, more size adds little
profit but all of its risk. Set it to 1.0 to maximise profit outright. The figures are exact,
with no execution buffer, so `MIN_PROFIT_PERCENT` is the safety margin for the oracle or pool
moving between leg 1 and leg 2.

### Live mode (`python main.py --live`)

Live mode executes whichever of **pool buy -> bank redeem** and **bank mint -> pool sell**
(the same code as `arb.py arb --path redeem|mint --execute`) passes every check with the higher
profit, sized again on fresh boxes right before signing (see **Trade size** above). Bank mint is only possible while the reserve
ratio stays >= 400% after minting:

| Check | Setting (default) |
|---|---|
| Profitable at its best size | `MIN_PROFIT_PERCENT` (0.5), `SIZE_PROFIT_CAPTURE` (0.95) |
| ...for N scans in a row | `LIVE_CONFIRM_SCANS` (3) |
| Size capped | `MAX_TRADE_SIZE_ERG`, wallet minus `LIVE_ERG_RESERVE` (1) |
| Node synced and wallet unlocked | checked before each trade |
| Kill switch file absent | `LIVE_STOP_FILE` (`STOP`) |
| Wallet value drawdown since start | `LIVE_MAX_DRAWDOWN_ERG` (5) |
| Time since last trade | `LIVE_TRADE_COOLDOWN_SECONDS` (300) |
| Trades today | `LIVE_MAX_TRADES_PER_DAY` (10) |

Each scan prints `LIVE: not trading - <reasons>` while any check fails. A leg-2 failure or a
TX-guard refusal pauses live trading until restart and is sent to Discord with the redeem
command to finish.

### Execution Scripts

Every script builds the transaction, checks it with the TX guard (`ergo/tx_guard.py`) and
stops there unless you pass `--execute`. The guard resolves all inputs from your own node
and refuses to sign if any output goes somewhere other than your wallet, the recreated
pool/bank box, the miner fee or a whitelisted service-fee address, or if the wallet would
spend more / receive less than the quote allows (`SLIPPAGE_TOLERANCE`, `MAX_FEE_BUDGET_ERG`,
`MAX_TRADE_SIZE_ERG`).

```bash
python execute_bank_redeem.py --sigusd 1.0            # dry run: build + verify
python execute_bank_redeem.py --sigusd 1.0 --execute  # sign and submit
python execute_swap_sigusd_to_erg.py --execute
```

`execute_pool_swap.py` swaps directly against the ErgoDEX pool box, with no Crux service
fee (~0.76 ERG saved per swap). `--check` signs the TX and has your node validate it,
including the pool contract, without broadcasting:

```bash
python execute_pool_swap.py --sell erg --amount 1 --check
python execute_pool_swap.py --sell sigusd --amount 0.5 --execute
```

| Script | Direction | Status | TX Proof |
|--------|-----------|--------|----------|
| `execute_arb.py` | Full loop: pool buy -> bank redeem (two chained TXs) | NEW: dry run / `--check` first; executes only if profitable | - |
| `execute_pool_swap.py` | ERG <-> SigUSD (direct pool spend, no Crux fee) | NEW: dry run / `--check` first | - |
| `execute_swap_use.py` | ERG -> USE (Crux mint) | TESTED LIVE | Confirmed on-chain |
| `execute_swap_use_to_erg.py` | USE -> ERG (Crux LP) | TESTED LIVE | TX `bf544106...` |
| `execute_swap_erg_to_use_lp.py` | ERG -> USE (Crux LP) | TESTED LIVE | TX `e2582256...` |
| `execute_swap_sigusd_to_erg.py` | SigUSD -> ERG (Crux/Spectrum) | TESTED LIVE | TX `863979a5...` |
| `execute_bank_redeem.py` | SigUSD -> ERG (Bank redeem) | TESTED LIVE | TX `c7a08cda...` |
| `execute_swap_erg_to_sigusd_spectrum.py` | ERG -> SigUSD (Spectrum) | QUOTE ONLY | Not live tested |
| `execute_swap_sigusd_to_erg_spectrum.py` | SigUSD -> ERG (Spectrum) | QUOTE ONLY | Not live tested |

## Data Tracking

All data is stored in `arbitrage_tracker.db` (SQLite):

- **price_snapshots** - Every scan's prices from all sources
- **scan_results** - All paths analyzed per scan
- **opportunities** - Profitable opportunities with full fee breakdown
- **trades** - Executed trades with timing, actual profit, and tx IDs
- **daily_summary** - Aggregated daily stats

## Key Token IDs (Ergo Mainnet)

| Token | ID | Decimals |
|-------|-----|---------|
| ERG | `0000...0000` (native) | 9 |
| SigUSD | `03faf2cb329f2e90d6d23b58d91bbb6c046aa143261cc21f52fbe2824bfcbf04` | 2 |
| SigRSV | `003bd19d0187117f130b62e1bcab0939929ff5c7709f843c5c4dd158949285d0` | 0 |
| USE (DexyUSD) | `a55b8735ed1a99e46c2c89f8994aacdf4b1109bdcf682f1e5b34479c6e392669` | 3 |
| Bank NFT (SigmaUSD) | `7d672d1def471720ca5782fd6473e47e796d9ac0c138d9911346f118b2f6d9d9` | - |

## Important Notes

- **Never commit `.env`** - it contains your API keys and is gitignored
- **NonKYC ERG withdrawal fee is 3.3 ERG** - makes small CEX trades unprofitable
- **SigmaUSD Bank has restrictions** - SigUSD minting is blocked if the post-mint RR would fall below 400%; SigUSD redeem is always allowed (the 800% cap only applies to SigRSV minting)
- **SigUSD is currently depegged** - trading at ~$1.23 instead of $1.00 (makes CEX<>DEX paths unreliable)
- **USE (DexyUSD) is well-pegged** - trading at ~$0.99, more predictable for arb paths
- **Crux service fee is ~0.785 ERG flat** - brutal for small swaps, negligible for large ones
- **Ergo TX fees are EXPLICIT** - must be an output box, not implicit (inputs must equal outputs)
- **Node must be fully synced** for wallet operations to work correctly

---

## Swap Flow Testing Status

See [FLOWS.md](FLOWS.md) for the full detailed reference. Summary:

### Individual Swap Legs

| # | Route | Status | Notes |
|---|-------|--------|-------|
| 1 | ERG -> SigUSD (Mew batcher) | TESTED LIVE | Template-based, replaces pubkey |
| 2 | ERG -> SigUSD (Bank mint) | BLOCKED | RR < 400% |
| 3 | ERG -> SigUSD (Spectrum via Crux) | QUOTE TESTED | Script ready |
| 4 | SigUSD -> ERG (Crux/Spectrum) | TESTED LIVE | 0.782 ERG service fee |
| 5 | SigUSD -> ERG (Bank redeem) | TESTED LIVE | Direct EIP-15 contract TX |
| 6 | SigUSD -> ERG (Spectrum via Crux) | QUOTE TESTED | Script ready |
| 7 | ERG -> USE (Crux free mint) | TESTED LIVE | /dexy/build_mint_tx |
| 8 | ERG -> USE (Crux LP swap) | TESTED LIVE | /dex/swap endpoint |
| 9 | USE -> ERG (Crux LP swap) | TESTED LIVE | /dex/swap endpoint |
| 10 | NonKYC buy/sell ERG | MONITOR ONLY | No execution code |
| 11 | Kucoin buy/sell ERG | MONITOR ONLY | No execution code |

### Arbitrage Paths (Full Round-Trips)

| Path | Route | Status | Blocker |
|------|-------|--------|---------|
| **E** | Crux mint USE -> Crux LP sell | BOTH LEGS WORK | Free mint availability |
| **B** | Spectrum buy SigUSD -> Bank redeem | Bank redeem WORKS, Spectrum buy quote-only | Need live test Spectrum buy |
| **C** | Mew buy SigUSD -> Crux sell | Both legs tested separately | High round-trip fees (~1.7 ERG) |
| **A** | Bank mint SigUSD -> Spectrum sell | Neither leg executable | RR < 400% blocks mint |
| **G** | CEX <> DEX cross-venue | Monitor only | SigUSD depeg, no exec code |
| **H** | CEX <> CEX | Monitor only | No execution code |

---

## Completed Features

- [x] Price monitoring across 6 venues (NonKYC, Kucoin, Spectrum, SigmaUSD Bank, Crux, Oracle)
- [x] Arbitrage scanner with compact grid display (paths x trade sizes)
- [x] Full fee accounting in all profit calculations
- [x] Wallet-based analysis (what can you do with current holdings)
- [x] Kucoin integration with rate limiting (25 req/s)
- [x] NonKYC integration with rate limiting (10 req/s)
- [x] ERG -> SigUSD swap via Mew Finance batcher (tested live)
- [x] ERG -> USE mint via Crux Finance API (tested live)
- [x] ERG -> USE swap via Crux LP (tested live)
- [x] SigUSD -> ERG swap via Crux/Spectrum (tested live)
- [x] USE -> ERG swap via Crux LP (tested live)
- [x] SigUSD -> ERG bank redeem via direct EIP-15 contract TX (tested live)
- [x] Stuck swap order recovery (via `inputsRaw` + `/utxo/byIdBinary`)
- [x] SQLite tracking (price snapshots, opportunities, trades, daily stats)
- [x] Discord notifications with anti-spam (streak confirmation, tier system, cooldowns)
- [x] Wallet analysis to Discord (rate limited)
- [x] Periodic summary heartbeat to Discord
- [x] Price staleness guard (skip stale notifications)

---

## Future Improvements

### High Priority

- [ ] **Wire `--live` mode execution**: Scanner currently has a placeholder. Need to call the tested execute_* scripts when scanner detects a confirmed, profitable opportunity.
- [ ] **Live test Spectrum swap scripts**: `execute_swap_erg_to_sigusd_spectrum.py` and `execute_swap_sigusd_to_erg_spectrum.py` are quote-tested only. Need a small live test to confirm they work end-to-end.
- [ ] **Build `exchanges/crux_finance.py` module**: Wrap `/dex/quote`, `/dex/swap`, `/dexy/build_mint_tx`, `/dexy/mint_status` into a proper exchange adapter instead of inline API calls in scanner.
- [ ] **Build `exchanges/mew_finance.py` module**: Dynamic contract template discovery, correct box value calculation, swap order submission/monitoring, failed order recovery.
- [x] ~~Fix hardcoded node URLs in execute_* scripts~~ - All scripts now read from `.env` via `os.getenv()`.

### Medium Priority

- [ ] **CEX trade execution**: Build NonKYC and Kucoin buy/sell execution (price monitoring already works, just need order placement).
- [ ] **ERG withdrawal from CEX**: Automate withdrawal to node wallet after CEX trade.
- [ ] **Make trade sizes configurable**: Currently hardcoded `[1, 5, 10, 25, 50, 100]` in scanner. Should be in config.
- [ ] **Transaction monitoring**: After submitting a swap, poll for confirmation and report success/failure.
- [ ] **Dry-run mode**: Simulate with real prices but no actual trades (for testing thresholds).
- [ ] **Test SigUSD -> ERG via Mew Finance** (reverse swap, never tested).
- [ ] **Initialize `_nonkyc_usdt_fee` / `_kucoin_usdt_fee` in `__init__`**: Currently set in `connect_all()`, which means accessing before connection uses `getattr` fallback.

### Low Priority

- [ ] **WebSocket price streaming**: NonKYC supports `wss://ws.nonkyc.io` for real-time prices instead of polling.
- [ ] **Pool reserve queries**: For accurate slippage calculation based on actual pool depth.
- [ ] **Dynamic contract fetching**: Mew Finance contracts change over time; auto-discover from recent batcher TXs.
- [ ] **Integration tests**: End-to-end test of scanner lifecycle with mock data. Regression tests for Discord notification filtering (streak, tier, cooldown).
- [ ] **Improve Path E calculation**: Currently assumes oracle price equals pool sell rate. Should use actual Crux LP quote for the sell leg.
- [ ] **Add `analysis.get()` safety in `discord.py`**: `send_wallet_analysis` accesses `analysis[asset_key]` directly - could KeyError if analysis dict is incomplete.
