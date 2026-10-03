# Bank mint gate: alert when SigUSD minting reopens (issue #39, lean)

Date: 2026-10-03. Status: implemented, "lean" scope (PR #45). Design record; the README is authoritative.

## Goal

Tell the user, once and without false alarms, when the SigmaUSD bank mint reopens or closes again.
Bank mint -> pool sell is the direction with an edge right now, and only the reserve ratio (RR)
keeps it shut. While it is shut, show how far ERG has to move to open it.

## Scope

**In**
- Exact forecast: the ERG price at which minting opens, and how much ERG can be minted while it is open
- A watcher that turns bank readings into confirmed open/closed transitions
- Discord: one message per opening, edited when it closes; ping with a cooldown
- Dashboard Bank line and daily digest line

**Out** (the lean choice; the rest of #39 can follow later)
- RR history table, `--rr-watch` / `--rr-once` flags, "approaching 400%" warnings, 800% events,
  box-change events
- Any trading: `--live` already trades the mint path by itself once it is allowed. This feature
  contains no signing or submission code.

## Definitions

- `state`: `BankState` (bank ERG, SigUSD circulation, oracle R4), already read every
  `CHAIN_POLL_SECONDS` by the chain watcher and exposed as `state.prices["bank"]["state"]`.
- **RR** = `bank_erg * 100 / (circulating_cents * rate)`. With circulation and bank ERG fixed it is
  proportional to the oracle ERG/USD price.
- **Room**: the largest ERG amount whose mint keeps the post-mint RR >= 400%, using the
  contract-exact `can_mint_sigusd` and `mint_cost_nanoerg`.
- **Open**: room >= `MINT_GATE_MIN_ROOM_ERG` (default `MIN_TRADE_SIZE_ERG`, 1 ERG). An RR of 400.1%
  that allows minting only a few cents is not worth an alert, so it counts as closed.

## Forecast (`exchanges/sigmausd.py`, pure)

```python
def mint_open_price(state: BankState) -> Optional[float]
    # ERG/USD oracle price at which RR reaches 400%: oracle_usd_per_erg * 400 / RR.
    # None when RR is infinite (no SigUSD circulating) or the oracle price is 0.
def mint_room_nanoerg(state: BankState) -> int
    # Max nanoERG mint cost with can_mint_sigusd still True; 0 when blocked.
    # Binary search (existing _max_true) over cents in [1, bank_erg_nano // rate + 1].
```

The search relies on `can_mint_sigusd(state, c)` being monotone decreasing in `c`, which holds for
RR > 102% (a mint adds ERG at ~102% collateral, so it can only lower a higher RR). Below that,
minting is blocked anyway.

## Watcher (`notifications/mint_gate.py`)

```python
@dataclass
class MintGateEvent:
    kind: str                  # "opened" | "closed"
    reserve_ratio: float
    room_erg: float
    open_price: Optional[float]   # ERG price needed (for "closed")
    oracle_price: float
    opened_at: Optional[float]    # for "closed": when it opened, to show how long
    ping: bool

class MintGateWatcher:
    def __init__(self, confirm_polls: int, min_room_erg: float, ping_cooldown_s: float)
    def update(self, now: float, bank: Optional[dict]) -> Optional[MintGateEvent]
    is_open: bool              # confirmed state; starts False
    message_id: Optional[str]  # the Discord message of the current opening
```

- Each reading is classified as open or closed by its room (see Definitions). A missing `bank`,
  a missing `state` in it, or any error while computing room counts as **no reading**: nothing
  changes and the confirm counter resets.
- The confirmed state flips only after `confirm_polls` consecutive readings disagree with it
  (default `MINT_GATE_CONFIRM_POLLS` = 3, about 6 s). A single odd reading never alerts.
- The confirmed state starts closed, so a bot started while minting is open sends one "opened".
- `ping` is True on "opened" only if no ping was sent in the last `ping_cooldown_s`
  (`MINT_GATE_PING_COOLDOWN_SECONDS`, default 3600). "closed" never pings. The message is always
  posted; only the ping is throttled, so an RR hovering around 400% cannot ping every block.
- The state lives in memory. After a restart, an open gate is reported again (one message, ping
  subject to a fresh cooldown). That is acceptable and stated in the README.

## Discord and dashboard (`arbitrage/scanner.py`, `notifications/embeds.py`)

In `_discord_tick` (already called every poll, never awaits, errors caught and logged):

- `opened`: `discord.post(mint_gate_embed(event), content=ping if event.ping, on_id=store message_id)`
  and a dashboard event `"good"`: "Bank mint OPEN: RR 403%, room ~85 ERG".
- `closed`: `discord.edit(lambda: watcher.message_id, mint_gate_embed(event))` and an `"info"`
  event. If the opening message was never posted (Discord down), the edit is dropped by the
  existing queue rule; nothing else is affected.

The watcher runs only in `--notify` and `--live`, like the other Discord features.

`mint_gate_embed(event)`:
- opened: green, title "Bank mint OPEN · RR 403%", fields: room (ERG), oracle price,
  a line "Bank mint -> pool sell is possible now; --live trades it when profitable".
- closed: grey, title "Bank mint closed · open for 12m 30s", fields: RR, ERG price needed to reopen.

Dashboard Bank line (all modes, from the same `state`, computed in `_refresh_state` so the view
stays a pure renderer):
- closed: `Bank    RR 322%  redeem ✓  mint ✗ needs ERG $0.392 (+21.7%)`
- open: `Bank    RR 403%  redeem ✓  mint ✓ room ~85 ERG`

Daily digest: one field "Bank mint" with the same text as the dashboard, from the latest bank state
(`build_digest(..., bank=...)`); omitted when no bank state is known.

## Config

| Name | Default |
|---|---|
| `MINT_GATE_CONFIRM_POLLS` | 3 |
| `MINT_GATE_MIN_ROOM_ERG` | `MIN_TRADE_SIZE_ERG` (1) |
| `MINT_GATE_PING_COOLDOWN_SECONDS` | 3600 |

## Robustness

| Case | Behaviour |
|---|---|
| No SigUSD circulating (RR infinite) | `mint_open_price` is None; room is computed normally (minting allowed) |
| Oracle R4 / rate is 0 | `can_mint_sigusd` is False -> room 0; open price None; the text shows "—" |
| Bank read fails or is stale | no reading: no transition, counter resets |
| RR hovers around 400% | needs 3 agreeing polls per flip; the ping is throttled to once per hour |
| Opening too small to trade | counts as closed (room < min room) |
| Discord down | the queue drops a failed post; the later close edit is dropped too; no error reaches the scanner |
| Exception in the watcher | caught by `_discord_tick`'s handler; logged; polling continues |

## Docs

README "Discord messages" section: one bullet for the mint alert and its three settings.
`.env.example`: the three settings.

## Testing

- **Forecast**:
  - `mint_open_price` matches a hand computation from a fixed `BankState`
  - room is 0 when blocked and > 0 above 400%
  - the room's mint keeps RR >= 400%, and one more cent would not
  - infinite RR and a zero rate
- **Watcher**:
  - opens only after N agreeing polls; a single odd reading is ignored; a missing reading resets the counter
  - closes after N polls
  - starts closed (an open gate at startup alerts once)
  - small room counts as closed
  - ping cooldown: two openings within an hour, only the first pings
  - "closed" carries `opened_at`
- **Embeds**: opened/closed colours and text.
- **Dashboard and digest**: Bank line text for open and closed; digest field present or absent.
- **Scanner** (fake notifier, as in `tests/test_scanner_discord.py`): a bank state with room for N polls posts one opened message with a ping; going back below 400% edits that same message to closed; monitor mode posts nothing.
