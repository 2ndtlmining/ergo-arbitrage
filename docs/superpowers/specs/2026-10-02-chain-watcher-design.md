# Chain watcher: event-driven on-chain scanning (issues #9, #8 on-chain part)

Date: 2026-10-02. Status: approved in chat, pending spec review.

## Goal

React to pool, bank and oracle changes within about 2 s instead of up to 15+ s. Price
against the same mempool-aware boxes the runner will spend, so scanner and trade
never disagree.

**In scope:** on-chain only (ERG/SigUSD AMM pool, SigmaUSD bank, oracle).

**Out of scope:**
- CEX WebSockets and order books (CEX parked).
- Dexy/USE (ENABLE_USE off by default; unchanged).
- A TUI (#14).

## Why

Measured on the user's node (2026-10-02):

| Source | Latency | Mempool |
|---|---|---|
| Public explorer `/boxes/unspent/byTokenId` (scanner today) | ~3,800 ms | no; confirmed state lags |
| Node `/blockchain/box/unspent/byTokenId` (extraIndex, at tip) | ~50 ms | no |
| Node `/transactions/unconfirmed/outputs/byTokenId` | ~50 ms | yes |

Today the scanner prices from the explorer, while the runner (`ergo/arb_runner.py`)
spends boxes found via the node index. Neither sees a *pending* pool/bank box. If
someone's swap is in the mempool, our leg 1 double-spends the same pool box and one
of the two is rejected.

## Decisions

- **Polling, not events:** the Ergo node has no WebSocket/event API. Polling the
  three box ids every 2 s costs about 6 parallel ~50 ms node calls.
- **Mempool-aware state:** use the pending output holding a contract NFT when one
  exists and is still unspent in the pool, chaining onto it instead of conflicting.
- **Live confirmation:** an opportunity must hold for `LIVE_CONFIRM_POLLS` (2)
  consecutive polls, about 2–4 s. This replaces `LIVE_CONFIRM_SCANS` (3 × 15 s), which
  filtered flickering explorer/CEX data. The runner still re-sizes and re-checks on
  fresh boxes, and the TX guard still applies.
- **Language:** stay with Python. The loop is network-bound and sizing takes
  milliseconds. A Rust rewrite would re-implement the contract math, the TX guard and
  leg-2 recovery for no latency gain. A TUI can use `rich.Live`/Textual (#14).

## Components

### 1. `ergo/chain_state.py` (new), the single source for contract boxes

```python
async def latest_box(ns, nft: str, explorer=None) -> tuple[dict, bool]:
    """(box, pending) for the newest unspent box holding `nft`."""
```

1. Mempool: `GET /transactions/unconfirmed/outputs/byTokenId/{nft}`. Keep each
   candidate only if `GET /utxo/withPool/byId/{id}` is 200, i.e. not already spent by
   another mempool tx. If several remain (should not happen for a singleton NFT),
   take the last one listed. Use it with `pending=True`.
2. Otherwise the confirmed box: `find_box_id(nft, ns, explorer)` (node index, then
   explorer with retries) plus `node_box`. `pending=False`.
3. Raise `RuntimeError` if neither works.

The returned box is node JSON, so the existing builders and parsers accept it
unchanged.

```python
@dataclass(frozen=True)
class ChainSnapshot:
    height: int
    pool: dict
    bank: dict
    oracle: dict
    pending: frozenset[str]   # subset of {"pool", "bank", "oracle"}
    read_ms: float
    @property
    def key(self) -> tuple[str, str, str]: ...   # the three box ids

async def read_snapshot(ns, explorer=None) -> ChainSnapshot
```

`read_snapshot` reads the three boxes and `/info` height in parallel and times it.

### 2. Runner

`run_arb` fetches pool/bank/oracle via `latest_box` instead of `find_box_id` +
`node_box`. Leg-2 rebuilds (`build_and_submit_leg2`) use `latest_box` too, so a
rebuild chains onto a pending bank/oracle/pool update. `arb.py` actions that read
these boxes (`quote`, `swap`, `redeem`, `balance`) use it as well, through the shared
`_boxes` helper.

### 3. Scanner prices from the snapshot

```python
def prices_from_snapshot(snap: ChainSnapshot) -> dict
```

This builds `spectrum_pool` (via `parse_n2t_pool_box`), `spectrum_erg_sigusd` and the
`bank` dict with the same keys as `SigmaUSDBank.get_full_state()`:
`oracle_erg_usd`, `bank_erg_reserve`, `sigusd_circulating`, `reserve_ratio`,
`can_mint_sigusd`, `can_redeem_sigusd`, `state`.

`fetch_all_prices` takes the on-chain part from the latest snapshot and keeps the
CEX/USE fetches as they are. The off-chain "frontend oracle" cross-check is dropped
from the scan path: the bank contract only uses the on-chain oracle box.

### 4. Loop: two cadences

`run()` ticks every `CHAIN_POLL_SECONDS` (default 2):

1. `read_snapshot`. On failure: log once per failure streak, mark chain state
   unavailable, reset live streaks, and skip trading this tick.
2. On success:
   - Build prices.
   - Run `_optimize_sizes` (exact sizing).
   - `_update_live_streak` (one count per poll).
   - If `key` changed since the last tick: print one line with the height, which
     boxes changed or are pending, the read time, and the best size per path.
3. If a full scan is due (`SCAN_INTERVAL_SECONDS` since the last one), run
   `scan_once(prices)`: tables, SQLite, Discord, wallet analysis. This replaces the
   timer loop of today.
4. Otherwise, in live mode, if any path's streak has reached `LIVE_CONFIRM_POLLS`:
   fetch wallet balances, then `_execute_trades`. This uses the same gate as today:
   node health, kill switch, cooldown, daily limit, drawdown.

A trade blocks the loop until it finishes, including the leg-2 watch, as it does
today; cooldown applies afterwards.

`scan_once` keeps working standalone: it reads a snapshot itself when not given
prices, so tests and callers stay simple.

### 5. Staleness

`_price_timestamps["spectrum"/"bank"]` are set from the snapshot time. If the last
good snapshot is older than the existing staleness limit (`PRICE_STALE_SECONDS`, 60), the full scan marks
on-chain paths stale (existing `_is_price_stale` logic) and the live gate blocks
with "chain state stale/unavailable".

## Config

| Name | Default | Notes |
|---|---|---|
| `CHAIN_POLL_SECONDS` | 2 | fast poll |
| `LIVE_CONFIRM_POLLS` | 2 | replaces `LIVE_CONFIRM_SCANS` (removed; README and .env.example updated) |
| `SCAN_INTERVAL_SECONDS` | 15 | unchanged meaning: full scan, display and logging |

## Error handling

- **Node unreachable or any read fails:** no trading; the last good state is shown as
  stale; reads retry next tick.
- **Node index missing:** explorer fallback for box ids only (boxes are still read
  from the node). This is slower but correct.
- **A pending box that disappears** (its tx was dropped): the next poll returns the
  confirmed box, the key changes, and the streak restarts naturally because sizing
  changes. A trade already built on it fails at the node (nothing spent) or is
  rebuilt by the leg-2 watcher.

## Testing

Unit tests with a fake node (aiohttp-shaped responses):

- `latest_box`:
  - prefers a pending output that is unspent in the pool
  - ignores a pending output already spent
  - falls back to index, then explorer
  - raises when nothing is found
- `read_snapshot`: the key, pending set, height and timing.
- `prices_from_snapshot`: matches `get_full_state` keys/values for the same boxes,
  and sizing on it equals sizing on `Market.from_boxes`.
- Loop (scanner with an injected snapshot reader and a fake clock):
  - a change prints one update line, with no repeat while unchanged
  - the streak counts per poll, so a trade fires after 2 polls and not 1
  - the full scan runs on its cadence
  - a failed read blocks trading and resets streaks
- Runner: uses `latest_box` (pending pool box is spent as leg-1 input).

Read-only live checks against the node after each section: snapshot timing, a pending
vs confirmed sanity check, `arb.py quote`/`arb --check`, and a 1-minute scanner run.
