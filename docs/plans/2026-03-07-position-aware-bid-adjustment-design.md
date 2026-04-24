# Position-Aware Bid Adjustment — Design Document

**Date:** 2026-03-07
**Status:** Approved
**Principles:** 15 (Position Awareness), 16 (Delta Neutral), 17 (Amend Don't Cancel-and-Replace), 18 (Profitable Arb Gate), 19 (Most-Behind-First)

## Problem

Talos detects when resting bids get "jumped" (outbid) but takes no action — it shows a toast notification and `!!` prefix in the UI. The operator must manually cancel and re-place orders. A previous automated system attempted this but failed catastrophically: it lost track of both resting orders and fills, leading to cascading over-placement on one side and runaway exposure.

## Goal

Build a position-aware bid adjustment system that:
1. Maintains an ironclad position model (single source of truth)
2. Proposes bid adjustments when jumped, with full position context
3. Enforces safety invariants structurally — unsafe states are impossible, not just checked for
4. Starts semi-automatic (propose → human approves), graduates to full-auto

## Architecture

Two new modules following the established pure state + async orchestrator split (Principle 13):

```
PositionLedger (pure state machine)     BidAdjuster (async orchestrator)
┌─────────────────────────────┐        ┌──────────────────────────────┐
│ Per-event, independent      │        │ Receives jump events         │
│                             │        │ Queries ledger for state     │
│ Tracks per side:            │◄───────│ Computes proposed actions    │
│  - filled_count             │        │ Checks profitability gate    │
│  - filled_total_cost        │        │ Manages deferred jump queue  │
│  - resting_order_id         │        │ Executes: cancel → place     │
│  - resting_count            │        │ Semi-auto / full-auto mode   │
│  - resting_price            │        └──────────────────────────────┘
│                             │
│ Safety gates:               │        ProposedAdjustment (model)
│  - is_placement_safe()      │        ┌──────────────────────────────┐
│  - unit gating              │        │ event_ticker, side           │
│  - profitability check      │        │ cancel_order_id, cancel_price│
│  - reconciliation           │        │ new_count, new_price         │
│                             │        │ reason, position_before/after│
│ Replaces:                   │        │ safety_check summary         │
│  compute_event_positions()  │        └──────────────────────────────┘
└─────────────────────────────┘
```

## Domain Concepts

### Unit
Atomic bidding quantity. Currently 10 contracts (configurable). All position tracking and safety checks are denominated in units.

### Pair
One unit on side A + one unit on side B of the same event. Only one pair at a time per event. Both sides must fully fill before the next pair deploys.

### Event Lifecycle
```
Empty → Bidding → Partial → Filled → Ready (for next pair)
```
Transition from Partial/Bidding → Filled requires exactly 1 full unit filled on EACH side. 9/10 is not complete.

## Module 1: PositionLedger

Pure state machine. No I/O, no async. One instance per active event. Single source of truth for both UI display and safety gates.

### Per-Side State
```python
filled_count: int              # contracts that have filled
filled_total_cost: int         # sum of (price * count) for all fills, in cents
resting_order_id: str | None   # the one resting order (if any)
resting_count: int             # contracts resting (0 if no order)
resting_price: int             # price of resting order, in cents
```

### Derived Queries
```python
avg_filled_price(side) -> float       # filled_total_cost / filled_count
total_committed(side) -> int          # filled_count + resting_count
units_filled(side) -> int             # filled_count // unit_size
unit_remaining(side) -> int           # unit_size - filled_count if partial, else unit_size
current_delta() -> int                # abs(total_committed(A) - total_committed(B))
is_unit_complete(side) -> bool        # filled_count >= unit_size and filled_count % unit_size == 0
both_sides_complete() -> bool         # is_unit_complete(A) and is_unit_complete(B)
```

### Safety Gate (the critical method)
```python
is_placement_safe(side, count, price) -> tuple[bool, str]
```
Returns `(False, reason)` if:
- `filled_count + resting_count + count > unit_size` — would exceed 1 unit
- `resting_order_id is not None` — order already resting on this side
- Fee-adjusted arb not profitable: `avg_price_other_side + price >= 100 (after fees)`

Returns `(True, "")` otherwise.

### State Mutations
```python
record_fill(side, count, price)                  # a fill came in
record_resting(side, order_id, count, price)     # new order confirmed resting
record_cancel(side, order_id)                    # order cancelled
reset_pair()                                     # both sides complete, clear for next
sync_from_orders(orders: list[Order])            # reconcile against polled state
```

`sync_from_orders` is the safety net. Every 10s polling cycle, the ledger reconciles against Kalshi's reported orders. On mismatch: flag discrepancy, halt all proposals. Never silently correct.

### Replaces compute_event_positions
The ledger becomes the single data source for the UI's position display (filled counts, avg prices, P&L) AND the safety gates. One system, not two — if the UI shows it, the safety logic agrees with it.

## Module 2: BidAdjuster

Async orchestrator. Receives jump events, queries ledger, proposes actions.

### Decision Flow (on jump event)
```
on_jump(ticker, at_top):
  1. Identify event and side from ticker
  2. Get ledger for this event
  3. If at_top=True → back at top, nothing to do
  4. Get new top-of-market price from OrderBookManager
  5. Profitability check (fee-adjusted):
     - Other side has fills: new_price + avg_filled_other >= 100? → WAIT
     - Other side has no fills: new_price + resting_price_other >= 100? → WAIT
     (Use top-of-market price for other side if no fills — more conservative)
  6. Dual-jump tiebreaker:
     - Both sides jumped? Only proceed for side with more remaining contracts
     - Defer the other side (remembered, auto re-evaluated when block clears)
  7. Compute action:
     - count = unit_remaining(side) or current resting count (whichever applies)
     - Proposed: CANCEL old order → PLACE new order (count @ new_price)
  8. Safety gate: ledger.is_placement_safe(side, count, new_price)
     - Unsafe → log reason, do nothing
  9. Emit ProposedAdjustment for human approval (semi-auto)
```

### ProposedAdjustment Model
```python
class ProposedAdjustment(BaseModel):
    event_ticker: str
    side: Literal["A", "B"]
    action: Literal["follow_jump"]
    cancel_order_id: str
    cancel_count: int
    cancel_price: int
    new_count: int
    new_price: int
    reason: str                # "jumped 48c→49c, arb profitable (49+50=99 < 100)"
    position_before: str       # "A: 10 filled @ 50c | B: 6 filled @ 31c, 4 resting @ 32c"
    position_after: str        # "A: 10 filled @ 50c | B: 6 filled @ 31c, 4 resting @ 33c"
    safety_check: str          # "resting+filled=10 ≤ unit(10), arb=83c < 100"
```

### Execution Flow (after approval)
```
execute(proposal):
  1. Call amend_order(order_id, ticker, side, action, no_price=new_price, count=total_count)
     - Single atomic API call — changes price on existing order
     - For partial fills: only unfilled portion moves to new price queue
     - Returns (old_order, amended_order) — both before and after state
  2. Update ledger from amended_order response (resting_price, resting_count)
  3. If amend fails → halt, flag operator. Do NOT fall back to cancel-then-place
  4. Check deferred queue: was the other side waiting? Re-evaluate it now
```

**Why amend over cancel-then-place (Principle 17):**
Amend is a single atomic API call. There is never a moment where two orders exist on the same side (the cascade failure mode), and never a moment where zero orders exist (no gap in queue time). The previous system used cancel-then-place, which created timing windows that caused the cascade. Amend eliminates this entire class of bug.

### Proposal Lifecycle
- New jump supersedes pending proposal on same side (old one expired, logged)
- Operator rejection discards proposal but does not block future proposals
- Deferred jumps (from dual-jump tiebreaker) auto re-evaluate when blocking condition clears

## Module 3: ProposedAdjustment Model

Pydantic model in `src/talos/models/adjustment.py`. Contains all context needed for the operator to make an informed approve/reject decision.

## Integration

### Wiring (callback pattern)
```python
TopOfMarketTracker.on_change → BidAdjuster.on_jump
BidAdjuster.on_proposal → TalosApp (display for approval)
TalosApp (user approves) → BidAdjuster.execute
```

### Polling Integration
- `refresh_account()` (10s) feeds order state to each PositionLedger via `sync_from_orders`
- This is the reconciliation safety net

### Ledger Lifecycle
- `GameManager.on_change` → BidAdjuster creates/destroys ledgers when games added/removed

### UI (semi-auto mode)
- Proposal displayed as modal or notification with full position context
- Operator approves → execute. Rejects → discard and log.
- Proposal expires if superseded by new jump event

### What Does NOT Change
- `TopOfMarketTracker` — already fires on jump state changes
- `OrderBookManager` — already provides `best_ask(ticker)`
- `fees.py` — already computes fee-adjusted arb math
- `ArbitrageScanner` — unrelated to adjustment
- `BidScreen` manual bid placement — still works independently

## Safety Invariants (structural enforcement)

| # | Invariant | Enforced By | Principle |
|---|-----------|-------------|-----------|
| 1 | resting + filled ≤ 1 unit per side | `is_placement_safe()` gate | P16 |
| 2 | No placement without profitable arb (fee-adjusted) | `is_placement_safe()` gate | P18 |
| 3 | No placement if order already rests on side | `is_placement_safe()` gate | P16 |
| 4 | Amend only, never cancel-then-place | `execute()` uses `amend_order()` | P17 |
| 5 | Dual jump: one side at a time, most-behind first | `on_jump()` tiebreaker + deferred queue | P19 |
| 6 | Mismatch with Kalshi → halt and flag | `sync_from_orders()` | P15 |

## Testing Strategy

### PositionLedger (pure — no mocks)
- Fill tracking: counts, avg prices, delta calculation
- Unit completion: exact boundary (9/10 not complete, 10/10 complete)
- Safety gate: reject over-unit, reject duplicate resting, reject unprofitable arb
- Reconciliation: match → no flag, mismatch → flag
- Fractional completion: 6 filled + 4 resting = 10, safe. 6 + 5 = 11, rejected
- Reset pair: clean state after completion

### BidAdjuster (mocks for REST only)
- Single jump, profitable → correct proposal emitted
- Single jump, unprofitable → no proposal
- Dual jump → most-behind first, other deferred
- Deferred re-evaluation on completion
- Deferred but no longer profitable → no proposal
- Execute: cancel → place → ledger updated
- Execute: cancel fails → halt
- Execute: cancel ok, place fails → halt and flag
- Proposal superseded by new jump → old expired

### Integration (mocked REST)
- Full cycle: pair → fills → jump → propose → approve → cancel → place → complete
- Reconciliation: ledger vs polled mismatch → halt

### Skills for Ongoing Verification
- `safety-audit` skill: run after any change to position/order code — checks structural invariants (D1–D6)
- `position-scenarios` skill: run after changes to ledger/adjuster — walks through 8 specific failure scenarios (S1–S8)

## Future Extensions (not in v1)
- Full-auto mode (graduate from semi-auto after trust established)
- Multiple units per pair
- Holding old bid at better price while also bidding at new price
- CPM/ETA-informed decisions (e.g., "queue position is good, don't chase the jump")
- Cross-event exposure limits
