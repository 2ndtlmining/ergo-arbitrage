# Ergo Arbitrage Monitor

A Python application that monitors price differences across centralized exchanges (CEX), decentralized exchanges (DEX), and the SigmaUSD bank on the Ergo blockchain. The goal is simple: **end up with more ERG**.

**New here?** Follow [Getting started](#getting-started) from install to the first launch. Keeping it
running: [Running unattended](#running-unattended), [Updating](#updating), [Backups](#backups),
[Troubleshooting](#troubleshooting). Discord: [setup](#discord-setup) and
[messages at a glance](#messages-at-a-glance). Every setting: [Settings reference](#settings-reference).

## How It Works

The app continuously scans prices from multiple sources and calculates whether an arbitrage opportunity exists after accounting for **all fees, slippage, and transaction costs**. No trade is recommended unless the math shows a net profit in ERG.

### Price Sources

| Source | Type | Pair | Notes |
|--------|------|------|-------|
| **ErgoDEX (Spectrum) pool** | AMM pool | ERG/SigUSD | Pool box read from your node every 2 s; 0.5% pool fee; traded with direct pool swaps |
| **SigmaUSD bank** | Protocol | ERG/SigUSD | Oracle-priced, ~2.23% fee; minting closes when the reserve ratio would fall below 400% |
| **Oracle** | Price feed | ERG/USD | The SigmaUSD oracle pool box, read from your node |
| **Crux Finance / Dexy** | LP + mint | ERG/USE | Off (`ENABLE_USE=false`): the USE LP was drained, a token migration is expected |
| **Kucoin, NonKYC, Gate, MEXC** (SafeTrade opt-in) | CEX | ERG/USDT | Public order books, watch-only, no API keys |

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

- **Header + health strip:** node, wallet, poll timing; then each source's response time (chain read,
  every exchange), the full-scan time, the Discord send queue and the age of the chain data. Slow,
  stale or backed-up items turn yellow, then red.
- **Prices:** pool, oracle and bank, with the mint gate (`mint ✗ needs ERG $0.393 (+21.7%)`) and a
  reserve-ratio trend over the last 6 hours (kept in memory, so it starts empty after a restart).
- **Live / Wallet / Paths:** what live mode would do and why not, the wallet, every path with its best size.
- **Events | History:** recent events, and the last opportunity episodes and live trades from the database.
- **Venues | Exchanges:** the on-chain venues; and every exchange's bid/ask, gap to the oracle, fees
  (`*` = published by the exchange, otherwise the configured default), response time and the best
  cross-exchange spread after fees.

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
┌──── Exchanges (watch-only, * = fee published by the exchange) ───────────┐
│ exchange      bid     ask  vs oracle  taker · withdrawal              ms │
│ Kucoin     0.3238  0.3242     +0.06%  0.10% · 2.0 ERG*               180 │
│ NonKYC     0.3261  0.3267     +0.80%  0.20% · 3.1 ERG*               240 │
│ best spread: buy on Kucoin (ask $0.3242), sell on NonKYC (bid $0.3261) … │
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
| `--yes` | arm `--live` without typing `LIVE` (systemd and other unattended runs) |

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
  Result:  10 ERG -> ~10.7 ERG before price impact (a 10% price gap; fees take ~2.7% of it)

  RESTRICTION: minting is refused if the bank's reserve ratio after the mint would be below 400%
```

### Strategy 2: DEX Buy -> SigmaUSD Bank Redeem

When the DEX pool prices ERG higher than the bank's oracle rate:

```
  Spectrum: 1 ERG = $0.35 (SigUSD)
  Oracle:   1 ERG = $0.32

  Step 1: Swap 10 ERG -> SigUSD on Spectrum (-0.5% pool, direct swap: miner fee only)
          Receive: 10 * 0.35 * (1 - 0.5%) = 3.48 SigUSD
  Step 2: Redeem 3.48 SigUSD at the bank (-2.23% bank fee, -0.0021 ERG receipt box + miner fee)
          Receive: 3.48 / $0.32 * (1 - 2.23%) = ~10.6 ERG
  Result:  10 ERG -> ~10.6 ERG before price impact; with a gap under ~2.7% fees eat it all

  NOTE: SigUSD redeem has no reserve-ratio restriction (pro-rata payout if RR < 100%)
```

### Strategy 3: CEX <> DEX (Cross-Venue)

When an exchange prices ERG differently than the pool. **Watch-only**, and it assumes 1 SigUSD = 1 USDT,
which is usually not true (SigUSD trades off its peg), so a gap is a signal, not a trade.

### Strategy 4: CEX <> CEX

When ERG is priced differently on two exchanges: buy on the cheaper one, withdraw, sell on the other.
**Watch-only:** the `WATCH spread` line shows it after both taker fees, the ERG withdrawal fee and
`CEX_USDT_TRANSFER_FEE`.

### Strategy 5: USE (DexyUSD) Mint -> LP Sell

**Off** (`ENABLE_USE=false`) since the USE LP was drained; kept for after the USE migration. When USE
can be minted at the oracle rate and sold via the Crux LP at a higher effective rate:

```
  Step 1: Mint USE via Crux Finance (oracle rate, ~0.79 ERG flat fee)
  Step 2: Sell USE -> ERG via Crux LP (0.3% pool fee, ~0.785 ERG service fee)
  Result:  Profit if LP rate > mint rate + fees
```

## Fee Accounting

Every opportunity calculation includes ALL of these costs:

| Fee | Amount | Applies To |
|-----|--------|-----------|
| Fee | Amount | Applies To |
|-----|--------|-----------|
| Pool fee | 0.5% (read from the pool box) | SigUSD/ERG pool swaps |
| Pool service fee | none (direct pool swap) | ~0.785 ERG per leg only with `POOL_SWAP_ROUTE=crux` |
| Price impact | exact, from the pool reserves, plus `EXECUTION_BUFFER` (0.3%) | Pool swaps |
| SigmaUSD protocol fee | 2.0% | Bank mint/redeem (stays in reserve) |
| SigmaUSD frontend fee | 0.229% | Bank mint/redeem |
| Bank redeem receipt box | 0.001 ERG | Bank redeem |
| Ergo miner fee | 0.0011 ERG | Every on-chain transaction |
| Crux LP / mint (USE, off) | 0.3% + ~0.785 ERG / ~0.79 ERG | USE paths |
| CEX taker fee (watch-only) | Kucoin 0.1%, NonKYC 0.2%, Gate 0.2%, MEXC 0.08% (refreshed live where published) | CEX paths |
| CEX ERG withdrawal (watch-only) | Kucoin 2.0, NonKYC 3.1 (read live), Gate 0.403, MEXC 0.1 ERG | Moving ERG off an exchange |

### Slippage Tiers

Only for legs without a known order depth (the CEX legs). Pool legs use the exact reserves.

| Trade Size | Estimated Slippage |
|-----------|-------------------|
| Up to 10 ERG | 0.5% |
| Up to 50 ERG | 1.0% |
| Up to 100 ERG | 2.0% |
| Up to 500 ERG | 3.0% |

## Discord Notifications

`--notify` and `--live` post to a Discord channel through a webhook. The bot never reads Discord;
it only posts and edits its own messages.

### Discord setup

1. **Webhook.** In your server, open the channel's settings (gear icon) -> **Integrations** ->
   **Webhooks** -> **New Webhook**. Name it, then **Copy Webhook URL**. Use a private channel: the
   messages include your wallet balances.
2. **Your user ID** (for @mentions on the important alerts): **User Settings** -> **Advanced** ->
   switch on **Developer Mode**. Then right-click your own name (in the member list or on a message)
   -> **Copy User ID**. It is a long number such as `123456789012345678`.
3. Put both in `.env`:

   ```env
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<id>/<token>
   DISCORD_USER_ID=123456789012345678
   ```

   To post into a thread of a forum or text channel, add `?thread_id=<thread id>` to the webhook URL
   (copy the thread's ID the same way, with Developer Mode on).
4. Check it: `python arb.py doctor` shows the webhook's name without posting anything
   (`OK discord webhook "arb-bot" (channel ...)`). Then `python main.py --notify` posts
   **Ergo arbitrage started · NOTIFY**.

The webhook URL is a password: anyone who has it can post to your channel. If it leaks, delete the
webhook in the channel settings and create a new one.

### Messages at a glance

| Message | Ping | Meaning | What to do |
|---|---|---|---|
| **Ergo arbitrage started · NOTIFY** (or `· LIVE`, red) | | The bot started; shows the alert thresholds, max trade and digest hour. | Nothing. A `LIVE` you did not start means live mode is running. |
| **Ergo arbitrage stopped** | | Clean shutdown, with the session's counts. | Nothing, unless you did not stop it (see **Troubleshooting**). |
| **OPEN · pool→redeem +1.23%** (green) | at or above `DISCORD_TIER1_PROFIT_PERCENT` | An on-chain path has stayed profitable for `DISCORD_CONFIRM_SECONDS` at its best size. Edited while it lasts. | `--notify`: decide whether to trade it by hand (`python arb.py arb --check`, then `--execute`). `--live` trades it by itself when every check passes. |
| **Closed · pool→redeem after 4m · peak +1.40%** (grey) | | The same message once the path has not qualified for `DISCORD_CLOSE_SECONDS`: duration, peak, last profit, and the live trade result if there was one. | Nothing. |
| **Closed · pool→redeem · bot restarted** (grey) | | An episode that was open when the bot stopped or crashed, closed at the next start. | Nothing; a new OPEN follows if the path still qualifies. |
| **⚠️ Chain state unreadable for over 2m: ...** (red) | yes | The node gives no usable pool/bank/oracle data (down, syncing, stalled, extra index missing). Nothing is priced or traded. | Run `python arb.py doctor`; see **Troubleshooting**. |
| **⚠️ Kucoin down for over 5m: ...** | | A watch-only exchange is not answering. | Nothing; it only affects the CEX watch. |
| **⚠️ Oracle update pending for over 10m (stuck?)** | | An oracle update is in the mempool and not confirming. Live mode waits. | Usually nothing; it clears when the update confirms. |
| **⚠️ Node wallet locked for over 2m: live mode cannot trade** (`--live` only) | yes | The node restarted and locked its wallet. | Unlock it (`/wallet/unlock`, see **Node setup**). |
| **⚠️ Live trading stopped: drawdown ...** | yes | The wallet value fell more than `LIVE_MAX_DRAWDOWN_ERG` since the start of the day; nothing trades until tomorrow. | Look at the trades (`History` panel, `python arb.py balance`) before letting it run on. |
| **⚠️ Live trading blocked on implausible data: ...** | yes | A path shows more profit than `LIVE_MAX_PROFIT_PERCENT`, or the oracle is far from the exchanges' prices: likely bad data, so nothing trades. | Compare the pool, bank and oracle with another source (`python arb.py quote`, an explorer). If it is real, trade it by hand with `arb.py arb --force`. |
| **⚠️ Live trading done for today: max N trades per day reached** | | `LIVE_MAX_TRADES_PER_DAY` reached. | Nothing; it resumes tomorrow. |
| **⚠️ Live trading held for over 10m: STOP file present** | | The kill switch is on. | Delete `STOP` when you want it to trade again. |
| **⚠️ Full scan failing for over 2m: ...** / **⚠️ Database writes failing ...** | | Something keeps failing every poll (see `arbitrage.log`), or the database is locked, full or corrupt; prices still update but nothing is recorded. | Read the error; check the disk (`df -h`) and the database. |
| **⚠️ Live trading paused: ...** | | Live mode stopped trading after a failure. The failure itself was the pinged **LIVE** message. | Read the LIVE message and fix the cause; then `python arb.py resume` and restart the bot. |
| **✅ ... recovered after 7m** (green) | | The failure above has ended. | Nothing. |
| **LIVE**: executed ... / did not execute ... / LEG 2 FAILED ... / UNEXPECTED ERROR ... | yes, except "not profitable" | Result of a live trade attempt. "Nothing was spent" means nothing left the wallet. | On **LEG 2 FAILED** or **UNEXPECTED ERROR** run the command the message gives (e.g. `python arb.py redeem --sigusd all --execute`) and check `python arb.py balance`. |
| **Bank mint OPEN · RR 412%** (green) | at most once per `MINT_GATE_PING_COOLDOWN_SECONDS` | The SigmaUSD bank lets you mint again, with the room in ERG. | A mint -> pool sell path may open; watch for an OPEN message. |
| **Bank mint closed · open for 3h** (grey) | | The same message once minting has stayed shut for `MINT_GATE_CLOSE_SECONDS`. | Nothing. |
| **Bank mint · bot stopped** (grey) | | The bot stopped while minting was open, so it no longer knows. | Nothing; the next start reports the gate again. |
| **Daily digest · last 24h** (blue) | | Once a day at `DISCORD_DIGEST_HOUR`: episodes per path, potential ERG, trades, outages, wallet. | Read it; it is the quickest way to see if the bot is earning anything. |
| **Wallet** (blue) | | Wallet balances and value, every `DISCORD_WALLET_COOLDOWN_SECONDS`. | Nothing. |
| **CEX watch-only: CEX** | | An exchange is far from the pool price, or the cross-exchange spread pays after every fee. At most once per `CEX_WATCH_COOLDOWN_SECONDS`. | Informational: no CEX is connected, nothing can trade it. |
| **Scan #123 - 12:00:00 \| 0 profitable paths** | | The text summary of all paths, every `DISCORD_SUMMARY_INTERVAL_SECONDS`. | Nothing. |
| **Arbitrage Opportunity Found** | at or above `DISCORD_TIER1_PROFIT_PERCENT` | Scan-based alert for the CEX/USE paths (both off by default). | See the path text; these paths are not traded automatically. |

### How the on-chain alerts work

The on-chain paths (pool ↔ bank) use the same exact sizing as the dashboard and the live gate:

- **One message per opportunity.** A path that stays GO (and meets both minimums) for `DISCORD_CONFIRM_SECONDS` posts one embed. While it stays open, that same message is edited at most every `DISCORD_EDIT_SECONDS`, and only when profit moves by more than 0.1 percentage points. Once the path has not qualified for `DISCORD_CLOSE_SECONDS`, the message turns grey and shows how long it lasted, the peak and the last profit, plus the live trade result if there was one. Opens at or above the Tier 1 % ping you.
- **Health alerts**, each followed by a recovery message:
  - chain state unreadable for 2 min or more (ping)
  - a CEX venue down for 5 min or more
  - an oracle update stuck for 10 min or more
  - live trading paused (no ping: the **LIVE** message for the failure behind it already pinged)
  - `--live`: the node wallet locked for 2 min or more (ping), the drawdown limit hit (ping), the
    day's trade cap reached, the STOP file holding trades for `DISCORD_HEALTH_LIVE_SECONDS`
  - the full scan failing, or database writes failing, for 2 min or more
  - a failure that continues is re-alerted every 30 min
- **Daily digest** at `DISCORD_DIGEST_HOUR` (local time), once a day, also after a restart. It shows the past 24 h: episodes per path, potential ERG, trades, outages and the wallet.
- **Bank mint gate.** When the SigmaUSD bank lets you mint again (reserve ratio above 400% with room for at least `MINT_GATE_MIN_ROOM_ERG`), one message is posted (ping at most once an hour) and edited when minting has stayed closed for `MINT_GATE_CLOSE_SECONDS`, so minting the room away and reopening a block later does not post a new pair of messages. If the bot stops while minting is open, the message is marked "bot stopped" (also after a crash, on the next start). The dashboard Bank line and the daily digest show the room, or the ERG price at which minting would open (e.g. `mint ✗ needs ERG $0.392 (+21.7%)`). A change needs `MINT_GATE_CONFIRM_POLLS` agreeing polls in a row; after a restart an open gate is reported again.
- Discord is sent from a background queue (bounded, with HTTP 429 retry), so a slow or unreachable Discord never delays the 2 s chain poll.

```env
DISCORD_MIN_PROFIT_PERCENT=1.0         # Min % to post (default 1.0)
DISCORD_MIN_PROFIT_ERG=0.5             # Min absolute ERG profit (default 0.5); both must be met
DISCORD_TIER1_PROFIT_PERCENT=2.0       # Ping at or above this % (default 2.0)
DISCORD_CONFIRM_SECONDS=10             # Held this long before a message opens
DISCORD_CLOSE_SECONDS=10               # Gone this long before it closes
DISCORD_EDIT_SECONDS=30                # At most one edit per this
DISCORD_HEALTH_CHAIN_SECONDS=120       # Chain unreadable -> alert (ping)
DISCORD_HEALTH_VENUE_SECONDS=300       # CEX venue down -> alert
DISCORD_HEALTH_ORACLE_SECONDS=600      # Oracle update pending -> alert
DISCORD_HEALTH_REPEAT_SECONDS=1800     # Re-alert interval while still failing
DISCORD_HEALTH_LIVE_SECONDS=600        # STOP file holding --live this long -> alert
DISCORD_DIGEST_HOUR=8                  # Local hour for the daily digest, -1 = off
MINT_GATE_CONFIRM_POLLS=3              # Agreeing polls before a mint open/close alert
MINT_GATE_MIN_ROOM_ERG=1               # Smaller mint room counts as closed (default MIN_TRADE_SIZE_ERG)
MINT_GATE_PING_COOLDOWN_SECONDS=3600   # At most one mint-open ping per this
MINT_GATE_CLOSE_SECONDS=600            # Minting shut this long before the message says closed
```

### Scan-based alerts (CEX/USE paths only)

The CEX and USE paths (both off by default) still use the older scan flow:

1. **Streak confirmation**: a path must be profitable for `DISCORD_CONFIRM_SCANS` scans in a row (default 3 = 45 seconds) before it posts.
2. **Minimum thresholds**: both `DISCORD_MIN_PROFIT_PERCENT` and `DISCORD_MIN_PROFIT_ERG`.
3. **Tiers**: at or above `DISCORD_TIER1_PROFIT_PERCENT` with no SigUSD = USDT assumption pings you; below that it posts silently.
4. **Per-path cooldown**: `DISCORD_COOLDOWN_SECONDS` (default 300) between posts for the same path.
5. **Price staleness guard**: a path whose prices are older than `PRICE_STALE_SECONDS` is skipped.

```env
DISCORD_CONFIRM_SCANS=3                # Scans in a row before posting (CEX/USE paths)
DISCORD_COOLDOWN_SECONDS=300           # Per-path cooldown (CEX/USE paths)
DISCORD_WALLET_COOLDOWN_SECONDS=600    # Wallet message interval
DISCORD_SUMMARY_INTERVAL_SECONDS=1800  # Text summary interval
PRICE_STALE_SECONDS=60                 # Max price age before skipping
```

**Privacy:** the webhook receives your wallet balances and your Discord user id. Post it to a private channel.

## CEX prices (watch-only)

With `CEX_WATCH=true` (the default) the bot reads the public ERG/USDT order books of **Kucoin,
NonKYC, Gate.io and MEXC** in one go each full scan (no API keys) and shows them in the dashboard's
Venues panel. **SafeTrade** (safe.trade) is listed too but off by default
(`SAFETRADE_ENABLED=true` to try it): from many connections Cloudflare answers its API with a
captcha page, shown as "blocked by Cloudflare".

- **Fees.** Each exchange's taker fee and ERG withdrawal fee come from what it publishes (refreshed
  hourly: Kucoin and NonKYC withdrawal fees, Gate and MEXC taker fees) or else from the settings
  below. Gate's and MEXC's ERG withdrawal fees are not public; check them on the exchange.
- **Cross-exchange spread.** A `WATCH spread` line shows the best "buy on one exchange, withdraw the
  ERG, sell on another" at the size your wallet can fund, after the taker fee on both trades, the ERG
  withdrawal fee and `CEX_USDT_TRANSFER_FEE` to move the USDT back. A gap smaller than those fees is
  never shown as profitable. At a ~20 ERG wallet the withdrawal fees alone are several percent.
- **Discord.** At most one combined watch message per `CEX_WATCH_COOLDOWN_SECONDS`: every exchange's
  gap to the pool, plus the best spread.
- **Nothing here trades.** All CEX paths are watch-only (no SigUSD<->USDT venue to close the loop).

```env
GATE_TRADING_FEE=0.002        GATE_ERG_WITHDRAW_FEE=0.403
MEXC_TRADING_FEE=0.0008       MEXC_ERG_WITHDRAW_FEE=0.1
SAFETRADE_ENABLED=false       SAFETRADE_TRADING_FEE=0.002      # SAFETRADE_ERG_WITHDRAW_FEE=
CEX_USDT_TRANSFER_FEE=1.0     TRADE_SIZES_UNFUNDED=false
```

The analysis grid (`TRADE_SIZES`) only prices sizes your wallet can fund, plus that funded size
itself; `TRADE_SIZES_UNFUNDED=true` prices the whole grid again (marked `*` above `MAX_TRADE_SIZE_ERG`).

## Getting started

From a fresh Ubuntu machine to a bot that watches the chain and posts to Discord. Every block is
copy-paste; run them in order.

### 1. Install

```bash
sudo apt update && sudo apt install -y git python3-venv
cd ~
git clone https://github.com/2ndtlmining/ergo-arbitrage.git
cd ergo-arbitrage
python3 --version
```

The bot needs **Python 3.11 or newer** (tested on 3.11 and 3.12). Ubuntu 24.04 ships 3.12: go on
to the venv. **Ubuntu 22.04 ships 3.10**; install 3.11 next to it (the system Python stays as it is)
and use `python3.11` instead of `python3` in the next block:

```bash
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update && sudo apt install -y python3.11 python3.11-venv
```

Create the virtual environment (venv) once. It keeps the bot's packages separate; recent Ubuntu
refuses a system-wide `pip install`.

```bash
python3 -m venv .venv              # 22.04: python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Inside the venv the command is plain `python`. Either activate it in every new terminal
(`source .venv/bin/activate`) or call `.venv/bin/python` directly, as the examples below do. An
alias saves typing (adjust the folder if you cloned elsewhere):

```bash
echo "alias arb='cd ~/ergo-arbitrage && .venv/bin/python main.py'" >> ~/.bashrc && source ~/.bashrc
# then: arb --notify
```

**Windows** (PowerShell), for development:

```powershell
git clone https://github.com/2ndtlmining/ergo-arbitrage.git
cd ergo-arbitrage
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py --once --plain
```

### 2. Node setup

The bot reads prices from your own Ergo node and signs with its wallet. The node needs:

| | Why | Scala node (`ergo.conf`) | Rust node (`ergo-node.toml`) |
|---|---|---|---|
| Extra index | finds the pool, bank and oracle boxes (`/blockchain/*`) | `ergo.node.extraIndex = true` | `[indexer] enabled = true` |
| API key | wallet, signing | `scorex.restApi.apiKeyHash` | `[api.security] api_key_hash` |
| Reachable REST API | the bot's only data source | `scorex.restApi.bindAddress` | `[api] bind` (+ `public_bind`) |
| Wallet restored and unlocked | signs the trades (`--live`, `arb.py`) | `/wallet/restore`, `/wallet/unlock` | same routes |

The extra index needs a full archival node (the default for both) and builds once, in the
background, after it is switched on. Until it is ready `arb.py doctor` reports "chain state" as failed.

**API key.** Pick a random secret and hash it. The secret goes in the bot's `.env`; only the hash
goes in the node config.

```bash
secret=$(openssl rand -hex 32)
echo "ERGO_NODE_API_KEY=$secret"                       # for .env (step 3); keep it safe
printf '%s' "$secret" | b2sum -l 256 | cut -d' ' -f1   # the hash, for the node config
```

(A running Scala node can also hash it: `curl -s -X POST http://127.0.0.1:9053/utils/hash/blake2b -H "Content-Type: application/json" -d "\"$secret\""`.)

**Same machine or LAN.** If the bot runs on the node's machine, leave the API on `127.0.0.1` and
use `ERGO_NODE_URL=http://127.0.0.1:9053`. If the node is another machine in your LAN, bind the API
to that machine's LAN address and let only the bot's machine in. **Never port-forward the API port to
the internet:** the API key travels in plain HTTP, and the Rust node's transaction submission needs
no key at all. On the node machine, with `192.168.40.50` standing in for the bot's IP:

```bash
sudo ufw allow OpenSSH       # first, or enabling the firewall locks out your SSH session
sudo ufw allow from 192.168.40.50 to any port 9053 proto tcp
sudo ufw allow 9030/tcp      # peer-to-peer port, so the node can sync
sudo ufw enable
```

**Scala node**, in `ergo.conf` (merge into the sections you already have):

```hocon
ergo {
  node {
    extraIndex = true
  }
}
scorex {
  restApi {
    bindAddress = "0.0.0.0:9053"     # LAN; keep "127.0.0.1:9053" when the bot runs on this machine
    apiKeyHash = "<the hash from above>"
  }
}
```

**Rust node** ([arkadianet/ergo](https://github.com/arkadianet/ergo)), in its `ergo-node.toml`. Its
API defaults to port **9099**; the lines below keep the Scala port so the URL stays the same.
Check its `docs/configuration.md` for your version: its configuration may change before 1.0.

```toml
[indexer]
enabled = true

[api]
bind = "192.168.40.86:9053"   # the node's LAN address; "127.0.0.1:9053" when the bot runs on this machine
public_bind = true            # required for any non-loopback bind; leave it out on 127.0.0.1

[api.security]
api_key_hash = "<the hash from above>"   # 64 lowercase hex characters
```

The bot handles the one API difference it meets (the Rust node's mempool lookup by token is a POST)
by itself.

**Wallet.** Restore the wallet the bot trades with on the node, once. Use the node's web UI
(Scala: `http://<node>:9053/panel`; Rust: the dashboard at `http://<node>:<port>/`, Wallet), or the
API (the leading space keeps the mnemonic out of your shell history in most shells):

```bash
 curl -s -X POST http://127.0.0.1:9053/wallet/restore -H "api_key: $secret" -H "Content-Type: application/json" \
   -d '{"pass": "<wallet password>", "mnemonic": "<your words>", "mnemonicPass": ""}'
```

The node then scans the chain for the wallet's boxes; balances may be missing until it finishes. The
wallet is **locked again after every node restart**; unlock it each time:

```bash
 curl -s -X POST http://127.0.0.1:9053/wallet/unlock -H "api_key: $secret" -H "Content-Type: application/json" \
   -d '{"pass": "<wallet password>"}'
```

Use a dedicated wallet holding only what the bot may trade.

### 3. Configure

```bash
cp .env.example .env
nano .env
```

The minimum for `--notify`:

```env
ERGO_NODE_URL=http://192.168.40.86:9053    # or http://127.0.0.1:9053 on the node's machine
ERGO_NODE_API_KEY=<the secret from step 2>
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<id>/<token>
DISCORD_USER_ID=<your numeric Discord user id>   # optional: @mentions for the important alerts
```

Everything else has a sensible default; see the [settings reference](#settings-reference). Check
what the bot will use (secrets masked, typos and leftover placeholders flagged):

```bash
.venv/bin/python arb.py config
```

### 4. First launch

```bash
.venv/bin/python arb.py doctor            # settings, Discord webhook, node, wallet, index, mempool, sign check
.venv/bin/python main.py --once --plain   # one full scan, printed
.venv/bin/python main.py --notify         # the dashboard + Discord; Ctrl+C stops it
```

`arb.py doctor` prints one line per check, each failure with a hint; fix them top to bottom. Its last
check signs a 1 ERG trade and has the node validate it, without broadcasting it. When everything is
OK, `--notify` is safe to leave running: it never spends anything. Before `--live`, read
**Live mode** and **First live run** below.

### Running

```bash
python main.py              # monitor (dashboard)
python main.py --notify     # + Discord
python main.py --live       # + auto-execute
python main.py --once --plain   # one scan, classic output

# Run tests (unit only; add -m live for the tests that call your node and public APIs)
python -m pytest -q
```

### Running unattended

Started from an SSH session, the bot stops when the session closes. Use **one** of these, never
both at once: two copies of the bot would both post to Discord, and in `--live` both would trade.
The bot enforces this: a second `main.py` on the same database refuses to start (it holds a lock,
`arbitrage_tracker.db.lock`, that the OS releases when it exits or crashes). `main.py --once` can
still run next to it.

**tmux** keeps the full dashboard and lets you look at it at any time. It does not survive a reboot.

```bash
sudo apt install -y tmux
tmux new -s arb                                   # a terminal that keeps running
cd ~/ergo-arbitrage && .venv/bin/python main.py --notify
# detach: Ctrl+B, then D.  Back later: tmux attach -t arb.  Stop: Ctrl+C inside it.
```

**systemd** starts the bot at boot and restarts it after a crash. The full-screen dashboard needs a
terminal, so the service runs `--plain` with its output discarded; the log is `arbitrage.log` in the
bot folder (rotated at midnight, 14 days kept), and crashes land in the journal. Run this from the
bot folder; it fills in your user name and folder:

```bash
cd ~/ergo-arbitrage
sudo tee /etc/systemd/system/ergo-arb.service > /dev/null <<EOF
[Unit]
Description=Ergo arbitrage bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$PWD
ExecStart=$PWD/.venv/bin/python main.py --notify --plain
Environment=PYTHONUNBUFFERED=1
StandardOutput=null
StandardError=journal
Restart=always
RestartSec=30
TimeoutStopSec=1300

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ergo-arb
```

`TimeoutStopSec` is longer than `LEG2_WATCH_TIMEOUT_SECONDS` (1200) plus a minute, so a stop never
kills the bot while it follows leg 2 of a live trade.

```bash
systemctl status ergo-arb             # running? since when?
tail -f ~/ergo-arbitrage/arbitrage.log
journalctl -u ergo-arb -n 50          # start-up errors and crashes
sudo systemctl stop ergo-arb          # stop (Ctrl+C equivalent: finishes the scan, posts "stopped")
sudo systemctl restart ergo-arb       # after an update or a .env change
```

The bot reads `.env` itself; do not also point the unit's `EnvironmentFile=` at it (systemd parses
quotes and comments differently). Keep `--notify` in the unit until you have done the **First live
run** by hand; for `--live` the unit needs `--yes` (`main.py --live --yes --plain`), because there is
no terminal to type the confirmation in. In `--live`, create the STOP file (`touch STOP`) before stopping or updating, so a
restart cannot start a trade halfway through your maintenance; delete it afterwards.

### Updating

```bash
cd ~/ergo-arbitrage
sudo systemctl stop ergo-arb                 # or Ctrl+C in tmux
git pull
.venv/bin/pip install -r requirements.txt    # only needed when requirements.txt changed
.venv/bin/python arb.py config               # new settings appear with their defaults
.venv/bin/python arb.py doctor --no-sign     # the node is still fine
sudo systemctl start ergo-arb                # or start it again in tmux
```

If `git pull` refuses because you changed a file in the folder, keep your change aside and update:
`git stash`, `git pull`, then `git stash pop` (or `git stash drop` to discard your change). `.env`, the
database and the logs are not in git and are never touched.

The tracker database (`arbitrage_tracker.db`) is upgraded in place on start.

### Backups

`arbitrage_tracker.db` holds the history (episodes, trades, scans). It runs in WAL mode, so a plain
`cp` while the bot runs can miss recent rows. `arb.py backup` takes a consistent copy, safe while the
bot runs, into `backups/` and keeps the newest 14:

```bash
.venv/bin/python arb.py backup                           # backups/arbitrage_tracker-2026-10-04-0315.db
.venv/bin/python arb.py backup --to /mnt/nas/arb --keep 30
```

Every night at 03:15, from the bot folder:

```bash
(crontab -l 2>/dev/null; echo "15 3 * * * cd $PWD && .venv/bin/python arb.py backup >> backups.log 2>&1") | crontab -
crontab -l                                               # check it is there
```

Copy `backups/` to another machine now and then. To restore: stop the bot, copy a backup over
`arbitrage_tracker.db`, delete `arbitrage_tracker.db-wal` and `arbitrage_tracker.db-shm`, start the bot.

Back up **`.env`** separately and privately (a password manager is a good place): it holds your node
API key and webhook. Your wallet's mnemonic is what really matters; the node only holds a copy of it.

### Troubleshooting

`python arb.py doctor` names most problems with a fix; `python arb.py config` shows the settings in
use. Common messages:

| Message | Meaning | Fix |
|---|---|---|
| `Cannot reach your Ergo node at http://...` / doctor `FAIL node reachable` | Nothing answers at `ERGO_NODE_URL`. | Is the node running? Right IP and port (Rust default 9099)? A node on another machine must listen on its LAN address and allow this machine through its firewall (see **Node setup**). |
| doctor `FAIL API key ... HTTP 401` or `403` | The node refused `ERGO_NODE_API_KEY`. | Put the plain secret in `.env`, and its hash in the node config; restart the node after changing the hash. On the Rust node, no `api_key_hash` at all also gives 403. |
| doctor `FAIL wallet locked` / dashboard `wallet ●` red / live `node not ready (... unlocked=False)` | The node restarted; its wallet locks on every restart. | Unlock it (`/wallet/unlock`, see **Node setup**). |
| doctor `FAIL wallet no wallet on this node` | The wallet was never restored on this node (e.g. a new Rust node). | Restore it (see **Node setup**) and wait for the wallet scan. |
| `node syncing: N blocks behind` / doctor `WARN synced` | The node is still catching up; nothing is priced or traded until it is synced. | Wait. |
| `no new block for N min ... (node stalled or without peers?)` | The node's height has not moved for `CHAIN_STALL_SECONDS`. | Check the node's peers and its log; restart it. |
| doctor `FAIL chain state` / `HTTP 404 ... /blockchain/...` | The node's extra index is off or still building. | Scala `extraIndex = true`, Rust `[indexer] enabled = true`, then let it build. |
| `⚠️ Chain state unreadable` in Discord | Any of the four rows above, for over 2 minutes. | `python arb.py doctor`. |
| doctor `WARN discord webhook answered HTTP 404 Unknown Webhook` (or 401) / log `Discord POST returned HTTP 404` | The webhook was deleted or the URL is mistyped. | Create a new webhook and copy the URL again (see **Discord setup**). |
| log `Discord queue full: dropped the oldest message` | Discord was unreachable or rate-limiting for a while. | Nothing if it stops by itself; check the network if it repeats. |
| Venues panel `blocked by Cloudflare` | SafeTrade's API answered with a captcha page. | Normal for SafeTrade; leave `SAFETRADE_ENABLED=false`. |
| Live panel `STOP file present (...)` | The kill switch is on. | `rm STOP` in the bot folder to allow live trades again. |
| Live panel `paused after: ...` | A live trade failed, or the bot was stopped in the middle of one. The pause survives restarts. | Read the pinged **LIVE** message, check `python arb.py balance` (redeem leftover SigUSD), then `python arb.py resume` and restart the bot. |
| `Refusing to --execute with these settings` / `refusing to start --live` | A live setting is outside its safe range (listed with the limit). | Fix it in `.env`; `arb.py config` shows the values in use. |
| `--max-trade-erg X is above MAX_TRADE_SIZE_ERG` | The flag can only lower the cap. | Raise `MAX_TRADE_SIZE_ERG` in `.env` instead. |
| `another bot is already running on this database (pid ..., mode ...)` | A second `main.py` (or an `arb.py --execute` next to a `--live` bot). | Stop the other one (`tmux attach -t arb`, or `sudo systemctl stop ergo-arb`); `ps -p <pid>` shows it. `main.py --once` works next to it. |
| `Refusing to --execute: STOP file present` / `live mode is paused` | `arb.py arb --execute` honours the kill switch and the live pause. | Delete `STOP`, or finish the recovery and run `python arb.py resume`. `redeem`/`swap`/`send` still work. |
| `X is no longer used; set Y instead` | A setting was renamed. | Rename it in `.env`; `arb.py config` lists them. |
| `arb.py config`: `unknown ... did you mean ...?` | A typo in `.env`; the setting is silently not used. | Fix the name. |
| `error: externally-managed-environment` | `pip install` outside the venv. | Use `.venv/bin/pip install -r requirements.txt` (see **Install**). |
| `Command 'python' not found` | Ubuntu has only `python3` outside the venv. | Use `.venv/bin/python`, or activate the venv first. |
| `SyntaxError` or `TypeError ... unsupported operand type(s) for \|` at start | Python older than 3.11. | Create the venv with Python 3.11+ (see **Install**). |
| Windows: `... cannot be loaded because running scripts is disabled on this system` | PowerShell blocks `Activate.ps1`. | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or skip activation and use `.venv\Scripts\python`. |

## Settings reference

Settings live in `.env` in the bot folder (environment variables of the same name win). A blank
value, or one still holding a `your_..._here` placeholder, uses the default. `python arb.py config`
lists every setting with its value and source. Times are in seconds, amounts in ERG unless noted.

**Node**

| Setting | Default | Effect |
|---|---|---|
| `ERGO_NODE_URL` | `http://127.0.0.1:9053` | Your node's REST API. |
| `ERGO_NODE_API_KEY` | (none) | Plain secret whose hash is the node's API key hash. Needed for the wallet, signing and `--live`. |
| `ERGO_EXPLORER_API_URL` | `https://api.ergoplatform.com/api/v1` | Public explorer, the fallback when the node's extra index cannot find a box. |
| `CHAIN_POLL_SECONDS` | `2` | How often the node is polled for pool, bank, oracle and mempool changes. |
| `CHAIN_STALL_SECONDS` | `1200` | No new block for this long = node stalled: nothing trades and a health alert fires. |

**Venues**

| Setting | Default | Effect |
|---|---|---|
| `ENABLE_CEX` | `false` | Price the CEX paths with your exchange API keys (they never auto-trade). |
| `ENABLE_USE` | `false` | Price the USE (Dexy) paths. Off: the USE LP was drained. |
| `POOL_SWAP_ROUTE` | `direct` | SigUSD pool legs: `direct` (own pool swap, miner fee only) or `crux` (Crux API, ~0.785 ERG per leg). |
| `USE_TOKEN_ID` | current USE token | Set after the expected USE token migration. |
| `DEXY_USE_LP_NFT` | current USE LP | Set after the expected USE token migration. |
| `EXTRA_SERVICE_FEE_ERGO_TREES` | (none) | Extra service-fee ErgoTrees (comma separated) the transaction guard lets a third-party-built TX pay. |
| `NONKYC_API_KEY`, `NONKYC_API_SECRET` | (none) | NonKYC keys, only read with `ENABLE_CEX=true`. |
| `KUCOIN_API_KEY`, `KUCOIN_API_SECRET`, `KUCOIN_API_PASSPHRASE` | (none) | Kucoin keys, only read with `ENABLE_CEX=true`. |

**Sizing and profit**

| Setting | Default | Effect |
|---|---|---|
| `MIN_PROFIT_PERCENT` | `0.5` | A path below this is not an opportunity (and `--live` does not trade it). |
| `MAX_TRADE_SIZE_ERG` | `100` | Hard cap on anything executed. `main.py --max-trade-erg X` overrides it for one run. |
| `TRADE_SIZES` | `10,100,200,500,1000` | Sizes the analysis grid prices. Sizes above `MAX_TRADE_SIZE_ERG` are analysis only. |
| `TRADE_SIZES_UNFUNDED` | `false` | `true` prices the whole grid, not only the sizes the wallet can fund. |
| `MIN_TRADE_SIZE_ERG` | `1` | Smallest size the best-size search and the doctor's sign check use. |
| `SIZE_PROFIT_CAPTURE` | `0.95` | Trade the smallest size that earns this share of the best profit; `1.0` = maximise profit. |
| `SLIPPAGE_TOLERANCE` | `0.01` | Leg 2 must return at least the planned ERG minus this; `--live`/`--execute` require 0 < x <= 0.05. |
| `EXECUTION_BUFFER` | `0.003` | Margin for the pool moving between quote and inclusion. |
| `MAX_FEE_BUDGET_ERG` | `1.0` | Most service + miner fees the transaction guard allows per signed TX (0.002 to 2). |
| `SCAN_INTERVAL_SECONDS` | `15` | Seconds between full scans. `main.py --interval N` overrides it. |
| `PRICE_STALE_SECONDS` | `60` | A price older than this keeps a path out of the scan-based alerts. |

**Live** (`main.py --live`)

| Setting | Default | Effect |
|---|---|---|
| `LIVE_CONFIRM_POLLS` | `2` | Chain polls in a row a path must stay profitable before it trades. |
| `LIVE_TRADE_COOLDOWN_SECONDS` | `300` | Pause after each trade (at least 30). Survives restarts. |
| `LIVE_MAX_TRADES_PER_DAY` | `10` | Trade attempts per day before live mode stops for the day. Counted from the database, so restarts do not reset it. |
| `LIVE_MAX_DRAWDOWN_ERG` | `5` | Live mode pauses if the wallet value falls this much. |
| `LIVE_ERG_RESERVE` | `1` | ERG always left in the wallet. |
| `LIVE_MAX_PROFIT_PERCENT` | `10` | A profit above this is treated as bad data: nothing trades, a pinged alert is sent. `arb.py --execute` refuses it too (`--force` overrides). |
| `LIVE_MAX_ORACLE_DEVIATION_PERCENT` | `5` | The oracle further than this from the median of the exchanges' prices (at least two, from the CEX watch) blocks live trading, with a pinged alert. |
| `LIVE_STOP_FILE` | `STOP` | Kill switch: while this file exists nothing trades (relative = in the bot folder). |
| `LEG2_WATCH_TIMEOUT_SECONDS` | `1200` | How long leg 2 (bank redeem) is followed until confirmed. |
| `LEG2_WATCH_INTERVAL_SECONDS` | `20` | How often it is checked. |
| `LEG2_MAX_REBUILDS` | `5` | Times leg 2 is rebuilt on fresh boxes if it drops from the mempool. |

**Discord** (see [Discord Notifications](#discord-notifications))

| Setting | Default | Effect |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | (none) | Webhook for `--notify` / `--live`. Blank = no Discord. |
| `DISCORD_USER_ID` | (none) | Your numeric user id for @mentions. Blank = no pings. |
| `DISCORD_MIN_PROFIT_PERCENT` | `1.0` | Smallest profit % that posts. |
| `DISCORD_MIN_PROFIT_ERG` | `0.5` | Smallest profit in ERG that posts (both minimums must be met). |
| `DISCORD_TIER1_PROFIT_PERCENT` | `2.0` | At or above this the message pings you. |
| `DISCORD_CONFIRM_SECONDS` | `10` | An on-chain opportunity must hold this long before its message opens. |
| `DISCORD_CLOSE_SECONDS` | `10` | Gone this long before its message closes. |
| `DISCORD_EDIT_SECONDS` | `30` | At most one edit of an open message per this. |
| `DISCORD_HEALTH_CHAIN_SECONDS` | `120` | Chain unreadable this long: alert (ping). |
| `DISCORD_HEALTH_VENUE_SECONDS` | `300` | An exchange down this long: alert. |
| `DISCORD_HEALTH_ORACLE_SECONDS` | `600` | Oracle update pending this long: alert. |
| `DISCORD_HEALTH_REPEAT_SECONDS` | `1800` | A failure that continues is re-alerted this often. |
| `DISCORD_HEALTH_LIVE_SECONDS` | `600` | The STOP file holding `--live` this long: alert. |
| `DISCORD_DIGEST_HOUR` | `8` | Local hour of the daily digest; `-1` = off. |
| `DISCORD_CONFIRM_SCANS` | `3` | Scan-based alerts (CEX/USE paths): scans in a row before posting. |
| `DISCORD_COOLDOWN_SECONDS` | `300` | Scan-based alerts: per-path cooldown. |
| `DISCORD_WALLET_COOLDOWN_SECONDS` | `600` | Wallet analysis at most this often. |
| `DISCORD_SUMMARY_INTERVAL_SECONDS` | `1800` | The text summary of all paths. |

**Bank mint gate**

| Setting | Default | Effect |
|---|---|---|
| `MINT_GATE_CONFIRM_POLLS` | `3` | Agreeing polls before a mint open/close alert. |
| `MINT_GATE_MIN_ROOM_ERG` | `MIN_TRADE_SIZE_ERG` | Less mint room than this counts as closed. |
| `MINT_GATE_PING_COOLDOWN_SECONDS` | `3600` | At most one mint-open ping per this. |
| `MINT_GATE_CLOSE_SECONDS` | `600` | Minting shut this long before the message says closed. |

**CEX watch** (see [CEX prices](#cex-prices-watch-only)). Kucoin's and NonKYC's fees are read from
the exchanges and are not settings.

| Setting | Default | Effect |
|---|---|---|
| `CEX_WATCH` | `true` | Read public exchange prices (no keys); never trades. |
| `CEX_WATCH_ALERT_PERCENT` | `3.0` | Gap to the pool that posts a watch message. |
| `CEX_WATCH_COOLDOWN_SECONDS` | `3600` | At most one watch message per this. |
| `GATE_TRADING_FEE` | `0.002` | Fallback until Gate's taker fee is read. |
| `GATE_ERG_WITHDRAW_FEE` | `0.403` | Gate's ERG withdrawal fee (not public; check on Gate). |
| `MEXC_TRADING_FEE` | `0.0008` | Fallback until MEXC's taker fee is read. |
| `MEXC_ERG_WITHDRAW_FEE` | `0.1` | MEXC's ERG withdrawal fee (not public; check on MEXC). |
| `SAFETRADE_ENABLED` | `false` | Try SafeTrade (often blocked by Cloudflare). |
| `SAFETRADE_TRADING_FEE` | `0.002` | SafeTrade taker fee. |
| `SAFETRADE_ERG_WITHDRAW_FEE` | (read from SafeTrade) | Override SafeTrade's withdrawal fee. |
| `CEX_USDT_TRANSFER_FEE` | `1.0` | USDT cost of moving the proceeds back after a cross-exchange round. |

**Housekeeping**

| Setting | Default | Effect |
|---|---|---|
| `SCAN_RESULTS_RETENTION_DAYS` | `14` | Non-profitable scan rows, and price snapshots nothing refers to, older than this are deleted at startup and once a day. |

Command-line flags of `main.py` (`--interval`, `--max-trade-erg`, `--db`, ...) are in the table
under [Modes](#modes).

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
└── archive/                # retired scripts (old execute_*.py, one-off recoveries), reference only
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
python arb.py doctor                                    # is the node ready for the bot? (see below)
python arb.py config                                    # settings in use and mistakes in .env
python arb.py backup                                    # consistent copy of the database (see Backups)
python arb.py resume                                    # clear a live-mode pause (then restart the bot)
```

**`arb.py doctor`** first warns about `.env` mistakes and checks the Discord webhook (a GET that shows
its name and posts nothing). Then it checks everything the bot needs from your node, one line each
(OK / WARN / FAIL with a hint / SKIP): reachable and synced, API key accepted, wallet restored and unlocked, wallet
boxes, the pool/bank/oracle boxes through the extra index, the mempool lookup (the GET form on the
Scala node, the POST form on the Rust node `arkadianet/ergo`), and finally a 1 ERG pool buy -> bank
redeem that the node signs and validates but that is **never broadcast** (`--no-sign` skips it).
It exits with 1 when a node check fails. Run it after switching or upgrading the node.

**`arb.py config`** lists every setting with the value in use and where it comes from (`.env`,
environment or default), secrets masked, and flags keys the bot does not read (with the closest real
name, e.g. `DISCORD_WEBHOOK` -> `DISCORD_WEBHOOK_URL?`), values still holding a `.env.example`
placeholder (ignored) and renamed settings. It needs no node.

**Guardrails on `--execute`.** Every `arb.py ... --execute` checks the live settings first (see
**Live mode**) and refuses while a `--live` bot runs on the same database, since both could spend the
same boxes (`--ignore-lock` overrides it). While it runs it holds the bot's lock, so `--live` cannot
start in the middle. `arb.py arb --execute` also refuses while the STOP file exists or live mode is
paused. `swap`, `redeem` and `send` stay allowed then, because they are how you finish a failed trade
by hand. `arb --force --execute` below `MIN_PROFIT_PERCENT` shows the expected result and only goes
ahead when you type that amount back (e.g. `-0.0644`).

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
| Wallet value drawdown since the start of the day | `LIVE_MAX_DRAWDOWN_ERG` (5) |
| Time since last trade | `LIVE_TRADE_COOLDOWN_SECONDS` (300) |
| Trades today | `LIVE_MAX_TRADES_PER_DAY` (10) |
| Profit not implausibly high | `LIVE_MAX_PROFIT_PERCENT` (10) |
| Oracle close to the exchanges' median price | `LIVE_MAX_ORACLE_DEVIATION_PERCENT` (5) |

**Chain watcher.** Pool, bank and oracle boxes come from your node every `CHAIN_POLL_SECONDS`
(2 s), mempool included: a pending pool or bank transaction is priced right away, and trades chain
onto it instead of conflicting with it. The oracle box is only a data input of the bank
transaction, so it is always read from the confirmed state; while an oracle update is pending,
nothing trades until it confirms (about one block). Each poll re-runs the exact sizing and the live
gate; the tables, SQLite logging and Discord stay on `SCAN_INTERVAL_SECONDS`. If the node cannot be
read, nothing trades until it can. A `CHAIN` line is printed whenever a contract box changes.

Each scan prints `LIVE: not trading - <reasons>` while any check fails. A leg-2 failure, a
TX-guard refusal or an unexpected error pauses live trading and is sent to Discord with the redeem
command to finish. The pause is stored in the database: restarting does not clear it, and a trade
the bot was in the middle of when it stopped (killed, crashed) also pauses the next start. Check
`python arb.py balance`, then clear it with `python arb.py resume` and restart. The cooldown, the
day's trade count and the day's drawdown baseline are also restored from the database.

**Settings checked before trading.** `--live` and every `arb.py ... --execute` refuse to start when a
setting is outside its safe range: `SLIPPAGE_TOLERANCE` 0-0.05, `MIN_PROFIT_PERCENT` >= 0.1,
`MAX_TRADE_SIZE_ERG` up to 1000, `LIVE_ERG_RESERVE` >= 0.01, `LIVE_CONFIRM_POLLS` >= 2,
`LIVE_TRADE_COOLDOWN_SECONDS` >= 30, `LIVE_MAX_DRAWDOWN_ERG` up to `MAX_TRADE_SIZE_ERG`,
`MAX_FEE_BUDGET_ERG` 0.002-2, `EXECUTION_BUFFER` 0-0.05, `LIVE_MAX_PROFIT_PERCENT` above
`MIN_PROFIT_PERCENT` and up to 100, `LIVE_MAX_ORACLE_DEVIATION_PERCENT` 0.5-50. `--max-trade-erg` can only lower the cap.

**Leg 2 price floor.** Leg 2 is built again on fresh boxes right before it is signed, and again if it
drops from the mempool. It must return at least the planned ERG minus `SLIPPAGE_TOLERANCE` (1%);
the TX guard enforces the same floor. Below it, the bot keeps the SigUSD and retries until the price
recovers or the watch times out (`leg2_failed`, with the command to finish by hand), instead of
selling into a moved pool or at a moved oracle.

**What the TX guard accepts.** Foreign inputs must be known contract boxes (the ErgoDEX pool, the
SigmaUSD bank, the Dexy LP), identified by their NFT and recreated by the transaction; someone
else's wallet box is never accepted. `MAX_TRADE_SIZE_ERG` caps the ERG a transaction takes from the
wallet; SigUSD sells and redeems are capped by the amount you ask for.

### First live run

1. **Wallet unlocked.** Live mode signs with your node wallet. Unlock it in the node panel
   (`http://YOUR_NODE_IP:9053/panel`) or with `POST /wallet/unlock` (body
   `{"pass": "<wallet password>"}`, header `api_key`). The dashboard shows `wallet ●` when it is
   unlocked, and live mode will not trade while it is locked.
2. **Check the pieces without spending.** `python arb.py balance` shows what the bot will trade
   with. `python arb.py arb --check` builds both legs at the best size, and your node validates
   them without broadcasting.
3. **Start small and watch.** `python main.py --live --max-trade-erg 5`. It prints the limits in
   force and asks you to type `LIVE`; anything else exits without trading (`--yes` skips the question
   for unattended runs). A trade only happens when
   a path shows **GO** for `LIVE_CONFIRM_POLLS` polls; until then the Live panel lists why it is not
   trading. Every trade, success or failure, is sent to Discord with a ping.
4. **Kill switch.** Create a file named `STOP` in the bot folder, the folder with `main.py`, wherever the bot was started from; the path is logged at startup (`touch STOP`, or
   `New-Item STOP` on Windows) and no new trade starts; delete it to resume. Ctrl+C stops the bot.
5. **If leg 2 fails**, you hold SigUSD and live trading pauses, also across restarts. The Discord
   message and the Live panel give the exact command to finish (`python arb.py redeem --sigusd ...
   --execute`); afterwards `python arb.py resume` and restart the bot.


### Retired execution scripts

The standalone `execute_*.py` scripts and `verify_opportunity.py` now live in `archive/`. Each
had its own copy of the quote, sign and monitor code. Everything they did is in the wallet tool,
on the shared, TX-guarded code in `ergo/`:

| Old script | Now |
|---|---|
| `execute_arb.py` | `python arb.py arb [--path redeem\|mint] [--erg N] --check / --execute` |
| `execute_pool_swap.py`, `execute_swap_sigusd_to_erg*.py`, `execute_swap_erg_to_sigusd_spectrum.py` | `python arb.py swap --sell erg\|sigusd --amount N\|all` (direct pool swap, no Crux fee) |
| `execute_bank_redeem.py` | `python arb.py redeem --sigusd N\|all` |
| `execute_swap_use*.py`, `execute_swap_erg_to_use_lp.py` | none while USE is disabled (its LP was drained); kept in `archive/` for reference |
| `verify_opportunity.py` | the bot's own exact sizing (`python arb.py quote`) |

## Data Tracking

All data is stored in `arbitrage_tracker.db` (SQLite):

- **price_snapshots** - Every scan's prices from all sources
- **scan_results** - All paths analyzed per scan
- **opportunities** - Profitable opportunities with full fee breakdown
- **trades** - Executed trades with timing, actual profit, and tx IDs
- **daily_summary** - Aggregated daily stats
- **opportunity_episodes** - Continuous runs of a profitable path, from the 15 s full scans
- **chain_episodes** - The on-chain opportunities sent to Discord (opened, closed, peak, trade)
- **meta** - Small key/value state, e.g. the date the last daily digest was sent, the live pause

Once a day (and at start) rows older than `SCAN_RESULTS_RETENTION_DAYS` are pruned and the WAL file is
checkpointed, so a bot that runs for months stays small; freed space is reused. Profitable rows,
opportunities, episodes and trades are kept.

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
- **CEX ERG withdrawal fees are large** (NonKYC 3.1 ERG, Kucoin 2.0 ERG) - they make small CEX rounds unprofitable
- **SigmaUSD Bank has restrictions** - SigUSD minting is blocked if the post-mint RR would fall below 400%; SigUSD redeem is always allowed (the 800% cap only applies to SigRSV minting)
- **SigUSD trades off its peg** - so a CEX (USDT) vs pool (SigUSD) price gap is not a trade by itself
- **USE paths are off** - the USE LP was drained (Oct 2026); a token migration is expected (`USE_TOKEN_ID`, `DEXY_USE_LP_NFT`)
- **Crux service fee is ~0.785 ERG flat** - only paid on Crux-routed swaps (USE LP, or SigUSD with `POOL_SWAP_ROUTE=crux`); SigUSD pool swaps go direct and pay only the miner fee
- **Ergo TX fees are EXPLICIT** - must be an output box, not implicit (inputs must equal outputs)
- **Node must be fully synced** for wallet operations to work correctly

---

## Swap legs and paths

[FLOWS.md](FLOWS.md) lists every swap leg and round trip, the command that runs it, what has been
tested on mainnet and its fees.

---

## Status and Roadmap

**Working today**
- On-chain arbitrage between the ErgoDEX SigUSD/ERG pool and the SigmaUSD bank (pool buy -> bank
  redeem, and bank mint -> pool sell while the reserve ratio allows minting), executed by `--live`
  with direct pool swaps (no service fee), exact sizing, a leg-2 price floor and the TX guard
- Chain watcher: pool, bank and oracle read from your node every 2 s, mempool aware (Scala node, and
  the Rust node `arkadianet/ergo` via its mempool lookup)
- Wallet tool `arb.py` (balance, quote, swap, redeem, send, arb), dry run by default, plus `doctor`
  (is the node ready?), `config` (settings in use, mistakes in `.env`) and `backup`
- One-screen dashboard with health strip, Exchanges and History panels and the mint-gate / reserve-ratio
  trend, plus `--plain` and `--json` views
- Discord: one message per opportunity, health alerts, bank mint gate alert, daily digest, wallet analysis
- CEX prices (Kucoin, NonKYC, Gate, MEXC; SafeTrade opt-in), watch-only, with live fees and the
  cross-exchange spread after fees; fetched in the background so they never slow the chain poll
- SQLite tracking of prices, opportunities, episodes and trades

**Next** (plan: issue #90)
- Check the bot against the Rust node once it is synced (`arb.py doctor`, `pytest -m live`)
- Guardrails that must land before any `--live` trade: limits that survive a restart, validated live
  settings, a single-instance lock, a visible arming step, alerts for silent failures (#59-#66, #71)
- Then the first supervised `--live` trade with a small `--max-trade-erg` (once a path shows GO)

**Later** (details in the GitHub issues)
- A venue/leg quote abstraction with a path graph, once more on-chain venues exist (#13; the
  script and wallet-analysis duplication from that issue is already gone)
- MachinaFi as a venue, if it ever gets real liquidity (#12; re-checked 2026-10-03: ~1 ERG fillable)
- CEX execution (order placement, withdrawals): only worth it at several hundred ERG, because of the
  withdrawal fees
