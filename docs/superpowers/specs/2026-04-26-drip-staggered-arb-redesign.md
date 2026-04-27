# Drip — Staggered Arb Manager (Redesign)

**Date:** 2026-04-26
**Status:** Spec revised against kalshi-mcp. POC scope locked. BLIP threshold formula still open.
**Supersedes:** `docs/specs/2026-03-19-drip-staggered-arb-design.md` (v1, deleted in commit `87e3752`)
**v2 implementation reference:** commit `bdc6f7e` (3,172 LOC + 1,608 LOC tests; deleted 2026-04-23, recoverable)

---

## POC scope (locked 2026-04-26)

**Goal:** Validate that BLIP is mechanically possible and that per-side ETA correctly identifies which side is racing — *not* to demonstrate balance improvement at scale. Defensive measure first; optimization later.

### Terminology

| Term | Meaning | POC default |
|------|---------|-------------|
| `DRIP_SIZE` | Contracts per individual bid | `1` |
| `MAX_DRIPS` | Max drips resting per side at once | `1` |
| `BLIP_DELTA_MIN` | BLIP fires when `ETA_behind - ETA_ahead > X` minutes | `5.0` |
| Effective per-side cap | `DRIP_SIZE × MAX_DRIPS` contracts | `1` contract per side |

> **Why renamed:** "unit size" already means something in Talos's non-DRIP strategy. `DRIP_SIZE` and `MAX_DRIPS` are unambiguous.

### Replenishment rule (Reading A — matched-pair)

Track `pairs_filled = min(filled_A, filled_B) // DRIP_SIZE`. When `pairs_filled` increments, replenish one drip on each side. The fast side accumulates "owed" replenishment until the slow side catches up. Both sides always have `MAX_DRIPS` resting *unless* the fast side has filled but the slow side hasn't — in which case the fast side's depth is intentionally below `MAX_DRIPS` (depth drains as a natural throttle, complementing BLIP).

### Market scope

- POC: **single yes/no market** (one `market_ticker`, bids on each side of the same orderbook). Confirmed `event_ticker`-keyed state mirrors exit-only's pattern.
- Out of scope for POC: NO-only sports markets (e.g. team-specific game-winner tickers in pairs). Logic should translate; intentionally deferred.

### Mutual exclusion with normal Talos flow

Enabling DRIP on a ticker **replaces** Talos's normal trading on that ticker. Normal opportunity proposal, bid adjustment, and fill rebalancing are skipped for DRIP-enabled events. No side-by-side operation in POC.

### Prerequisite: CPM/ETA fix in `src/talos/cpm.py`

The current `CPMTracker` aggregates all trades on a ticker into a single stream. Per-side ETA — needed for BLIP — requires the granularity already documented in [docs/KALSHI_CPM_AND_ETA.md](../../KALSHI_CPM_AND_ETA.md):

```
flow_key = (ticker, outcome ∈ {yes, no}, book_side ∈ {BID, ASK}, price_cents)
```

Decomposition rule (from doc spec):
- Trade with `taker_side == outcome` → ASK hit at that price (fill against a resting ASK)
- Trade with `taker_side != outcome` → BID hit at that price (fill against a resting BID — i.e., one of OUR bids)

The fix lands as a **separate PR before the DRIP POC PR**. UI consumers (existing tiles) get updated to consume the new granularity; aggregate sums must remain unchanged. Tests must lock the equivalence.

### UI surface

| Element | Choice | Notes |
|---------|--------|-------|
| Toggle key | `d` | Freed by moving Remove Game to `delete` |
| Trigger mode | Manual-only (no auto-trigger) | Exit-only-style game-time auto-trigger out of scope |
| Input | Popup modal on first `d`-press | Three fields: `DRIP_SIZE` (int, default `1`), `MAX_DRIPS` (int, default `1`), `BLIP_DELTA_MIN` (float minutes, default `5.0`) |
| Toggle off | `d` again on a DRIP-enabled row | No popup; cancels resting drips, returns ticker to normal |
| Pattern mirror | Exit-only (`_exit_only_events: set[str]` on `TradingEngine`) | New `_drip_events: set[str]` + `is_drip(event_ticker)` + `toggle_drip(event_ticker, drip_size, max_drips)` + `_enforce_drip(event_ticker)` |
| Status column | `DRIP` (active), `DRIP↑A` (BLIP-throttling A), `DRIP↑B` (BLIP-throttling B), `DRIPPING` (cancellation in progress when toggled off) | Final glyph TBD; modeled on `EXIT` / `EXIT -10 B` / `EXITING` |
| Hotkey rebind | `delete` → Remove Game (was `d`) | One-line change in [src/talos/ui/app.py:68](../../src/talos/ui/app.py:68) |

### Cancel-then-place ordering for BLIP

Confirmed: cancel the front bid first, then re-place at the back. The brief gap where the behind side could fill is a non-issue — if BLIP is firing, the behind side is by construction not close to filling. If the behind side does fill in the gap, the BLIP threshold was set too aggressively.

### BLIP trigger threshold (locked: delta in minutes)

```
BLIP fires when (ETA_behind - ETA_ahead) > BLIP_DELTA_MIN
```

Operator-set per-ticker via the modal. Default `5.0` minutes.

Edge cases:
- **`ETA_behind` is `None` / `∞`** (no recent trades on the slow side) — treat as effectively infinite delta → BLIP fires. The behind side has zero observed flow; the ahead side is racing relatively.
- **`ETA_ahead` is `None`** — should not happen if the ahead side has any fill activity; if it does, no BLIP (no signal).
- **Both ETAs near zero** (frantic trading near event start) — delta is naturally small even when one side is meaningfully faster. Operator may want to lower `BLIP_DELTA_MIN` mid-session, OR we accept that BLIP becomes a no-op when both sides are about to fill. POC: accept the no-op.
- **Per-side ETA fluctuation between polls** — small absolute deltas in slow markets (e.g. 25 vs 27 min) shouldn't fire. Default `5.0` provides natural smoothing for typical event windows.

Open follow-up after POC observation: does delta-in-minutes hold up in practice, or do we need to revisit ratio/hybrid? Capture during POC runs.

### POC pass/fail

Mechanism-level: BLIP fires when triggered, observable via `queue_position_fp` change before/after. No balance-improvement claim. No A/B comparison to non-DRIP runs.

---

## What changed since v1

| Area | v1 said | Reality (kalshi-mcp + project history) |
|------|---------|-----------------------------------------|
| Fee math import | `MAKER_FEE_RATE` constant from `talos.fees` | Migrated to `fee_adjusted_cost_bps` in commit `cc305df`; bps/fp100 migration shipped in PR #1 (`98aeabb`) |
| Maker fee universality | Implicit assumption maker fees apply | They don't — depends on the series's `fee_type`. `quadratic` = makers pay $0; `quadratic_with_maker_fees` = both sides pay. Read from `series.get_series` |
| Fee formula precision | Single `fee_adjusted_cost(price_a) + fee_adjusted_cost(price_b) < 100` check | Per-fill rounding-up-to-cent dominates 1-contract fills — the formula systematically *under-predicts* fees on tiny fills (empirically up to 80× on 0.01-contract fills near 0¢/100¢) |
| Order placement | `count=20` etc. | Use `count_fp` (fixed-point string) on fractional-enabled markets. Add `post_only=true`, `time_in_force="GTC"` for resting bids |
| `/portfolio/orders` | Treated as full order history for crash recovery | Only returns *active/resting* orders since 2026-02-19. Archived orders at `/historical/orders` |
| `/portfolio/positions` | Treated as full position view | Only *unsettled* since 2025-12-05. Settled at `/portfolio/settlements` |
| `/portfolio/fills` | "Fresh enough for fill counts" | Lags real execution by 2–3+ min empirically. WS `fill` channel is the fresh source |
| Cancel propagation | DELETE response treated as "canceled" | Async — DELETE returns zeroed fields immediately, but a follow-up GET can still show `status='resting'` for 1–2 poll cycles |
| Queue position field | Unspecified ("Q-front: 3") | Real endpoint: `portfolio.get_queue_position` returns `queue_position_fp` (fixed-point STRING, not int). `"0.00"` = front of queue, NOT missing |
| Queue priority on amend vs decrease | Not addressed | `amend_order` with count change FORFEITS queue priority. `decrease_order` PRESERVES it. Cancel + new place = back of queue (BLIP semantics) |
| Orderbook shape | Generic | REST: `orderbook_fp.{yes_dollars,no_dollars}` (no `_fp` suffix on side fields). WS snapshot: `yes_dollars_fp`/`no_dollars_fp`. WS delta: `price_dollars` + `delta_fp` + `side`. Three different shapes — normalize at adapter layer |
| WS channels | "orderbook + fills channels for both tickers" | `websocket.fill` is account-wide (one subscription covers BOTH tickers' fills). `websocket.orderbook_delta` is per-market. The v2 implementation also subscribed to a `user_orders` channel — that channel is **NOT** documented in the MCP and needs verification before relying on it |
| Source-of-truth ledger flow | Not addressed | After commit `5c45274` (CLE-TOR fix), WS fills MUST write the ledger via `record_fill_from_ws` (trade-id deduped) with `sync_from_fills` periodic backstop. Cache-prune logic is now blind on same-ticker pairs |
| Wind-down trigger | "Game start / operator key" | Don't terminate on `determined` — Kalshi has a `settlement_timer_seconds` dispute window. Terminal state is `finalized` (or appearance in `/portfolio/settlements`) |
| BLIP primitive | Not in v1 | Operator-specified addition — formal definition deferred to a follow-up brainstorming pass |

---

## Problem (unchanged from v1)

Talos places arb bids as single large orders (~60 contracts per side). Fills are uncontrollable — one side can fill 18/20 while the other sits at 3/20, creating large imbalanced exposure. The rebalance logic reacts after the fact, but by then the damage (adverse selection, one-sided fills near game start) is already done.

## Solution (refined)

A standalone Textual TUI ("Drip") that manages a single arb event by drip-feeding small bids with fill-reactive balance control. Two primitives:

- **DRIP** — incrementally place 1-contract NO bids on each side, alternating, until each side reaches `max_resting`. Replenish on fills.
- **BLIP** — when one side is filling faster than the other, move the leading side's frontmost resting bid to the back of the queue (cancel + re-place at back). This throttles flow on the fast side without reducing depth, in contrast to v1's "let it drain" defensive branch.

> **BLIP semantics deferred.** v1's defensive branch *cancelled* the front bid on the fast side without replacing it, letting the queue drain. BLIP keeps depth constant by re-placing at the back. Open questions for the operator's redesign pass: (1) Does BLIP coexist with v1's drain branch, or replace it? (2) What's the trigger threshold (delta > 1, sustained delta over N seconds, queue-position-based)? (3) On a price jump, is BLIP a no-op (the existing "jump rotation" already moves to new price queue)? Resolve these before implementation.

## Design Decisions (revised)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Balance mode | Strict 1:1 (delta never exceeds 1) | Start simple; graduate to proportional later |
| Target sizing | Manual — operator sets per-event target | Matches current Talos workflow |
| Per-queue cap | `max_resting` resting orders per side (default 20) | Same worst case as current system; staggering makes blowout unlikely |
| Rebalance trigger | Fill-reactive (on each WS `fill` event) | Deterministic, easy to reason about |
| Imbalance handling | DRIP replenish + BLIP rotation (TBD) + v1-style cancel-on-overflow | Three layers: maintain depth; throttle fast side; emergency cancel |
| Jump handling | Cancel front bid, place at back of new price queue | Same as BLIP mechanically — preserves stagger depth, no amend-burst |
| Initial deployment | Manual kick-off, automatic stagger (alternating A/B) | Human-in-the-loop event selection; system handles pacing |
| Order placement defaults | `time_in_force="GTC"`, `post_only=true`, `count_fp="1"`, `self_trade_prevention="cancel_resting"` | Resting maker orders only; reject if would cross; STP as a belt-and-braces — DRIP places opposite-side bids but the complement relation generally prevents self-cross |
| Relationship to Talos | Separate standalone program; no shared in-process state | Zero risk to Talos. Imports `talos.auth`, `talos.rest_client`, `talos.fees` (now `fee_adjusted_cost_bps`), `talos.config` |
| UI | Minimal Textual TUI | Reuse Catppuccin theme from `src/talos/ui/theme.py` |

## Architecture

### Project Structure

```
drip/
├── __main__.py          # Entry: python -m drip EVENT TICKER_A TICKER_B PRICE_A PRICE_B [--demo]
├── config.py            # DripConfig: event ticker, prices, max_resting, stagger_delay
├── controller.py        # DripController: pure state machine (no I/O)
├── runtime_state.py     # Desired vs acknowledged state separation (v2 pattern, retain)
├── side_state.py        # DripSide: per-side order tracking, fill counts
├── ws_runtime.py        # WS orchestration with auto-reconnect backoff
├── ui/
│   ├── app.py           # DripApp: Textual shell, timers, WS wiring
│   ├── theme.py         # Reuse Catppuccin Mocha from Talos
│   └── widgets.py       # Status table, balance indicator, action log
```

### Reused from Talos (import, don't copy)

```python
from talos.auth import KalshiAuth
from talos.config import KalshiConfig
from talos.rest_client import KalshiRESTClient
from talos.models.order import Order, Fill
from talos.models.market import Market, Event
from talos.fees import fee_adjusted_cost_bps   # MIGRATED — was fee_adjusted_cost in v1
from talos.position_ledger import record_fill_from_ws, sync_from_fills  # CLE-TOR fix path
```

### Key Architectural Principles

- **Pure state machine** — `DripController` receives fills and book updates, returns action objects (`PlaceOrder`, `CancelOrder`, `NoOp`). No async, no I/O. Same pattern as Talos's `PositionLedger`.
- **Desired vs acknowledged state** (v2 retention) — controller mutations only commit on WS confirmation, NOT on optimistic REST success. A network blip during cancel-and-replace must not produce a "ghost" reduction in our model that doesn't match Kalshi.
- **One DripController per event** — architecture supports multiple, but v1 is single-event.
- **Kalshi is the single source of truth** — Cardinal rule (Talos Principle 7/15). Before any money-touching action, re-fetch from Kalshi. If fresh data is unavailable, do not act.

## State Machine

### Per-Side State

```
resting_orders: list[OrderInfo]   # ordered by queue position (front first)
filled_count: Decimal              # total fills received (count_fp aware — fractional safe)
target_price_dollars: Decimal      # current NO price as fixed-point dollars (e.g. Decimal("0.470"))
deploying: bool                    # still in initial stagger phase
```

### Core Decision Loop (on every WS fill)

```
ON FILL(side, order_id, count_fp):
    1. Record the fill via record_fill_from_ws(trade_id) — trade-id deduped
    2. Remove order from resting list (or decrement remaining_count_fp if partial)
    3. Compute delta = abs(filled_A - filled_B)

    IF delta == 0 (balanced):
        → Both sides: replenish up to max_resting (add 1 bid to back of queue)

    IF delta == 1 (this fill just created imbalance):
        → Ahead side: do NOT replenish (let queue drain naturally)
        → Behind side: replenish up to max_resting

    IF delta > 1 (imbalance growing — defensive):
        → Ahead side: BLIP front bid (TBD — see deferred semantics) OR
                      cancel front (v1 behavior, drains depth)
        → Behind side: replenish up to max_resting
```

### Jump Handling (on orderbook_delta indicating new best price)

```
ON JUMP(side, new_price_dollars):
    → Update target_price for that side
    → Cancel frontmost resting bid at old price
    → Place new 1-contract bid at new price (back of queue)
    → Leave remaining bids alone — they rotate naturally via fills or future jumps
```

Note: jump detection requires maintaining a local orderbook from `websocket.orderbook_delta`. Track `delta_fp` (signed) at each `price_dollars` level; remove level when count hits zero.

### Initial Deployment

```
DEPLOY:
    → Place 1 bid on side A, wait stagger_delay (e.g. 5s)
    → Place 1 bid on side B, wait stagger_delay
    → Alternate A, B, A, B... until both sides reach max_resting
    → Set deploying = False, hand off to fill-reactive loop
```

Alternating ensures both sides build up together — never more than 1 order ahead during deployment.

## Kalshi API Integration

### Order placement (`portfolio.place_order`)

```python
client.portfolio.place_order(
    ticker=market_ticker,           # e.g. "KXNHLGAME-26MAR19WPGBOS-WPG"
    action="buy",
    side="no",                       # NO-side bids only (DRIP convention)
    count_fp="1",                    # fixed-point STRING
    no_price_dollars="0.470",        # fixed-point STRING; OR yes_price_dollars
    time_in_force="GTC",
    post_only=True,                  # CRITICAL: reject if would cross
    self_trade_prevention="cancel_resting",  # belt-and-braces
    client_order_id=str(uuid.uuid4()),       # for idempotent retry + reconciliation
)
```

### Order cancel (`portfolio.cancel_order`) — async caveat

`DELETE /portfolio/orders/{order_id}` returns immediately with zeroed fields. **A follow-up GET can still show `status='resting'` for 1–2 poll cycles.** Do not treat the DELETE response as proof of finality. Reconcile cancel acks against WS confirmation (or against the next REST poll cycle), and use `client_order_id` to detect double-cancels.

### Cancel + replace ("BLIP / send to back") — there is no API shortcut

| Operation | Queue priority | Use case |
|-----------|----------------|----------|
| `amend_order` (price change only) | Forfeit at new price level | Jump handling — already moving to a new queue |
| `amend_order` (count change) | Forfeit | Generally avoid — go through cancel + new |
| `decrease_order` | **Preserve** | Reducing exposure on a winning side without losing priority |
| `cancel_order` + `place_order` | Forfeit (back of new queue) | **BLIP** — explicitly want to send to back |

**Race condition:** cancel and place are two separate writes. Two ordering choices, both with tradeoffs:
- **Place-then-cancel** — depth is briefly +1 on the fast side; a fill arriving in the gap leaves us +1 deep
- **Cancel-then-place** — depth is briefly -1; a fill arriving on the slow side in the gap creates a delta-2 state

Recommend **place-then-cancel** with `post_only=true` so the new bid joins the back of the queue and the redundant front-bid cancel doesn't matter much if it lands late.

### Queue position monitoring (`portfolio.get_queue_position` / `portfolio.get_queue_positions`)

- Single: `GET /portfolio/orders/{order_id}/queue_position` → `queue_position_fp` (fixed-point STRING, zero-indexed)
- Batch: `GET /portfolio/orders/queue_positions` (requires a filter param — Kalshi returns 400 with no params)
- `"0.00"` = at the front; not missing data
- Optional input to BLIP trigger logic (e.g. trigger when one side's front order has `queue_position_fp == "0"` and the other side's front is deep)

### WebSocket subscriptions

| Channel | Scope | Use |
|---------|-------|-----|
| `websocket.fill` | Account-wide (one subscription covers ALL tickers) | Primary fill source — write to ledger via `record_fill_from_ws` |
| `websocket.orderbook_delta` | Per-market | Subscribe to BOTH `ticker_a` and `ticker_b`; maintain local book for jump detection |
| `websocket.market_lifecycle` | Per-market | Detect transitions to `closed`/`determined`/`finalized` for wind-down |
| ~`user_orders`~ | (used by deleted v2 impl) | **NOT documented in kalshi-mcp.** Verify against Kalshi docs before subscribing. If absent, fall back to REST poll of `/portfolio/orders` for ack confirmation |

WS shape gotchas (already handled in v2 code, retain):
- `orderbook_snapshot.msg.yes_dollars_fp` (with `_fp` suffix); `orderbook_delta.msg.price_dollars` + `delta_fp` (signed) + `side`
- Deltas are NOT replayable on disconnect — resubscribe and start from a fresh snapshot
- Use `ts_ms` (preferred) over `ts` (deprecated)

### REST sync as backup (every 30s)

```python
# 1. Resting orders for the event's two markets
orders_a = await client.portfolio.get_orders(ticker=ticker_a, status="resting")
orders_b = await client.portfolio.get_orders(ticker=ticker_b, status="resting")
# Reconcile against controller's resting_orders list — Kalshi wins on disagreement

# 2. Fill counts (NOTE: 2-3 min lag empirically — WS is fresher)
# Use sync_from_fills as a periodic backstop, NOT a primary fill source
await sync_from_fills(ledger, since=last_reconcile_ts)

# 3. Queue positions for resting orders (optional — for BLIP trigger logic)
queue = await client.portfolio.get_queue_positions(ticker=ticker_a)
```

`/portfolio/orders` returns active only since 2026-02-19. For long-running sessions where orders may have aged into the historical cutoff, also check `/historical/orders`.

### Profitability gate (revised P18)

```python
# Pre-trade estimate (lower bound; under-predicts on 1-contract fills)
gate_ok = (
    fee_adjusted_cost_bps(price_a_dollars) +
    fee_adjusted_cost_bps(price_b_dollars)
) < 100_00   # 100¢ in bps (per bps/fp100 migration)
```

Three layers of fee truth (in increasing authority):
1. **PDF formula** (what `fee_adjusted_cost_bps` implements) — good for *a priori* gates and ladder-wide checks
2. **Per-fill rounding pipeline** — each fill's trade fee is ceiled up to $0.0001, balance-change floored to $0.01. On 1-contract fills, the rounding fee can dominate the trade fee. The formula UNDER-predicts. Per-order rebate accumulator issues a $0.01 rebate when overpayment exceeds $0.01, but each new bid is a new `order_id` so the accumulator does NOT carry between bids
3. **`fee_cost` from `/portfolio/fills`** (or `maker_fees_dollars`/`taker_fees_dollars` on `/portfolio/orders`) — the ONLY authoritative post-trade value. Use for ledger and P&L; never recompute

Maker-fee scope: not all markets charge them. Read `series.get_series().fee_type`:
- `quadratic` → makers pay $0 on this series
- `quadratic_with_maker_fees` → both sides pay (rate 0.0175)
- `flat` → per-trade flat fee
- `fee_multiplier` may further scale per-series

If maker fees apply on the configured event, the gate must include them. Sanity check the configured event's series before deploying.

## Safety & Edge Cases

### Mega-Fill Protection

WS fills can arrive in bursts. If 8 fills arrive on side A in one event-loop tick:
1. Process sequentially through the state machine. Delta climbs to 8.
2. Controller emits cancel/BLIP actions. Serialize these through a single action queue (v2 retention).
3. Hard cap: never more than `max_resting` orders per side, enforced at the placement layer (defense-in-depth against a controller bug).

### Fractional fills

WS `fill_msg.count_fp` is a fixed-point STRING. Parse with `decimal.Decimal`, never `float`. Markets with `fractional_trading_enabled=true` may produce sub-1.0 fills. The DRIP `count_fp="1"` design assumes integer fills, but the controller must still tolerate fractional fills correctly (delta arithmetic in `Decimal`).

### Game Start / Wind-Down

Operator presses a key to trigger wind-down OR `websocket.market_lifecycle` reports market closing.
- Wind-down = stop deploying new bids; let existing bids fill or expire.
- If one side fills during wind-down and creates imbalance, cancel the ahead side's resting bids.
- **Do not treat `determined` as terminal** — there's a `settlement_timer_seconds` dispute window during which the result can change. Wait for `finalized` before final P&L claims, or for the position to appear in `/portfolio/settlements`.

### Process Isolation from Talos

Drip and Talos must NOT manage the same event simultaneously. No technical enforcement initially — operator discipline. The TUI shows a clear warning. Future: shared lock file, or use Kalshi `order_group_id` to tag orders with their owner.

### Crash Recovery

On startup, query Kalshi for state:
1. `/portfolio/orders?ticker={ticker_a}&status=resting` and same for `ticker_b` — reconstruct `DripSide.resting_orders`
2. `/portfolio/positions` for unsettled position counts on each market — sanity check
3. `/portfolio/fills` for fill history (note 2–3 min lag; WS will catch up live fills shortly after subscription)
4. Subscribe to `websocket.fill` + both markets' `websocket.orderbook_delta` BEFORE any further action
5. Resume the loop without re-deploying. Drip is stateless on disk — Kalshi is the source of truth.

Filter caveat: orders/fills are keyed on `market_ticker`, not `event_ticker`. To get all activity on the configured event, enumerate `event.markets` and filter by their tickers.

### Rate Limiting

`/account/limits` returns `read_limit` and `write_limit` (note the URL: `/account/limits`, not `/api_keys/limits`). Each 1-contract DRIP bid generates 1 write at place time and ~1 write at cancel/BLIP time, so a 20-cap × 2-side system at steady state can generate `2 × 20` writes per round-trip burst.

Mitigations:
- Use `portfolio.batch_place_orders` for initial deployment (one HTTP call for many orders; per-order success/failure is independent)
- Use `portfolio.batch_cancel_orders` for wind-down
- Respect `Retry-After` on 429 responses
- Pace fill-reactive replenish — when a burst hits, queue up actions and drain at the write-limit cadence

## TUI Layout (unchanged from v1)

```
┌─────────────────────────────────────────────────┐
│  DRIP — KXNHLGAME-26MAR19WPGBOS                 │
├────────────┬────────────┬───────────────────────┤
│  Side A    │  Side B    │  Balance              │
│  WPG       │  BOS       │  Δ = 0 ✓              │
│  Price: 47¢│  Price: 53¢│                       │
│  Filled: 12│  Filled: 12│  Matched: 12          │
│  Resting: 8│  Resting: 8│  Exposure: $0.00      │
│  Q-front: 0│  Q-front:150│                      │
├────────────┴────────────┴───────────────────────┤
│  Actions                                        │
│  02:15  Fill A #12 — balanced, replenish both   │
│  02:14  Fill B #12 — balanced, replenish both   │
│  02:13  Fill A #12 — ahead +1, hold A           │
│  02:12  BLIP A front — q=0 → back at 47¢        │
│  02:11  Cancel A front — jump 47→48¢, rotate    │
└─────────────────────────────────────────────────┘
```

`Q-front: 0` means at the front of the queue (`queue_position_fp == "0.00"`). NOT missing data.

## Success Criteria (unchanged from v1)

Measured against events run on the current Talos system:
1. **P&L improvement** (primary) — pilot event's actual P&L is better because fewer one-sided fills eat profit
2. **Balance ratio** — pilot event stays more balanced (lower average delta) than typical Talos events
3. **Fill completion rate** — both sides reach target more reliably; fewer events stuck at 18/3 when game starts
4. **Throughput** — queue never goes cold; always working toward the next fill on both sides

### Failure Signals
- Rate limiting kicks in frequently enough to disrupt the rotation loop
- Mega-fills consistently blow through the stagger, producing the same imbalance as the current system
- Per-fill rounding fees on tiny bids erode profit faster than imbalance reduction recovers
- Overhead of managing 40 individual orders exceeds the balance improvement benefit

## Explicit Non-Goals (v1)

- No multi-event support — one event at a time
- No automatic event selection — operator picks event and prices
- No queue velocity prediction — fill-reactive only
- No proportional balance mode — strict 1:1 only
- No dynamic max_resting — fixed at config value
- No Talos integration — no shared state, no IPC
- No persistence — no SQLite, no JSONL logs
- No price discovery — operator provides both NO prices

## Open Questions Before Implementation

1. **BLIP semantics** — coexist with v1 drain branch, or replace? Trigger threshold? Place-then-cancel vs cancel-then-place ordering?
2. **`user_orders` WS channel** — verify it exists against Kalshi docs (kalshi-mcp has no entry). If absent, design fallback for order-ack confirmation.
3. **Same-ticker vs different-ticker pairs** — DRIP examples use opposite-side bids on different market tickers (NO on each of two complementary markets). Does the design also support same-ticker yes+no pairs? If so, the CLE-TOR same-ticker ledger fix path applies directly.
4. **Maker-fee enrollment** — does the operator's account have a non-standard maker rebate (mentioned as possible in MCP gotchas)? Affects pre-trade gate accuracy.
5. **Fee rounding on 1-contract bids** — is the systematic under-prediction acceptable, or do we need a buffer added to the profitability gate?

## Future Enhancements (post-pilot)

- **Queue velocity monitoring** — proactive throttling based on CPM and queue depth
- **Proportional balance mode** — configurable delta tolerance
- **Dynamic max_resting** — adjust per-queue cap based on volume and queue size
- **Multi-event support** — manage multiple events simultaneously
- **Talos integration** — show drip events in Talos table, shared state
- **Automatic deployment** — scanner-driven event selection with auto-deploy
- **Data collection** — SQLite/JSONL logging for strategy analysis

---

## MCP validation log

Validated 2026-04-26 against `kalshi-mcp` (project's curated wisdom layer + Kalshi primary docs). Sources consulted:
- `portfolio.place_order` (full describe — confirms `count_fp`, `post_only`, `time_in_force`, `self_trade_prevention`, `client_order_id`)
- `portfolio.amend_order` (parameter naming gotcha + queue-priority warning)
- `portfolio.cancel_order` (async propagation 1–2 cycles)
- `portfolio.decrease_order` (preserves queue priority)
- `portfolio.get_orders` (active-only since 2026-02-19)
- `portfolio.get_queue_position` / `portfolio.get_queue_positions` (`queue_position_fp` string)
- `portfolio.batch_place_orders` / `portfolio.batch_cancel_orders` (independent per-order success)
- `portfolio.get_fills` (2–3 min lag empirical)
- `portfolio.get_positions` (unsettled-only since 2025-12-05)
- `websocket.fill`, `websocket.orderbook_delta` (channel shapes; `user_orders` NOT in MCP — flagged)
- `api_keys.get_limits` (read_limit, write_limit at `/account/limits`)
- Tips: fee formulas, fee_rounding pipeline, `fee_cost` authority, REST-vs-WS field-name drift, `determined` vs `finalized`, complement relation, fixed-point Decimal parsing
