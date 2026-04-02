# Queue-Aware Price Improvement

**Date:** 2026-04-01
**Status:** Approved

## Problem

When a partially-filled arb pair has its unfilled side stuck deep in queue (ETA exceeds time remaining before game), Talos does nothing. A human trader would improve the bid by 1c to leapfrog the queue while remaining profitable. Currently, queue position is fetched and displayed but never used for decisions.

Example: DET filled 5/5 at 57.4c, MIN resting 0/5 at 41c with 186k queue and 23h ETA. Game starts in ~18h. Improving to 42c puts us near the front of queue — still profitable, still a resting order.

## Design

### Detection: `check_queue_stress()` in engine.py

New method that runs during the existing `refresh_account` cycle (~30s). Scans all partially-filled pairs and evaluates whether the behind side's resting order is stuck.

**Trigger condition:** `ETA > time_remaining`

Where:
- `ETA = queue_position / CPM` (already computed, stored in `LegSummary.eta_minutes`)
- `time_remaining = game_time - now`, using the same source as the Game column in the table:
  - `GameStatus.scheduled_start` if available (from `GameStatusResolver`)
  - Falls back to `ArbPair.close_time` (non-sports events)
- A pair is "partially filled" when one side has `filled_count > 0` and the other side has `filled_count < filled_count` of the first side

**Skip conditions:**
- No game time available for this event
- No ETA available (no queue position or CPM data)
- A proposal already exists for this event in the proposal queue
- The behind side has no resting order
- The pair is not partially filled

### Action: Propose 1c improvement

Generate a `Proposal` with a new `ProposedQueueImprovement` payload:

```python
class ProposedQueueImprovement(BaseModel):
    """Proposed price improvement to escape deep queue."""
    event_ticker: str
    side: Literal["A", "B"]          # The behind/stuck side
    order_id: str                     # Resting order to amend
    ticker: str                       # Market ticker
    current_price: int                # Current resting price (e.g., 41c)
    improved_price: int               # Proposed price (e.g., 42c)
    current_queue: int                # Queue position before (e.g., 186903)
    eta_minutes: float                # Current ETA in minutes
    time_remaining_minutes: float     # Minutes until game time
    other_side_avg: float             # Other side's average fill price
    kalshi_side: str                  # "yes" or "no" for API call
```

The proposal flows through the existing `ProposalQueue` and appears in `ProposalPanel` for operator approval (or auto-accept).

### Proposal integration

Add to `ProposalKey.kind` and `Proposal.kind`:
- New literal: `"queue_improve"`

Add to `Proposal`:
- New optional field: `queue_improve: ProposedQueueImprovement | None = None`

### Execution

On approval, uses `amend_order()` (atomic cancel + replace) — same mechanism as jump-follows in `BidAdjuster.execute()`. Updates ledger with new resting price.

### Safety Gates

All checked **before** generating the proposal:

1. **Profitability (P18):** `fee_adjusted_cost(improved_price) + fee_adjusted_cost(other_side_avg) < 100`. Uses existing `fee_adjusted_cost()` from `fees.py`.
2. **No spread crossing:** `improved_price < best_ask` on the same market. The improved order must remain a resting post, not a take. Uses `OrderBookManager.best_ask()`.
3. **Partially filled only:** At least one side must have fills, and the stuck side must be behind.
4. **No duplicate proposals:** Skip if the proposal queue already has an active proposal for this event.

Re-checked at execution time with fresh data (same pattern as rebalance and jump-follow execution).

### Repeat Behavior

Each 30s cycle re-evaluates all pairs. If 42c still has `ETA > time_remaining`, proposes 43c next cycle. Natural stopping points:
- **Filled** — queue position drops to 0, ETA no longer exceeds time remaining
- **Edge exhausted** — P18 gate blocks the next improvement (e.g., 47c is unprofitable)
- **ETA resolved** — queue drains faster at the new price, ETA drops below time remaining
- **Proposal queue throttle** — only one proposal per event active at a time

### Edge Cases

| Case | Behavior |
|------|----------|
| CPM = 0 (dead market) | ETA = infinity, triggers improvement. Being first at 42c in a dead market beats 186k back at 41c. |
| No game time available | Skip — cannot compute time_remaining. |
| Already at best bid | Still improves — 1c above creates a new price level with zero queue. |
| Improvement would cross spread | Blocked by safety gate #2. Proposal not generated. |
| Both sides partially filled, both stuck | Improve the side with fewer fills (the "behind" side). If equal fills, improve the side with worse ETA. Only one proposal per event, so only one side per cycle. |

### UI: Proposal Panel Display

Queue improvement proposals display in `ProposalPanel` with a summary like:
```
QUEUE: #35 MIN 41c → 42c (queue 186k, ETA 23h, game in 18h)
```

## Files Changed

| File | Change |
|------|--------|
| `models/proposal.py` | Add `ProposedQueueImprovement` model, extend `ProposalKey.kind` and `Proposal` |
| `engine.py` | Add `check_queue_stress()`, wire into `refresh_account` cycle, add execution handler |
| `fees.py` | No change — reuse `fee_adjusted_cost()` and `max_profitable_price()` |
| `ui/proposal_panel.py` | Render queue improvement proposals |
| `ui/screens.py` | Handle approval/rejection of queue improvement proposals |
| Tests | New test file `tests/test_queue_improvement.py` |

## Non-Goals

- Changing entry rules (initial bid placement is unchanged)
- Improving by more than 1c at a time
- Auto-executing without proposal queue
- Applying to pairs with zero fills on both sides
