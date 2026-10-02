# Chain Watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The scanner reacts to pool, bank and oracle changes within about 2 s. It reads mempool-aware boxes from the node, the same ones the runner spends.

**Architecture:**
- New `ergo/chain_state.py` finds the newest unspent box per contract NFT (pending mempool output first, then the node index, then the explorer). It also builds a timed `ChainSnapshot` and the scanner's price dict from it.
- The runner and CLI switch to it.
- The scanner loop ticks every `CHAIN_POLL_SECONDS`: fast exact sizing and live gate every tick, the full scan every `SCAN_INTERVAL_SECONDS`.

**Tech Stack:** Python 3.13, asyncio, aiohttp, pytest (`asyncio.run` in tests, no pytest-asyncio), rich.

**Spec:** `docs/superpowers/specs/2026-10-02-chain-watcher-design.md`

## Global Constraints

- On-chain only. Do not change CEX or USE fetching beyond moving them onto the shared HTTP session.
- Node endpoints used:
  - `/transactions/unconfirmed/outputs/byTokenId/{id}`
  - `/utxo/withPool/byId/{id}`
  - `/blockchain/box/unspent/byTokenId/{id}` (via the existing `find_box_id`)
  - `/info`
- Polling request timeout: 5 s per node call (`CHAIN_TIMEOUT`).
- Config:
  - `CHAIN_POLL_SECONDS` default 2
  - `LIVE_CONFIRM_POLLS` default 2, replacing `LIVE_CONFIRM_SCANS` (removed)
  - `SCAN_INTERVAL_SECONDS` stays 15
  - `PRICE_STALE_SECONDS` stays 60
- Boxes are node JSON with serialized hex registers. Read registers with `ergo.sigmausd_tx.register_int`, never `renderedValue`.
- Tests run with `python -m pytest -q`. `conftest.py` pins config defaults, so add new config names there.

## Review Focus

1. **Old node without the unconfirmed-outputs endpoint** (HTTP 400/404): treat it as "no pending box" and use the confirmed box. Test in Task 1.
2. **Two pending outputs for one NFT** (two chained mempool swaps): use the one still unspent in the pool, not one already spent. Test in Task 1.
3. **Node without extraIndex:** box id comes from the explorer and the box is read from the node. Test in Task 1.
4. **Chain read fails after earlier good reads** (node restart, timeout): no trade even though old sizing was profitable; streaks reset. Test in Task 4.
5. **A poll timing out** (`asyncio.TimeoutError`) must not crash the loop or leave trading enabled. Test in Task 4.

---

### Task 1: `ergo/chain_state.py`: `latest_box`, `ChainSnapshot`, `read_snapshot`

**Files:**
- Create: `ergo/chain_state.py`
- Create: `tests/fake_node.py` (test helper)
- Test: `tests/test_chain_state.py`

**Interfaces:**
- Consumes: `ergo.chain.find_box_id(token_id, ns=None, explorer=None, retry_delay=2.0) -> str` and `ergo.chain.node_box(ns, box_id) -> dict` (existing).
- Produces:
  - `CHAIN_TIMEOUT: aiohttp.ClientTimeout`
  - `async latest_box(ns, nft: str, explorer=None) -> tuple[dict, bool]`
  - `@dataclass(frozen=True) ChainSnapshot(height: int, pool: dict, bank: dict, oracle: dict, pending: frozenset[str], read_ms: float)` with property `key -> tuple[str, str, str]` (box ids of pool, bank, oracle)
  - `async read_snapshot(ns, explorer=None) -> ChainSnapshot`

- [ ] **Step 1: Write the fake node helper**

`tests/fake_node.py`:

```python
"""aiohttp-shaped fake for node/explorer GETs: routes map a URL suffix to (status, json)."""


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """`routes`: {url_suffix: (status, body) | Exception}. Unknown URLs return 404."""

    def __init__(self, routes: dict):
        self.routes = dict(routes)
        self.calls: list[str] = []

    def get(self, url, timeout=None, **kw):
        self.calls.append(url)
        for suffix, result in self.routes.items():
            if url.endswith(suffix):
                if isinstance(result, Exception):
                    raise result
                return _Resp(*result)
        return _Resp(404, {"error": 404})
```

- [ ] **Step 2: Write the failing tests**

`tests/test_chain_state.py`:

```python
"""Mempool-aware contract boxes from the node (spec: chain watcher)."""
import asyncio

import pytest

import config
from ergo.chain_state import ChainSnapshot, latest_box, read_snapshot
from tests.fake_node import FakeSession

NFT = config.SPECTRUM_SIGUSD_POOL_NFT
UNCONF = f"/transactions/unconfirmed/outputs/byTokenId/{NFT}"
INDEX = f"/blockchain/box/unspent/byTokenId/{NFT}?offset=0&limit=1"


def box(box_id, value=10**12):
    return {"boxId": box_id, "value": value, "ergoTree": "00", "assets": [], "additionalRegisters": {}}


def run(coro):
    return asyncio.run(coro)


def node(**routes):
    return FakeSession(routes)


class TestLatestBox:
    def test_pending_output_unspent_in_pool_wins(self):
        ns = FakeSession({UNCONF: (200, [box("p1")]),
                          "/utxo/withPool/byId/p1": (200, box("p1")),
                          INDEX: (200, [box("c1")]),
                          "/utxo/withPool/byId/c1": (200, box("c1"))})
        b, pending = run(latest_box(ns, NFT))
        assert b["boxId"] == "p1" and pending is True

    def test_spent_pending_output_is_skipped(self):
        # p1 already spent by a second mempool tx that created p2
        ns = FakeSession({UNCONF: (200, [box("p1"), box("p2")]),
                          "/utxo/withPool/byId/p1": (404, None),
                          "/utxo/withPool/byId/p2": (200, box("p2"))})
        b, pending = run(latest_box(ns, NFT))
        assert b["boxId"] == "p2" and pending

    def test_no_pending_uses_confirmed_index_box(self):
        ns = FakeSession({UNCONF: (200, []), INDEX: (200, [box("c1")]),
                          "/utxo/withPool/byId/c1": (200, box("c1"))})
        b, pending = run(latest_box(ns, NFT))
        assert b["boxId"] == "c1" and pending is False

    def test_old_node_without_unconfirmed_endpoint(self):
        ns = FakeSession({UNCONF: (400, {"error": 400}), INDEX: (200, [box("c1")]),
                          "/utxo/withPool/byId/c1": (200, box("c1"))})
        b, pending = run(latest_box(ns, NFT))
        assert b["boxId"] == "c1" and not pending

    def test_node_without_index_uses_explorer_for_the_id(self):
        ns = FakeSession({UNCONF: (200, []), INDEX: (404, None),
                          "/utxo/withPool/byId/c9": (200, box("c9"))})
        explorer = FakeSession({f"/boxes/unspent/byTokenId/{NFT}?limit=1": (200, {"items": [box("c9")]})})
        b, pending = run(latest_box(ns, NFT, explorer=explorer))
        assert b["boxId"] == "c9" and not pending

    def test_nothing_found_raises(self, monkeypatch):
        import ergo.chain
        monkeypatch.setattr(ergo.chain, "EXPLORER_ATTEMPTS", 1)  # skip the explorer retry sleeps
        ns = FakeSession({UNCONF: (200, []), INDEX: (200, [])})
        explorer = FakeSession({f"/boxes/unspent/byTokenId/{NFT}?limit=1": (200, {"items": []})})
        with pytest.raises(RuntimeError):
            run(latest_box(ns, NFT, explorer=explorer))


def snapshot_node(pending_pool=False):
    routes = {"/info": (200, {"fullHeight": 1_885_700})}
    for name, nft in (("pool", config.SPECTRUM_SIGUSD_POOL_NFT), ("bank", config.SIGMAUSD_BANK_NFT),
                      ("oracle", config.SIGMAUSD_ORACLE_NFT)):
        pend = pending_pool and name == "pool"
        routes[f"/transactions/unconfirmed/outputs/byTokenId/{nft}"] = (200, [box(f"{name}-p")] if pend else [])
        routes[f"/utxo/withPool/byId/{name}-p"] = (200, box(f"{name}-p"))
        routes[f"/blockchain/box/unspent/byTokenId/{nft}?offset=0&limit=1"] = (200, [box(f"{name}-c")])
        routes[f"/utxo/withPool/byId/{name}-c"] = (200, box(f"{name}-c"))
    return FakeSession(routes)


class TestSnapshot:
    def test_reads_all_three_and_height(self):
        snap = run(read_snapshot(snapshot_node()))
        assert snap.key == ("pool-c", "bank-c", "oracle-c")
        assert snap.height == 1_885_700 and snap.pending == frozenset() and snap.read_ms >= 0

    def test_pending_pool_changes_the_key(self):
        a = run(read_snapshot(snapshot_node()))
        b = run(read_snapshot(snapshot_node(pending_pool=True)))
        assert b.key != a.key and b.key[0] == "pool-p" and b.pending == frozenset({"pool"})

    def test_failure_propagates(self):
        ns = snapshot_node()
        ns.routes["/info"] = asyncio.TimeoutError()
        with pytest.raises(asyncio.TimeoutError):
            run(read_snapshot(ns))
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_chain_state.py -q`
Expected: collection ERROR, `ModuleNotFoundError: No module named 'ergo.chain_state'`.

- [ ] **Step 4: Implement `ergo/chain_state.py`**

```python
"""Newest unspent pool, bank and oracle boxes as the node sees them, mempool included.

The scanner and the runner both read contract boxes through latest_box(), so the
state the scanner prices is the state the runner's transactions spend. A pending
mempool output holding the NFT wins over the confirmed box: spending the confirmed
box would conflict with that pending transaction, chaining onto it does not.
"""
import asyncio
import time
from dataclasses import dataclass

import aiohttp

import config
from ergo.chain import find_box_id, node_box

CHAIN_TIMEOUT = aiohttp.ClientTimeout(total=5)
CONTRACTS = (("pool", config.SPECTRUM_SIGUSD_POOL_NFT), ("bank", config.SIGMAUSD_BANK_NFT),
             ("oracle", config.SIGMAUSD_ORACLE_NFT))


async def _get(ns, path: str):
    """(status, json or None) for a node GET."""
    async with ns.get(f"{config.ERGO_NODE_URL}{path}", timeout=CHAIN_TIMEOUT) as r:
        return r.status, (await r.json() if r.status == 200 else None)


async def latest_box(ns, nft: str, explorer=None) -> tuple[dict, bool]:
    """(box, pending) for the newest unspent box holding `nft`.

    1. Mempool outputs holding `nft` that are still unspent in the pool (the last one wins).
    2. Otherwise the confirmed box: node index, then the explorer, read from the node.
    Raises RuntimeError if neither finds it.
    """
    status, outputs = await _get(ns, f"/transactions/unconfirmed/outputs/byTokenId/{nft}")
    if status == 200 and outputs:
        live = []
        for out in outputs:
            st, b = await _get(ns, f"/utxo/withPool/byId/{out['boxId']}")
            if st == 200 and b:
                live.append(b)
        if live:
            return live[-1], True
    box_id = await find_box_id(nft, ns, explorer)
    return await node_box(ns, box_id), False


@dataclass(frozen=True)
class ChainSnapshot:
    height: int
    pool: dict
    bank: dict
    oracle: dict
    pending: frozenset
    read_ms: float

    @property
    def key(self) -> tuple[str, str, str]:
        return self.pool["boxId"], self.bank["boxId"], self.oracle["boxId"]


async def _height(ns) -> int:
    status, info = await _get(ns, "/info")
    if status != 200 or not info:
        raise RuntimeError(f"node /info returned HTTP {status}")
    return int(info.get("fullHeight") or 0)


async def read_snapshot(ns, explorer=None) -> ChainSnapshot:
    """Pool, bank and oracle boxes plus height, read in parallel."""
    start = time.perf_counter()
    *found, height = await asyncio.gather(*(latest_box(ns, nft, explorer) for _, nft in CONTRACTS), _height(ns))
    boxes = {name: b for (name, _), (b, _) in zip(CONTRACTS, found)}
    pending = frozenset(name for (name, _), (_, p) in zip(CONTRACTS, found) if p)
    return ChainSnapshot(height, boxes["pool"], boxes["bank"], boxes["oracle"], pending,
                         (time.perf_counter() - start) * 1000)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_chain_state.py -q`
Expected: 9 passed.

- [ ] **Step 6: Live read-only check against the node**

Run:
```bash
python -c "import asyncio,aiohttp,config; from dotenv import load_dotenv; load_dotenv(); import importlib; importlib.reload(config); from ergo.chain_state import read_snapshot
async def m():
    async with aiohttp.ClientSession() as ns:
        for _ in range(3):
            s = await read_snapshot(ns); print(s.height, s.key, sorted(s.pending), f'{s.read_ms:.0f}ms')
asyncio.run(m())"
```
Expected: three lines at the current height with real box ids, reads under about 500 ms, and no exceptions.

- [ ] **Step 7: Full suite and commit**

Run: `python -m pytest -q`, expect all passing. Then:
```bash
git add ergo/chain_state.py tests/fake_node.py tests/test_chain_state.py
git commit -m "chain_state: mempool-aware contract boxes and timed snapshots from the node"
```

---

### Task 2: `prices_from_snapshot`

**Files:**
- Modify: `ergo/chain_state.py` (append)
- Test: `tests/test_chain_state.py` (append)

**Interfaces:**
- Consumes: `ChainSnapshot` (Task 1), `exchanges.spectrum.parse_n2t_pool_box(box, token_id, decimals_y, symbol_y="SigUSD", fee_num=None, exchange=...) -> PoolState`, `exchanges.sigmausd.BankState`, `can_mint_sigusd`, `ergo.sigmausd_tx.register_int`.
- Produces: `prices_from_snapshot(snap: ChainSnapshot) -> dict` with keys `spectrum_pool` (PoolState), `spectrum_erg_sigusd` (float), and `bank` (dict with keys `oracle_erg_usd, bank_erg_reserve, sigusd_circulating, reserve_ratio, can_mint_sigusd, can_redeem_sigusd, state`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_chain_state.py`)

```python
from arbitrage.sizing import Market, best_size
from ergo.chain_state import prices_from_snapshot
from tests.test_bank_redeem_tx import BANK_BOX, ORACLE_BOX
from tests.test_chain_arb import pool_box


def real_snapshot(pending=frozenset()):
    return ChainSnapshot(1, pool_box(0.35, 2_000 * 10**9), BANK_BOX, ORACLE_BOX, pending, 1.0)


class TestPricesFromSnapshot:
    def test_bank_dict_has_the_scanner_keys(self):
        bank = prices_from_snapshot(real_snapshot())["bank"]
        assert set(bank) == {"oracle_erg_usd", "bank_erg_reserve", "sigusd_circulating", "reserve_ratio",
                             "can_mint_sigusd", "can_redeem_sigusd", "state"}
        assert bank["state"].oracle_r4 == 3_100_000_000
        assert bank["oracle_erg_usd"] == pytest.approx(1e9 / 3_100_000_000)
        assert bank["can_mint_sigusd"] is False  # BANK_BOX RR ~322%
        assert bank["can_redeem_sigusd"] is True

    def test_pool_reads_fee_from_hex_register(self):
        p = prices_from_snapshot(real_snapshot())
        assert p["spectrum_pool"].fee_num == 995
        assert p["spectrum_erg_sigusd"] == pytest.approx(p["spectrum_pool"].price_x_in_y)

    def test_sizing_on_prices_equals_sizing_on_boxes(self):
        snap = real_snapshot()
        p = prices_from_snapshot(snap)
        via_prices = best_size("redeem", Market.from_pool_state(p["spectrum_pool"], p["bank"]["state"]), 100)
        via_boxes = best_size("redeem", Market.from_boxes(snap.pool, snap.bank, snap.oracle), 100)
        assert via_prices.size_nanoerg == via_boxes.size_nanoerg
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_chain_state.py -q -k PricesFromSnapshot`
Expected: ImportError, `cannot import name 'prices_from_snapshot'`.

- [ ] **Step 3: Implement** (append to `ergo/chain_state.py`, and add the imports at the top)

```python
from ergo.sigmausd_tx import register_int
from exchanges.sigmausd import BankState, can_mint_sigusd
from exchanges.spectrum import parse_n2t_pool_box
```

```python
def prices_from_snapshot(snap: ChainSnapshot) -> dict:
    """The scanner's on-chain price entries, from exactly the boxes in `snap`."""
    pool = parse_n2t_pool_box(snap.pool, config.SIGUSD_TOKEN_ID, config.SIGUSD_DECIMALS,
                              fee_num=register_int(snap.pool, "R4"), exchange="ErgoDEX pool (node)")
    state = BankState(int(snap.bank["value"]), register_int(snap.bank, "R4"), register_int(snap.oracle, "R4"))
    return {
        "spectrum_pool": pool,
        "spectrum_erg_sigusd": pool.price_x_in_y,
        "bank": {
            "oracle_erg_usd": state.oracle_usd_per_erg,
            "bank_erg_reserve": state.bank_erg_nano / 1e9,
            "sigusd_circulating": state.sigusd_circ_cents / 100,
            "reserve_ratio": state.reserve_ratio,
            "can_mint_sigusd": can_mint_sigusd(state, 1),
            "can_redeem_sigusd": True,
            "state": state,
        },
    }
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_chain_state.py -q`
Expected: 12 passed.

- [ ] **Step 5: Live check**

Extend the Step 6 command of Task 1 to print `prices_from_snapshot(s)["spectrum_erg_sigusd"]` and `["bank"]["reserve_ratio"]`.
Expected: the pool price is about 0.3 SigUSD/ERG and the RR matches `python arb.py balance` (330% at writing).

- [ ] **Step 6: Commit**

```bash
git add ergo/chain_state.py tests/test_chain_state.py
git commit -m "chain_state: scanner price entries from a snapshot"
```

---

### Task 3: Runner and CLI use `latest_box`

**Files:**
- Modify: `ergo/arb_runner.py` (`_fetch_boxes`, around line 98)
- Modify: `ergo/actions.py` (`_boxes`, around line 76)
- Test: `tests/test_runner_sizing.py` (append)

**Interfaces:**
- Consumes: `latest_box(ns, nft, explorer=None) -> tuple[dict, bool]` (Task 1).
- Produces: `arb_runner._fetch_boxes(ns, nfts) -> list[dict]` and `actions._boxes(ns, *nfts) -> list[dict]`, with the same signatures as before, now mempool-aware and parallel.

- [ ] **Step 1: Write the failing test** (append to `tests/test_runner_sizing.py`)

```python
def test_runner_spends_the_pending_pool_box():
    from tests.fake_node import FakeSession
    nft = config.SPECTRUM_SIGUSD_POOL_NFT
    pending = dict(POOL, boxId="pool-pending")
    ns = FakeSession({f"/transactions/unconfirmed/outputs/byTokenId/{nft}": (200, [pending]),
                      "/utxo/withPool/byId/pool-pending": (200, pending)})
    (got,) = asyncio.run(runner._fetch_boxes(ns, (nft,)))
    assert got["boxId"] == "pool-pending"


def test_cli_reads_the_pending_bank_box():
    from ergo import actions
    from tests.fake_node import FakeSession
    nft = config.SIGMAUSD_BANK_NFT
    pending = dict(BANK_BOX, boxId="bank-pending")
    ns = FakeSession({f"/transactions/unconfirmed/outputs/byTokenId/{nft}": (200, [pending]),
                      "/utxo/withPool/byId/bank-pending": (200, pending)})
    (got,) = asyncio.run(actions._boxes(ns, nft))
    assert got["boxId"] == "bank-pending"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_runner_sizing.py -q -k pending`
Expected: 2 failed. The old code calls `/blockchain/box/...`, which the fake answers with 404, then the explorer, which raises `RuntimeError` or a network error.

- [ ] **Step 3: Implement**

`ergo/arb_runner.py`: replace the import of `find_box_id, node_box` usage in `_fetch_boxes`:

```python
from ergo.chain_state import latest_box
```

```python
async def _fetch_boxes(ns, nfts) -> list[dict]:
    """Newest unspent box per NFT, pending mempool outputs included (ergo/chain_state.py)."""
    return [b for b, _ in await asyncio.gather(*(latest_box(ns, nft) for nft in nfts))]
```

Remove `find_box_id` and `node_box` from the `ergo.chain` import in `arb_runner.py` if they are now unused. `wait_for_box` and `wallet_context` stay.

`ergo/actions.py`:

```python
from ergo.chain_state import latest_box
```

```python
async def _boxes(ns, *nfts) -> list[dict]:
    return [b for b, _ in await asyncio.gather(*(latest_box(ns, nft) for nft in nfts))]
```

Add `import asyncio` to `actions.py` if it is missing. Remove `find_box_id` and `node_box` from its `ergo.chain` import if they are unused.

- [ ] **Step 4: Run to verify**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Live check (read-only and `--check`)**

Run: `python arb.py quote`, then `python arb.py arb --force --check`.
Expected:
- `quote` prints both best-size lines.
- `arb` prints `Leg 1 node check: VALID` and `Not broadcast (check mode).`

- [ ] **Step 6: Commit**

```bash
git add ergo/arb_runner.py ergo/actions.py tests/test_runner_sizing.py
git commit -m "Runner and CLI read contract boxes through chain_state (pending mempool boxes included)"
```

---

### Task 4: Scanner prices from the snapshot, with a chain-unavailable gate

**Files:**
- Modify: `arbitrage/scanner.py`:
  - `__init__` (around lines 67–90)
  - `connect_all`/`disconnect_all` (around lines 104–151)
  - `_fetch_use_*` (around lines 153–174)
  - `fetch_all_prices` (around lines 176–251)
  - `_update_live_streak` (around line 1060)
  - `_global_blockers` (around line 1097)
- Modify: `tests/conftest.py`
- Test: `tests/test_chain_scanner.py` (new)

**Interfaces:**
- Consumes: `read_snapshot`, `prices_from_snapshot`, `ChainSnapshot` (Tasks 1–2).
- Produces, on `ArbitrageScanner`:
  - `self._http: Optional[aiohttp.ClientSession]`: the shared plain session for Crux and the explorer fallback. It replaces `_crux_session`.
  - `self._snapshot: Optional[ChainSnapshot]`: the last good snapshot.
  - `self._chain_error: Optional[str]`: None while chain reads succeed.
  - `async _read_chain(self) -> Optional[ChainSnapshot]`
  - `fetch_all_prices()`: on-chain entries come from `_read_chain()`, falling back to the last good snapshot for display.

- [ ] **Step 1: Write the failing tests**

`tests/test_chain_scanner.py`:

```python
"""Scanner on node snapshots: prices, chain-unavailable gate (spec: chain watcher)."""
import asyncio

import pytest

import arbitrage.scanner as scanner_module
import config
from arbitrage.scanner import ArbitrageScanner
from ergo.chain_state import ChainSnapshot
from tests.test_bank_redeem_tx import BANK_BOX, ORACLE_BOX
from tests.test_chain_arb import pool_box

KEY = "Spectrum buy->Bank redeem"
HEALTHY = {"reachable": True, "synced": True, "unlocked": True, "height": 1, "headers": 1, "ok_to_trade": True}
WALLET = {"erg": 50.0, "sigusd": 0.0, "use": 0.0}


def snap(pool_id="pool-a"):
    """Profitable for pool buy -> bank redeem (pool sells SigUSD cheap)."""
    return ChainSnapshot(1_885_700, dict(pool_box(0.35, 2_000 * 10**9), boxId=pool_id), BANK_BOX, ORACLE_BOX,
                         frozenset(), 12.0)


class Reader:
    """Stand-in for read_snapshot: returns queued snapshots or raises queued exceptions."""

    def __init__(self, *items):
        self.items = list(items)

    async def __call__(self, ns, explorer=None):
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 1000.0)
    monkeypatch.setattr(config, "LIVE_STOP_FILE", str(tmp_path / "STOP"))
    s = ArbitrageScanner(mode="live", db_path=str(tmp_path / "t.db"))
    calls = []

    async def fake_run(ns, path, erg_in, **kw):
        calls.append(path)
        from ergo.arb_runner import ArbResult
        return ArbResult("executed", erg_in=5 * 10**9, sigusd_cents=100, profit_nanoerg=10**8,
                         profit_percent=2.0, tx1="t1", tx2="t2", path=path)

    async def healthy():
        return dict(HEALTHY)

    async def wallet():
        return dict(WALLET)

    monkeypatch.setattr(scanner_module, "run_arb", fake_run)
    monkeypatch.setattr(s.ergo_node, "get_health", healthy)
    monkeypatch.setattr(s, "_fetch_wallet_balances", wallet)
    s.calls = calls
    yield s
    s.tracker.close()


def test_prices_come_from_the_node_snapshot(scanner, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    prices = asyncio.run(scanner.fetch_all_prices())
    assert prices["spectrum_pool"].fee_num == 995
    assert prices["bank"]["state"].oracle_r4 == 3_100_000_000
    assert scanner._snapshot.key[0] == "pool-a" and scanner._chain_error is None


def test_failed_read_keeps_last_state_for_display_but_blocks_trading(scanner, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), RuntimeError("node down")))
    asyncio.run(scanner.fetch_all_prices())
    prices = asyncio.run(scanner.fetch_all_prices())
    assert prices["spectrum_pool"] is not None           # last good state still shown
    assert scanner._chain_error == "node down"
    blockers = asyncio.run(scanner._global_blockers(WALLET, prices))
    assert any("chain state unavailable" in b for b in blockers)


def test_failed_read_resets_live_streaks(scanner, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), asyncio.TimeoutError()))
    prices = asyncio.run(scanner.fetch_all_prices())
    scanner._find_opportunities(prices)
    scanner._update_live_streak()
    assert scanner._live_streak[KEY] == 1
    asyncio.run(scanner.fetch_all_prices())
    scanner._update_live_streak()
    assert scanner._live_streak[KEY] == 0
```

In `tests/conftest.py` `TEST_DEFAULTS`, replace `"LIVE_CONFIRM_SCANS": 3,` with:

```python
    "LIVE_CONFIRM_POLLS": 2,
    "CHAIN_POLL_SECONDS": 2,
```

and in `config.py` (Live section, replacing the `LIVE_CONFIRM_SCANS` line):

```python
LIVE_CONFIRM_POLLS = int(os.getenv("LIVE_CONFIRM_POLLS", "2"))            # profitable on N chain polls in a row
```

and next to `SCAN_INTERVAL_SECONDS`:

```python
CHAIN_POLL_SECONDS = float(os.getenv("CHAIN_POLL_SECONDS", "2"))  # node poll for pool/bank/oracle changes
```

Update the uses of `LIVE_CONFIRM_SCANS` in `arbitrage/scanner.py` (`_path_blockers`) and `tests/test_live.py` (two `range(...)` loops) to `LIVE_CONFIRM_POLLS`. The blocker text becomes `f"profitable {streak}/{config.LIVE_CONFIRM_POLLS} polls in a row"`. Update the test in `tests/test_live.py` that asserts the reason `"scans"` to assert `"polls"`.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_chain_scanner.py -q`
Expected: failures. `scanner_module` has no `read_snapshot`, and there is no `_snapshot`/`_chain_error`.

- [ ] **Step 3: Implement in `arbitrage/scanner.py`**

Imports:

```python
from ergo.chain_state import ChainSnapshot, prices_from_snapshot, read_snapshot
```

`__init__`: replace `self._crux_session: Optional[aiohttp.ClientSession] = None` with:

```python
        self._http: Optional[aiohttp.ClientSession] = None  # shared: Crux API + explorer fallback
        self._snapshot: Optional[ChainSnapshot] = None      # last good node snapshot
        self._chain_error: Optional[str] = None             # set while chain reads fail
```

Rename every `self._crux_session` to `self._http`; it is used in `connect_all`, `disconnect_all`, `_fetch_use_mint_status` and `_fetch_use_lp`.

`connect_all`: the scanner no longer reads the pool or bank through `SpectrumDEX`/`SigmaUSDBank`, so drop `self.spectrum.connect()` and `self.sigmausd.connect()` from `venues`. Drop the matching disconnects in `disconnect_all`. Keep the attributes, because other code paths and the live tests import these classes.

Add:

```python
    async def _read_chain(self) -> Optional[ChainSnapshot]:
        """Read pool/bank/oracle from the node. On failure keep the last good snapshot
        for display, record the error (which blocks trading) and log once per outage."""
        try:
            snap = await read_snapshot(self.ergo_node.session, self._http)
        except (RuntimeError, ValueError, KeyError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            message = str(e) or e.__class__.__name__
            if self._chain_error is None:
                logger.warning(f"Chain state unavailable: {message}")
            self._chain_error = message
            return None
        if self._chain_error is not None:
            logger.info("Chain state readable again")
        self._chain_error = None
        self._snapshot = snap
        self._price_timestamps["spectrum"] = self._price_timestamps["bank"] = time.time()
        return snap
```

`fetch_all_prices`: remove `self.spectrum.get_pool_state()` and `self.sigmausd.get_full_state()` from the gather (and their result handling and timestamp lines), and add the chain read to the same gather:

```python
        snap, nonkyc_price, kucoin_price, use_lp, use_mint = await asyncio.gather(
            self._read_chain(),
            self.nonkyc.fetch_erg_usdt_price() if self.enable_cex else _none(),
            self.kucoin.fetch_erg_usdt_price() if self.enable_cex else _none(),
            self._fetch_use_lp() if self.enable_use else _none(),
            self._fetch_use_mint_status() if self.enable_use else _none(),
            return_exceptions=True,
        )
        if isinstance(snap, BaseException):  # _read_chain catches the expected ones
            logger.error(f"Chain read failed: {snap}")
            snap = None
        onchain = prices_from_snapshot(self._snapshot) if self._snapshot else \
            {"spectrum_pool": None, "spectrum_erg_sigusd": None, "bank": {}}
        results.update(onchain)
```

Keep the existing NonKYC/Kucoin/USE handling and the `results[...]` assignments for those keys, and delete the ones for `spectrum_pool`, `spectrum_erg_sigusd` and `bank`.

`_update_live_streak`:

```python
            ok = bool(choice and choice.ok and self._chain_error is None)
```

`_global_blockers`: first check, before the STOP file:

```python
        if self._chain_error is not None:
            blockers.append(f"chain state unavailable ({self._chain_error})")
```

- [ ] **Step 4: Run to verify**

Run: `python -m pytest -q`
Expected: all pass, including `tests/test_live.py` after the `LIVE_CONFIRM_POLLS` rename.

- [ ] **Step 5: Live check**

Run the scanner for about 40 s: `timeout 40 python main.py 2>&1 | grep -E "BEST SIZE|Chain|Error|Traceback"`.
Expected: BEST SIZE lines and no Traceback. The scan should be visibly faster than before, with no 3–4 s explorer wait.

- [ ] **Step 6: Commit**

```bash
git add arbitrage/scanner.py config.py tests/conftest.py tests/test_live.py tests/test_chain_scanner.py
git commit -m "Scanner prices pool/bank/oracle from node snapshots; chain read failures block trading"
```

---

### Task 5: Two-cadence loop (`poll_once`), change line, docs

**Files:**
- Modify: `arbitrage/scanner.py` (`run()` around line 1769, new `poll_once`, new `_note_change`)
- Modify: `README.md`, `.env.example`
- Test: `tests/test_chain_scanner.py` (append)

**Interfaces:**
- Consumes: `_read_chain`, `prices_from_snapshot`, `_optimize_sizes`, `_update_live_streak`, `_execute_trades`, `scan_once` (existing/Task 4).
- Produces: `async ArbitrageScanner.poll_once(self, now: float) -> None` (one tick) and `ArbitrageScanner._last_full_scan: float` (init `float("-inf")`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_chain_scanner.py`)

```python
def test_first_tick_is_a_full_scan_then_fast_polls(scanner, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    full = []
    real = scanner.scan_once

    async def counting():
        full.append(1)
        await real()

    monkeypatch.setattr(scanner, "scan_once", counting)
    for now in (0, 2, 4, 6, 15, 17):
        asyncio.run(scanner.poll_once(now))
    assert len(full) == 2  # t=0 and t=15


def test_trades_after_two_polls_not_one(scanner, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap()))
    asyncio.run(scanner.poll_once(0))
    assert scanner.calls == []
    asyncio.run(scanner.poll_once(2))
    assert scanner.calls == ["redeem"]


def test_change_line_printed_once_per_state(scanner, monkeypatch):
    from logging_config import console
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap("a"), snap("a"), snap("b")))
    monkeypatch.setattr(config, "LIVE_CONFIRM_POLLS", 99)  # no trading in this test
    asyncio.run(scanner.poll_once(0))
    with console.capture() as cap:
        asyncio.run(scanner.poll_once(2))   # same state: silent
    assert "CHAIN" not in cap.get()
    with console.capture() as cap:
        asyncio.run(scanner.poll_once(4))   # pool box changed
    out = cap.get()
    assert "CHAIN" in out and "pool" in out and "1885700" in out.replace(",", "")


def test_timeout_mid_run_blocks_trading_and_loop_survives(scanner, monkeypatch):
    monkeypatch.setattr(scanner_module, "read_snapshot", Reader(snap(), asyncio.TimeoutError()))
    asyncio.run(scanner.poll_once(0))          # streak 1
    asyncio.run(scanner.poll_once(2))          # read fails: no trade, streak reset
    asyncio.run(scanner.poll_once(4))          # still failing
    assert scanner.calls == []
    assert scanner._live_streak.get(KEY, 0) == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_chain_scanner.py -q`
Expected: the 4 new tests fail with `AttributeError: ... 'poll_once'`.

- [ ] **Step 3: Implement**

`__init__`:

```python
        self._last_full_scan = float("-inf")
        self._last_key: Optional[tuple] = None
```

New methods (place them after `scan_once`):

```python
    def _note_change(self):
        """One console line when the pool, bank or oracle box (or its pending state) changes."""
        snap = self._snapshot
        if snap is None or self._chain_error is not None or snap.key == self._last_key:
            return
        names = ("pool", "bank", "oracle")
        changed = [n for n, a, b in zip(names, snap.key, self._last_key or (None,) * 3) if a != b]
        pending = f" pending: {', '.join(sorted(snap.pending))}" if snap.pending else ""
        best = " | ".join(f"{LIVE_PATHS[k]}: {c.summary()}" for k, c in self.last_sizing.items())
        console.print(f"[dim]{datetime.now().strftime('%H:%M:%S')} CHAIN h{snap.height} "
                      f"changed: {', '.join(changed)}{pending} ({snap.read_ms:.0f} ms) | {best}[/dim]")
        self._last_key = snap.key

    async def poll_once(self, now: float):
        """One tick: full scan when due, otherwise a fast node read, exact sizing and the live gate."""
        if now - self._last_full_scan >= config.SCAN_INTERVAL_SECONDS:
            self._last_full_scan = now
            await self.scan_once()
            self._note_change()
            return
        snap = await self._read_chain()
        if snap is None:
            self._update_live_streak()  # resets streaks while the chain is unreadable
            return
        prices = prices_from_snapshot(snap)
        self.last_optima = self._optimize_sizes(prices)
        self._update_live_streak()
        self._note_change()
        if self.trading_enabled and any(self._live_streak.get(k, 0) >= config.LIVE_CONFIRM_POLLS
                                        for k in LIVE_PATHS):
            await self._execute_trades(await self._fetch_wallet_balances(), prices)
```

In `run()`, replace the body of the `while` loop:

```python
            while not self._stop.is_set():
                try:
                    await self.poll_once(loop.time())
                except Exception as e:
                    logger.error(f"Poll error: {e}", exc_info=True)
                next_tick = max(next_tick + config.CHAIN_POLL_SECONDS, loop.time())
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=next_tick - loop.time())
                except asyncio.TimeoutError:
                    pass
```

In the startup panel, change `f"Scan interval: {config.SCAN_INTERVAL_SECONDS}s\n"` to:

```python
            f"Chain poll: {config.CHAIN_POLL_SECONDS:g}s (node, mempool-aware) | full scan: {config.SCAN_INTERVAL_SECONDS}s\n"
```

`_update_live_streak` must treat a chain error as not-ok even when `last_sizing` is stale. Task 4 already added `self._chain_error is None`, so streaks reset; verify.

Docs:

`.env.example`: replace `LIVE_CONFIRM_SCANS=3` with:

```
# Polls (CHAIN_POLL_SECONDS apart) an opportunity must hold before --live trades it
LIVE_CONFIRM_POLLS=2
```

and add after `SCAN_INTERVAL_SECONDS=15`:

```
# How often the node is polled for pool/bank/oracle changes (incl. mempool); full scans stay on SCAN_INTERVAL_SECONDS
CHAIN_POLL_SECONDS=2
```

`README.md`: in the live checks table replace `| ...for N scans in a row | \`LIVE_CONFIRM_SCANS\` (3) |` with `| ...for N chain polls in a row (~2 s apart) | \`LIVE_CONFIRM_POLLS\` (2) |`. Add a short paragraph under "Live mode" explaining the watcher, in the README's existing tone:

```markdown
**Chain watcher.** Pool, bank and oracle boxes come from your node every `CHAIN_POLL_SECONDS`
(2 s), mempool included: a pending swap or oracle update is priced right away, and trades chain
onto it instead of conflicting with it. Each poll re-runs the exact sizing and the live gate; the
tables, SQLite logging and Discord stay on `SCAN_INTERVAL_SECONDS`. If the node cannot be read,
nothing trades until it can.
```

- [ ] **Step 4: Run to verify**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Live check**

Run `timeout 75 python main.py 2>&1 | grep -E "CHAIN|BEST SIZE|Scan #|Error|Traceback"`.
Expected:
- one CHAIN line at start
- Scan # headers about 15 s apart
- CHAIN lines only when a block or mempool tx touches the pool, bank or oracle
- no Traceback

- [ ] **Step 6: Commit**

```bash
git add arbitrage/scanner.py README.md .env.example tests/test_chain_scanner.py
git commit -m "Scanner loop: 2 s mempool-aware chain polls with exact sizing and live gate; full scan every 15 s"
```

---

### Task 6: Whole-branch verification and PR

- [ ] **Step 1:** `python -m pytest -q`, then `python -m pytest -m live -q`. Both must be all-pass.
- [ ] **Step 2:** Run `python arb.py quote`, `python arb.py arb --check --force` and `python arb.py balance`. All must work, with `Leg 1 node check: VALID`.
- [ ] **Step 3:** Run the scanner for 3 minutes (spanning at least one block). Confirm a CHAIN line appears when the height changes and a pool, bank or oracle box moved, and that there are no errors.
- [ ] **Step 4:** Check PR state on GitHub (the user merges fast). Push `feature/chain-watcher` and open a PR that closes #9 and references #8 (on-chain parts done; CEX parts parked).
