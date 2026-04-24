# Auto Catch-Up Design

## Problem

When one side of an arb pair fills faster than the other, Talos detects the imbalance and creates a rebalance proposal — but that proposal sits in the ProposalQueue waiting for operator approval. Meanwhile, the lagging side has zero resting bids trying to close the gap. At 250+ games with 30s polling cycles, this delay leaves significant unhedged exposure and is the primary cause of getting "trapped" on one side.

Getting trapped on one side is the #1 factor preventing profitability. Talos is roughly breakeven; reducing how often positions get trapped would directly improve returns.

## Solution

Auto-execute rebalance catch-ups with full gap closure in one shot. Keep notifications and structured logging for the audit trail, but remove the operator approval gate for rebalance actions.

## Design

### 1. Detection: `compute_rebalance_proposal` Changes

Three fixes to the pure detection function:

**a) Target = `over_filled` (not `max(over_filled, under_committed)`)**

The over-side's resting orders are always cancelled first — reduce to what's actually filled. Then catch up the under-side to match.

Before:
```python
target = max(over_filled, under_committed)
```

After:
```python
target = over_filled
```

**b) Remove unit_size cap on catchup_qty**

Catch-up places the full gap in one shot, not one unit at a time.

Before:
```python
catchup_qty = min(effective_gap, ledger.unit_size)
```

After:
```python
catchup_qty = effective_gap
```

Example: A = 40 filled + 10 resting, B = 15 filled + 0 resting.
- Cancel 10 resting on A (A → 40 filled, 0 resting)
- Place 25 on B (B → 15 filled + 25 resting = 40 committed)
- Done in one cycle

**c) Top-up check for mid-unit gaps**

New logic, separate from delta-based imbalance detection. Runs after the catch-up check in `check_imbalances()`. Mutually exclusive with catch-up — top-up only fires when committed counts are already equal.

If both sides have equal committed counts but either side is mid-unit (`filled_in_unit > 0`) with 0 resting, place enough to complete that unit. Each side is evaluated independently — one side may need top-up while the other already has resting.

Example: A = 15 filled + 0 resting, B = 12 filled + 0 resting.
- A top-up: 5 @ scanner price (15 + 5 = 20 = 1 complete unit)
- B top-up: 8 @ scanner price (12 + 8 = 20 = 1 complete unit)

Top-up uses the same `scanner_snapshot.no_a` / `scanner_snapshot.no_b` prices as catch-up.

Top-up does NOT fire when:
- Side already has resting orders
- Side is at a complete unit boundary (e.g., 20 filled, 0 resting)
- Exit-only mode is active for the event
- Scanner snapshot is unavailable (can't determine price)

**Top-up execution model:** Each side that needs topping up produces a separate placement call (not a `ProposedRebalance`). Top-up is simpler than catch-up — no reduce step, no fresh sync needed (the current cycle's sync is fresh enough since there's no imbalance to verify). Standard `is_placement_safe()` applies with `catchup=False` — P16 unit boundary IS enforced for top-up since we're completing a unit, not bridging across units.

### 2. Execution: Auto-Execute in `check_imbalances()`

**`check_imbalances()` becomes async and auto-executes instead of queuing.**

Current flow:
```
check_imbalances() → compute_rebalance_proposal() → ProposalQueue → operator approves → execute_rebalance()
```

New flow:
```
check_imbalances() → compute_rebalance_proposal() → execute_rebalance() → notify operator
```

Key details:
- `check_imbalances()` signature changes to `async def check_imbalances(self)`
- `refresh_account()` `await`s it instead of calling synchronously (only call site)
- Notifications fire after execution (operator sees what happened, not what's proposed)
- Fresh Kalshi sync inside `execute_rebalance()` is the safety net — even if detection used slightly stale data, execution re-verifies before placing (P7/P15)
- `is_placement_safe()` profitability gate (P18) still runs before any catch-up placement
- **Double-fire guard:** A `set[str]` of event tickers where rebalance was executed this cycle, cleared at the start of each `check_imbalances()` call. Checked before processing each pair — if the event ticker is in the set, skip it.
- Structured logging preserved: `position_imbalance`, `rebalance_catchup_placed`, etc.
- **Exit-only guard:** Before calling `compute_rebalance_proposal()`, check if the event is in exit-only mode. If so, skip — exit-only has its own cancellation flow. Implemented as an `exit_only_tickers: set[str]` parameter or checked inline against `self._exit_only_events`.

### 3. Safety Gate: Bypass Unit-Boundary for Catch-Up

`is_placement_safe()` currently enforces P16: `filled_in_unit + resting + new <= unit_size`. This would block catch-ups larger than one unit.

For catch-up placements, the unit-boundary check (P16) is bypassed. Rationale: P16 prevents new speculative exposure. Catch-up is not speculative — it's closing an existing gap. Having 15 filled + 25 resting on the under-side is *less* risky than having 15 filled + 0 resting while the other side has 40 filled.

The profitability gate (P18) remains enforced — catch-up must still satisfy `effective_this + effective_other < 100`.

Implementation: add a `catchup: bool = False` parameter to `is_placement_safe()`. When `True`, skip the unit-boundary check, keep the profitability check.

Note: top-up does NOT use `catchup=True` — top-up completes a unit, which is exactly what P16 governs. Only cross-unit catch-up bypasses P16.

### 4. Execution Safety: Fresh Sync Fixes

Two fixes to `execute_rebalance()` to harden safety now that operator approval is removed:

**a) Use `get_all_orders()` instead of `get_orders(limit=200)`**

The current fresh sync uses `get_orders(limit=200)` which silently truncates at 250+ games. With no operator gate, a truncated sync could lead to placing on a side that already has resting orders from missed data. Switch to `get_all_orders()` for the pre-placement sync.

**b) Recalculate `catchup_qty` from fresh ledger state**

After the fresh sync, the current code checks `fresh_delta > 0` but uses the proposal's original `catchup_qty`. With the unit-size cap removed, stale quantities can overshoot. After fresh sync, recompute:

```python
fresh_catchup_qty = fresh_over_filled - fresh_under_committed
catchup_qty = max(0, fresh_catchup_qty)
```

If the recalculated qty is 0, skip placement (the gap closed between detection and execution).

### 5. What Does NOT Change

- Manual bid proposals still go through ProposalQueue with operator approval
- Adjustment proposals (penny jump responses) still go through ProposalQueue
- Exit-only mode still prevents catch-up and top-up
- All structured logging and notifications preserved
- Order groups for fill-limit safety

### 6. Edge Cases

| Scenario | Behavior |
|----------|----------|
| Scanner snapshot missing/stale | Catch-up and top-up skipped, notification fires |
| Fresh sync shows imbalance resolved | Catch-up aborts (recalculated qty = 0) |
| Multiple events imbalanced | Each processes independently, same pass |
| Race with operator manual bids | Fresh sync sees them, recalculated qty adjusts |
| Exit-only active | No catch-up or top-up; guard in `check_imbalances()` skips event |
| Both sides mid-unit, balanced committed | Top-up fires (not catch-up); each side topped up independently |
| API error during catch-up placement | Notification fires, retried next cycle (30s) |
| Truncated order fetch (250+ games) | Fixed: uses `get_all_orders()` for fresh sync |

## Principles Alignment

| Principle | How This Design Adheres |
|-----------|------------------------|
| P1 (Safety Above All) | Fresh sync + profitability gate before every placement |
| P2 (Human in the Loop) | Moves rebalance from "supervised" to "autonomous" per P2's progression model. Justified: catch-up is risk-reducing (closing unhedged exposure), not risk-taking. Fresh sync + P18 gate provide the safety net. The 30s manual approval delay is the primary cause of trapped positions — the very problem this solves. |
| P4 (Subtract Before You Add) | Minimal code change — reuses existing rebalance detection + execution |
| P6 (Boring and Proven) | Extends existing polling loop, no new infrastructure |
| P7 (Kalshi Is Source of Truth) | Fresh sync in `execute_rebalance` before any placement |
| P9 (Idempotency and Resilience) | Catch-up is not "automatic recovery" — it's a structural correction to maintain delta neutrality. The fresh sync + recalculated qty make it safe to retry. On failure, it halts and notifies (does not retry within the same cycle). |
| P10 (Correctness Over Speed) | 30s cycle is fine — safety checks are not skipped for latency |
| P15 (Position Accuracy) | Re-fetch with `get_all_orders()` and re-verify before every money-touching action |
| P16 (Delta Neutral) | Entire purpose is restoring delta neutrality faster |
| P18 (Profitable Arb Gate) | Still enforced for all catch-up placements |
| P20 (Inaction Is Visible) | Notifications fire whether catch-up executes or is skipped |

## Files Affected

| File | Change |
|------|--------|
| `src/talos/rebalance.py` | Fix target, remove unit cap, add top-up detection, recalculate catchup_qty after fresh sync, use `get_all_orders()` |
| `src/talos/engine.py` | `check_imbalances()` → async with auto-execute, exit-only guard, double-fire guard, await in `refresh_account()` |
| `src/talos/position_ledger.py` | `is_placement_safe()` gains `catchup` parameter |
| `tests/test_rebalance.py` | Update expected catchup_qty, add top-up tests, test recalculated qty |
| `tests/test_position_ledger.py` | Test `catchup=True` bypasses unit gate, `catchup=False` preserves it |
| `tests/test_engine.py` | Test auto-execution flow, exit-only guard, double-fire guard |
