# Scope `avg_filled_price` to the Open Unit

**Date:** 2026-04-15
**Status:** Approved for implementation planning

## Problem

`PositionLedger.avg_filled_price` returns the cumulative average across every fill the ledger has ever recorded on a side. Decision-path callers (`is_placement_safe`, `BidAdjuster.evaluate_jump`, rebalance catch-up fallback, queue-stress safety gate) use that cumulative average as the "other-side price" in P18 profitability checks.

Consequence: favorable closed units subsidize placements in the current open unit. A position that earlier locked in a 92/7 pair can later justify a 86/18 "catch-up" because the Side-B ledger average looks like 12.5c (the blend) even though the only open-unit fill on B is at 18c. The catch-up fills at 86, the unit is actually a 104c loser, and the ledger never objects.

The same bug in the other direction makes Talos refuse a profitable jump: if Side A's lifetime average is 83c but the most recent matched unit closed at 79c, a jump on Side B from 17c to 18c gets blocked (18+83=101) even though the still-open unit's actual basis is 79c (18+79=97, +3c edge).

Both observed behaviors come from the same root cause: decisions about the **currently open unit** use averages computed across **all lifetime fills**.

## Goal

Make every decision-path P18 profitability check use the average of the **open unit only**. Preserve lifetime averages for display and PnL accounting.

## Non-goals

- Changing `OpportunityProposer.evaluate` Gate 6 (strict `< 0`). The user explicitly accepts 0-EV exits on fee-free markets.
- Tracking manual sells or close-out orders in the ledger. That's a separate problem.
- Any UI work beyond leaving existing displays on the lifetime avg.

## Design

### 1. Data model — new "closed" bucket

Add three cumulative counters to `PositionLedger._SideState`, mirroring the existing `filled_*` counters:

```python
closed_count: int = 0
closed_total_cost: int = 0
closed_fees: int = 0
```

Semantic: contracts in the closed bucket belong to a completed, balanced unit. They never leave the bucket and never influence decision averages. They remain summed into `filled_count` / `filled_total_cost` / `filled_fees` so lifetime accessors are unchanged.

Derived quantities:

- **Open count** = `filled_count - closed_count`
- **Open cost** = `filled_total_cost - closed_total_cost`
- **Open fees** = `filled_fees - closed_fees`

### 2. New accessors

```python
def open_count(self, side: Side) -> int:
    s = self._sides[side]
    return s.filled_count - s.closed_count

def open_avg_filled_price(self, side: Side) -> float:
    s = self._sides[side]
    open_count = s.filled_count - s.closed_count
    if open_count <= 0:
        return 0.0
    open_cost = s.filled_total_cost - s.closed_total_cost
    return open_cost / open_count
```

`open_count` matters for the P18 guard condition (see section 4). Callers currently check `filled_count > 0` before reading `avg_filled_price`. After the fix, the correct guard is `open_count > 0`: if the open unit is empty (e.g., immediately after a close), P18 should behave as "no other-side position," falling through to the existing resting/book branch.

Existing `avg_filled_price(side)` and `filled_count(side)` stay as-is. They continue to return the lifetime blended average / lifetime count for display and PnL.

### 3. Close trigger — after every fill recording

Add a private helper invoked at the end of `PositionLedger.record_fill`:

```python
def _try_close_matched_units(self) -> None:
    a = self._sides[Side.A]
    b = self._sides[Side.B]
    open_a = a.filled_count - a.closed_count
    open_b = b.filled_count - b.closed_count
    matchable = min(open_a, open_b)
    units_to_close = matchable // self.unit_size
    if units_to_close == 0:
        return
    contracts = units_to_close * self.unit_size
    for side_state in (a, b):
        open_count = side_state.filled_count - side_state.closed_count
        open_cost = side_state.filled_total_cost - side_state.closed_total_cost
        open_fees = side_state.filled_fees - side_state.closed_fees
        side_state.closed_count += contracts
        side_state.closed_total_cost += round(open_cost * contracts / open_count)
        side_state.closed_fees += round(open_fees * contracts / open_count)
```

Pro-rata cost/fee flushing: the ledger doesn't track per-fill FIFO, so we can't say "these specific fills closed." Pro-rata keeps the open-bucket average unchanged across a close — the mean of what remains equals the mean of what left. This is the correct behavior: closing a balanced unit should not shift the residual open average.

**Rounding drift caveat.** `round(open_cost * contracts / open_count)` returns an integer. When the division isn't exact, the remaining open bucket carries a ≤1-cent error relative to the "true" fractional split. Over many closes with balanced banker's-rounding, drift is zero-mean and bounded to a few cents across the life of a ledger. Do not "fix" this by switching `filled_total_cost` to floats — the ledger's integer discipline is load-bearing elsewhere (API reconciliation, persistence). If drift ever becomes measurable, switch to per-fill FIFO accounting, which is a separate, larger change.

### 4. Call sites

**Decision path — switch to `open_avg_filled_price` AND update the guard condition:**

| Site | Current | Fix |
|------|---------|-----|
| `PositionLedger.is_placement_safe` [position_ledger.py:220-227](src/talos/position_ledger.py:220) | `if other.filled_count > 0: other_price = other.filled_total_cost / other.filled_count` | `if self.open_count(side.other) > 0: other_price = self.open_avg_filled_price(side.other)` |
| `BidAdjuster.evaluate_jump` [bid_adjuster.py:220-232](src/talos/bid_adjuster.py:220) | `if ledger.filled_count(other_side) > 0: other_effective = fee_adjusted_cost(round(ledger.avg_filled_price(other_side)), ...)` | `if ledger.open_count(other_side) > 0: other_effective = fee_adjusted_cost(round(ledger.open_avg_filled_price(other_side)), ...)` |
| `rebalance.compute_rebalance_proposal` fallback [rebalance.py:139-141](src/talos/rebalance.py:139) | `if over_side_state.filled_count > 0: other_avg = over_side_state.filled_total_cost / over_side_state.filled_count` | Use `ledger.open_count(over)` and `ledger.open_avg_filled_price(over)` |
| `engine.check_queue_stress` [engine.py:2521-2524](src/talos/engine.py:2521) | `other_avg = ledger.avg_filled_price(ahead_side); if other_avg <= 0: continue` | `other_avg = ledger.open_avg_filled_price(ahead_side); if other_avg <= 0: continue` — the `<= 0` guard already handles the empty-open-unit case without needing a separate `open_count` check |

**Display / PnL — keep `avg_filled_price`:**

- `fees.scenario_pnl`, `fees.fee_adjusted_profit_matched` (position-wide settlement math)
- UI position panel / review panel avg rendering
- Any reporting or export paths

**Decision-log `effective_other` field:** write the **open** avg (so replay shows the number the gate actually used). The decision log should explain the decision, not the position aggregate.

Before touching call sites, run a grep for `avg_filled_price` and `filled_total_cost` across `src/talos/` and classify every hit. Flag any ambiguous site for user review rather than guess.

### 5. Cold-start reconstruction from Kalshi

`refresh_account` / reconciliation sets `filled_count` and `filled_total_cost` from Kalshi's position-endpoint data. Kalshi reports only the current blended average per side — no unit structure.

On reconstruction, after the ledger state is synced, run one close pass: `min(filled_A, filled_B) // unit_size × unit_size` contracts move into the closed bucket using the reported blended cost as the basis. The remainder stays open.

This matches the post-fix behavior for freshly-opened positions. Approximation caveat: reconstruction treats Kalshi's blended avg as uniform across all contracts, so for a position that was built unit-by-unit at varying prices, the approximation carries a small error. Kalshi doesn't expose the fill-level data needed to do better.

Emit one log line per reconstruction-close so the paper trail exists for debugging:

```
ledger_reconstruction_closed event=... side=A contracts=N open_remaining=M blended_avg=X
```

### 6. Persistence

Verify during implementation whether the ledger is persisted between restarts. If yes:

- Schema additions for `closed_count`, `closed_total_cost`, `closed_fees`
- Migration: on first read from an older persisted file, initialize all three to 0 and rely on the reconstruction close pass (section 5) to populate them on the next account refresh

If no persistence exists, nothing to do.

### 7. Edge cases

- **Unit size changes** (`set_unit_size`). Closed fills stay closed regardless — re-bucketing closed contracts would be arbitrary. Any mid-life unit-size change only affects how the open bucket is gated on placement and when the *next* close fires. The existing `set_unit_size` implementation needs to leave `closed_*` fields untouched.
- **Within-unit cross-jump fills** (the user's "bid 80 fill 2, jump to 81 fill 3" case). Handled automatically: both fills accumulate into the open bucket; the open avg is the weighted mean (80.6); no close fires because the unit isn't complete. No additional logic needed.
- **Imbalanced close** (e.g., A=5 @ 82, B=10 = 5 @ 18 + 5 @ 23). After close: 5 contracts flush each side. A open = 0. B open = 5 @ 23. Next catch-up on A uses B's open avg of 23 — matches user's stated intent.
- **Fill reversals / cancellations.** Out of scope — the ledger's existing behavior around cancelled fills is unchanged. If a close fires on a fill that later gets reversed, the closed bucket has no "unwind" path. This matches the existing ledger's non-reversibility and is an accepted limitation.
- **Open avg = 0.0 semantics.** Callers currently treat `filled_count == 0` and `avg_filled_price == 0.0` as "no other-side position — bypass P18." The new accessor returns 0.0 for an empty open unit (immediately after a close, before the next fill), which means the very next placement sees P18 effectively disabled. This is intended: it's the same as a fresh position, which is exactly what the open unit is post-close.

## Testing

### Unit tests — `tests/test_position_ledger.py`

- Close trigger fires at correct boundary (A=5, B=5 at unit_size=5 → one close).
- Close trigger doesn't fire when unequal (A=5, B=4).
- Close trigger fires exactly once for a multi-unit imbalance (A=10, B=10 → two units close, `closed_count == 10` on each side).
- `open_avg_filled_price` = 0 for a fresh ledger.
- `open_avg_filled_price` equals the open-bucket weighted avg during a partial unit.
- `open_avg_filled_price` resets to 0 immediately after a close flushes the last open contracts.
- Sequential units: fill unit 1 at 92/7, unit 2 at 82/18. After both close, `open_avg_filled_price` = 0 on both sides. `avg_filled_price` = 87 on A, 12.5 on B.
- Imbalanced close: A=5 @ 82, B=5 @ 18 + 5 @ 23. After close, A open = 0 with open avg 0; B open = 5 with open avg 23.
- Within-unit cross-jump: A bid 80 fill 2 then 81 fill 3. Open avg A = 80.6 exactly. No close fires.

### Regression tests — `tests/test_bid_adjuster.py`

- **Jump-follow scenario from the session.** Setup: unit 1 closed at A=92/B=7, unit 2 closed at A=82/B=18, unit 3 closed at A=80/B=19, unit 4 closed at A=82/B=23, unit 5 closed at A=80/B=17, unit 6 exit (A=80 closed at 79 sell-equivalent), B=5 resting at 17. Market moves B ask to 18. With the fix: `evaluate_jump` for B returns `follow_jump` because open avg on A = 0 (no open A fills) → P18 bypassed and 18c is the new target. Without the fix: returns `hold_unprofitable` using lifetime avg 83c.
- **Catch-up rejection scenario.** Setup: unit 1 closed at A=92/B=7, new unit open: A=5 resting @ 82 (no fills yet), B=5 filled @ 18. Market moves A ask to 86. `compute_rebalance_proposal` catch-up fallback: `max_profitable_price(open_avg_B=18)` = 81. Proposed catch-up is Yes @ 81, not 86. Without the fix: open avg would be 12.5, catch-up at 86 passes.

### Integration — `tests/test_engine.py` (or new `tests/test_ledger_reconstruction.py`)

- Cold-start reconstruction with balanced fills: A=10, B=10 at unit_size=5 → both sides close 10 contracts; open bucket empty.
- Cold-start with imbalanced fills: A=10, B=5 → close 5 on each side; A open = 5, B open = 0.
- Cold-start reconstruction logs a line per side when a close fires.

## Out of scope (flagged for follow-ups)

- `OpportunityProposer` Gate 6 tightening for fee-free markets
- Manual-sell / close-out handling in the ledger
- UI display of `fee_type` so a fee-free market is visually distinct from a fee-paying one
- Extending the ledger with per-fill FIFO so exact cost-basis accounting becomes possible (would also enable true unwind on cancellation)

## Rollout

This is a pure-logic fix with existing test coverage in place. No migration-gating or flag required. Land it, run the full suite, spot-check the decision log on a live event after deploy.
