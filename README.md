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
- Sign and submit transactions (live mode and the wallet tool)
- Interact with smart contracts for DEX swaps and bank operations

## Modes

```bash
python main.py            # monitor: live dashboard, no notifications, no trades
python main.py --notify   # + Discord alerts
python main.py --live     # + auto-execute (see "Live mode" below)
```

| | monitor (default) | `--notify` | `--live` |
|---|---|---|---|
| Dashboard, chain watcher (pool, bank, oracle every 2 s), SQLite log | ✓ | ✓ | ✓ |
| Discord: opportunities, health alerts, daily digest | | ✓ | ✓ |
| **Signs and sends transactions from your node wallet** | | | ✓ |

`--notify` is the safe way to run the bot all the time: it watches and tells you, and never spends
anything. `--live` does the same, and when a path passes every safety check (see **Live mode**) it
executes the trade with your ERG. Start live mode with a small `--max-trade-erg` the first time
(see **First live run**).

The default view is a one-screen live dashboard (refreshed twice a second, Ctrl+C quits):

```
 ERGO ARB  MONITOR   h1885748  node ● 2 ms  wallet ●  poll 2s  full scan in 3s  confirm 2  19:38:12
┌──────────── Prices ────────────┐┌──────────────── Live ────────────────┐
│ Pool    0.3127 SigUSD/ERG  …   ││ off   trades today 0/10  drawdown …   │
│ Oracle  $0.3238/ERG  peg +3.5% ││   monitor mode: no trading            │
│ Bank    RR 330%  redeem ✓  mint ✗││ Wallet: 20.7119 ERG  0.00 SigUSD …   │
┌──────────────────────────── Paths ───────────────────────────────────────┐
│ path            size   profit      %   b/even  conf  status     last 30  │
│ pool→redeem        —  -0.0644      —       —   0/2   no edge: … ▁▁▁▁▁▁▁  │
│ mint→pool sell     —        —      —       —   0/2   BLOCKED: … ▁▁▁▁▁▁▁  │
┌──────────────────────────── Events ──────────────────────────────────────┐
│ 19:37:29 CHAIN h1885748 changed: pool, bank, oracle                      │
┌──────────────────────────── Venues ──────────────────────────────────────┐
│ ErgoDEX pool   on-chain  ● live          0.3127 SigUSD/ERG, 111,872 ERG  │
│ SigmaUSD bank  on-chain  ● live          RR 330%, mint ✗                 │
│ Oracle         on-chain  ● live          $0.3238/ERG                     │
│ Dexy USE       on-chain  – disabled      ENABLE_USE=false                │
│ Kucoin         CEX       ○ watch only    $0.3238 / $0.3242               │
│ NonKYC         CEX       ○ watch only    $0.3261 / $0.3267               │
```

| Flag | Effect |
|---|---|
| `--plain` | the classic scrolling output (tables per scan) |
| `--json` | one JSON line per full scan on stdout, nothing else |
| `--once` | one full scan, then exit (works with every view) |
| `--interval N` | seconds between full scans (`SCAN_INTERVAL_SECONDS`) |
| `--max-trade-erg X` | cap on any executed trade (`MAX_TRADE_SIZE_ERG`) |
| `--log-level L` | level of `arbitrage.log` (rotated at midnight, 14 days kept) |
| `--db PATH` | tracker database |
| `--no-wallet` | hide the wallet panel and wallet analysis |

## Arbitrage Strategies

### Strategy 1: SigmaUSD Bank Mint -> DEX Sell

When the bank's oracle price values ERG higher than the DEX pool price:

```
  Oracle:   1 ERG = $0.32 (SigUSD via bank)
  Spectrum: 1 ERG = $0.29 (SigUSD via pool)

  Step 1: Send 10 ERG to SigmaUSD Bank, mint SigUSD
          Receive: 10 * $0.32 * (1 - 2.2% fee) = 3.13 SigUSD
  Step 2: Swap 3.13 SigUSD -> ERG on Spectrum
          Receive: 3.13 / $0.29 * (1 - 0.5% pool) - miner fee = ~10.7 ERG (direct pool swap)
  Result:  10 ERG -> ~9.9 ERG (fees eat the spread in this example)

  RESTRICTION: Bank minting requires reserve ratio > 400%
```

### Strategy 2: DEX Buy -> SigmaUSD Bank Redeem

When the DEX pool prices ERG higher than the bank's oracle rate:

```
  Spectrum: 1 ERG = $0.35 (SigUSD)
  Oracle:   1 ERG = $0.32

  Step 1: Swap 10 ERG -> SigUSD on Spectrum (-0.5% pool, direct swap: miner fee only)
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
| Spectrum service fee | none (direct pool swap) | ~0.785 ERG only with `POOL_SWAP_ROUTE=crux` |
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

### Discord messages (on-chain paths)

The on-chain paths (pool ↔ bank) use the same exact sizing as the dashboard and the live gate, not the scan streaks above:

- **One message per opportunity.** A path that stays GO (and meets both minimums) for `DISCORD_CONFIRM_SECONDS` posts one embed. While it stays open, that same message is edited at most every `DISCORD_EDIT_SECONDS`, and only when profit moves by more than 0.1 percentage points. Once the path has not qualified for `DISCORD_CLOSE_SECONDS`, the message turns grey and shows how long it lasted, the peak and the last profit, plus the live trade result if there was one. Opens at or above the Tier 1 % ping you.
- **Health alerts**, each followed by a recovery message:
  - chain state unreadable for 2 min or more (ping)
  - a CEX venue down for 5 min or more
  - an oracle update stuck for 10 min or more
  - live trading paused (ping)
  - a failure that continues is re-alerted every 30 min
- **Daily digest** at `DISCORD_DIGEST_HOUR` (local time), once a day, also after a restart. It shows the past 24 h: episodes per path, potential ERG, trades, outages and the wallet.
- **Bank mint gate.** When the SigmaUSD bank lets you mint again (reserve ratio above 400% with room for at least `MINT_GATE_MIN_ROOM_ERG`), one message is posted (ping at most once an hour) and edited when minting has stayed closed for `MINT_GATE_CLOSE_SECONDS`, so minting the room away and reopening a block later does not post a new pair of messages. If the bot stops while minting is open, the message is marked "bot stopped" (also after a crash, on the next start). The dashboard Bank line and the daily digest show the room, or the ERG price at which minting would open (e.g. `mint ✗ needs ERG $0.392 (+21.7%)`). A change needs `MINT_GATE_CONFIRM_POLLS` agreeing polls in a row; after a restart an open gate is reported again.
- Discord is sent from a background queue (bounded, with HTTP 429 retry), so a slow or unreachable Discord never delays the 2 s chain poll.
- The CEX/USE paths still use the streak/cooldown flow described above.

```env
DISCORD_CONFIRM_SECONDS=10             # Held this long before a message opens
DISCORD_CLOSE_SECONDS=10               # Gone this long before it closes
DISCORD_EDIT_SECONDS=30                # At most one edit per this
DISCORD_HEALTH_CHAIN_SECONDS=120       # Chain unreadable -> alert (ping)
DISCORD_HEALTH_VENUE_SECONDS=300       # CEX venue down -> alert
DISCORD_HEALTH_ORACLE_SECONDS=600      # Oracle update pending -> alert
DISCORD_HEALTH_REPEAT_SECONDS=1800     # Re-alert interval while still failing
DISCORD_DIGEST_HOUR=8                  # Local hour for the daily digest, -1 = off
MINT_GATE_CONFIRM_POLLS=3              # Agreeing polls before a mint open/close alert
MINT_GATE_MIN_ROOM_ERG=1               # Smaller mint room counts as closed (default MIN_TRADE_SIZE_ERG)
MINT_GATE_PING_COOLDOWN_SECONDS=3600   # At most one mint-open ping per this
MINT_GATE_CLOSE_SECONDS=600            # Minting shut this long before the message says closed
```

**Privacy:** the webhook receives your wallet balances and your Discord user id. Post it to a private channel.

## Setup

### Prerequisites

- Python 3.11+ (CI tests 3.11 and 3.12)
- An Ergo node with a wallet (can be on a local VM), and its API key
- (Optional) a Discord webhook URL for notifications
- (Optional) NonKYC / Kucoin API keys. The bot is on-chain only by default (`ENABLE_CEX=false`);
  the CEX prices are shown watch-only and need no keys.

### Installation

Use a virtual environment (venv). Recent Ubuntu/Debian versions refuse a system-wide
`pip install`, and a venv keeps the bot's packages separate from everything else. You create it
once.

**Linux** (on Ubuntu the command is `python3`; inside the venv it is plain `python`):

```bash
cd ergo-arbitrage
sudo apt install python3-venv        # only if the next line says venv is missing
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows** (PowerShell):

```powershell
cd ergo-arbitrage
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

In every new terminal, activate the venv again (`source .venv/bin/activate`) before running the
bot, or skip activation and call the venv's Python directly: `.venv/bin/python main.py --notify`
(Windows: `.venv\Scripts\python main.py --notify`). A shell alias saves typing:

```bash
alias arb='cd ~/ergo-arbitrage && .venv/bin/python main.py'   # in ~/.bashrc, then: arb --notify
```

### Updating

```bash
# stop the bot first (Ctrl+C), then:
git pull
pip install -r requirements.txt      # inside the venv; only needed when requirements.txt changed
```

The tracker database (`arbitrage_tracker.db`) is not in git and is upgraded in place on start.

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
python main.py              # monitor (dashboard)
python main.py --notify     # + Discord
python main.py --live       # + auto-execute
python main.py --once --plain   # one scan, classic output

# Run tests (unit only; add -m live for the tests that call your node and public APIs)
python -m pytest -q
```

## Project Structure

```
ergo_arbitrage/
├── .env / .env.example     # settings and secrets (.env is gitignored)
├── config.py               # settings, fee constants, token IDs
├── main.py                 # bot entry point (monitor / --notify / --live)
├── arb.py                  # wallet tool: balance, quote, swap, redeem, send, arb
├── logging_config.py       # rich console + rotating arbitrage.log
├── FLOWS.md                # detailed swap flow reference
├── arbitrage/
│   ├── scanner.py          # full scans + 2 s chain poll, live gate, Discord wiring
│   ├── sizing.py           # exact trade sizing on the real pool/bank/oracle boxes
│   ├── optimizer.py        # size search helpers
│   ├── calculator.py       # fee-aware profit math for the full-scan paths
│   ├── venues.py           # venue status rows (pool, bank, oracle, CEX)
│   ├── dashboard_state.py  # what the dashboard and --json show
│   └── dashboard_view.py   # rich dashboard layout
├── ergo/
│   ├── chain_state.py      # reads pool, bank and oracle boxes (mempool aware)
│   ├── chain_arb.py        # two-leg arb transaction building
│   ├── arb_runner.py       # executes an arb: leg 1, leg 2, recovery
│   ├── pool_swap.py        # direct ErgoDEX pool swaps
│   ├── sigmausd_tx.py      # bank mint / redeem transactions
│   ├── tx_guard.py         # refuses any transaction that pays someone unexpected
│   └── signing.py, wallet.py, chain.py, codec.py, amounts.py, actions.py, progress.py
├── exchanges/              # price sources: ergo_node, spectrum, sigmausd, dexy, crux, kucoin, nonkyc
├── notifications/
│   ├── discord.py          # webhook client, background send queue
│   ├── embeds.py           # Discord embed layouts
│   ├── episodes.py         # one message per on-chain opportunity
│   ├── health.py           # health alerts and recoveries
│   └── digest.py           # daily digest
├── tracker/
│   └── profit_tracker.py   # SQLite: snapshots, opportunities, episodes, trades, stats
├── tests/                  # pytest (unit by default; -m live for node/API tests)
├── execute_*.py            # standalone swap scripts (dry run unless --execute)
└── archive/                # retired scripts, kept for reference
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
| ...for N chain polls in a row (~2 s apart) | `LIVE_CONFIRM_POLLS` (2) |
| Chain state readable, no oracle update pending | node polled every `CHAIN_POLL_SECONDS` (2) |
| Size capped | `MAX_TRADE_SIZE_ERG`, wallet minus `LIVE_ERG_RESERVE` (1) |
| Node synced and wallet unlocked | checked before each trade |
| Kill switch file absent | `LIVE_STOP_FILE` (`STOP`) |
| Wallet value drawdown since start | `LIVE_MAX_DRAWDOWN_ERG` (5) |
| Time since last trade | `LIVE_TRADE_COOLDOWN_SECONDS` (300) |
| Trades today | `LIVE_MAX_TRADES_PER_DAY` (10) |

**Chain watcher.** Pool, bank and oracle boxes come from your node every `CHAIN_POLL_SECONDS`
(2 s), mempool included: a pending pool or bank transaction is priced right away, and trades chain
onto it instead of conflicting with it. The oracle box is only a data input of the bank
transaction, so it is always read from the confirmed state; while an oracle update is pending,
nothing trades until it confirms (about one block). Each poll re-runs the exact sizing and the live
gate; the tables, SQLite logging and Discord stay on `SCAN_INTERVAL_SECONDS`. If the node cannot be
read, nothing trades until it can. A `CHAIN` line is printed whenever a contract box changes.

Each scan prints `LIVE: not trading - <reasons>` while any check fails. A leg-2 failure or a
TX-guard refusal pauses live trading until restart and is sent to Discord with the redeem
command to finish.

### First live run

1. **Wallet unlocked.** Live mode signs with your node wallet. Unlock it in the node panel
   (`http://YOUR_NODE_IP:9053/panel`) or with `POST /wallet/unlock` (body
   `{"pass": "<wallet password>"}`, header `api_key`). The dashboard shows `wallet ●` when it is
   unlocked, and live mode will not trade while it is locked.
2. **Check the pieces without spending.** `python arb.py balance` shows what the bot will trade
   with. `python arb.py arb --check` builds both legs at the best size, and your node validates
   them without broadcasting.
3. **Start small and watch.** `python main.py --live --max-trade-erg 5`. A trade only happens when
   a path shows **GO** for `LIVE_CONFIRM_POLLS` polls; until then the Live panel lists why it is not
   trading. Every trade, success or failure, is sent to Discord with a ping.
4. **Kill switch.** Create a file named `STOP` in the bot folder (`touch STOP`, or
   `New-Item STOP` on Windows) and no new trade starts; delete it to resume. Ctrl+C stops the bot.
5. **If leg 2 fails**, you hold SigUSD and live trading pauses. The Discord message and the Live
   panel give the exact command to finish (`python arb.py redeem --sigusd ... --execute`).

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
- **opportunity_episodes** - Continuous runs of a profitable path, from the 15 s full scans
- **chain_episodes** - The on-chain opportunities sent to Discord (opened, closed, peak, trade)
- **meta** - Small key/value state, e.g. the date the last daily digest was sent

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
- **Crux service fee is ~0.785 ERG flat** - only paid on Crux-routed swaps (USE LP, or SigUSD with `POOL_SWAP_ROUTE=crux`); SigUSD pool swaps go direct and pay only the miner fee
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
| **B** | Pool buy SigUSD -> Bank redeem | Executed by `--live` (direct pool swap, then bank redeem) | First live trade pending
| **C** | Mew buy SigUSD -> Crux sell | Both legs tested separately | High round-trip fees (~1.7 ERG) |
| **A** | Bank mint SigUSD -> Spectrum sell | Neither leg executable | RR < 400% blocks mint |
| **G** | CEX <> DEX cross-venue | Monitor only | SigUSD depeg, no exec code |
| **H** | CEX <> CEX | Monitor only | No execution code |

---

## Status and Roadmap

**Working today**
- On-chain arbitrage between the ErgoDEX SigUSD/ERG pool and the SigmaUSD bank (pool buy -> bank
  redeem, and bank mint -> pool sell while the reserve ratio allows minting), executed by `--live`
  with direct pool swaps (no service fee), exact sizing and the TX guard
- Chain watcher: pool, bank and oracle read from your node every 2 s, mempool aware
- One-screen dashboard, plus `--plain` and `--json` views
- Wallet tool `arb.py` (balance, quote, swap, redeem, send, arb), dry run by default
- Discord: one message per opportunity, health alerts, daily digest, wallet analysis
- SQLite tracking of prices, opportunities, episodes and trades
- CEX prices (Kucoin, NonKYC), watch-only

**Next**
- First supervised `--live` trade with a small `--max-trade-erg`
- Alert when the bank mint opens again (reserve ratio back above 400%): issue #39

**Later** (details in the GitHub issues)
- CEX paths: fix the profit math (#2, #3), then order placement and withdrawals
- More price sources: Gate.io and MEXC (#40), MachinaFi (#12)
- Faster scans (#8), a web dashboard with history charts (#16), a venue/leg architecture (#13)
