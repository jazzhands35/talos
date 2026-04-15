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

### 3. Close reconciliation — invariant, not a single-hook

**Invariant: every mutation path that increases `filled_count` or `filled_total_cost` MUST invoke `_reconcile_closed()` immediately after the mutation completes.**

This is non-negotiable. Multiple paths mutate filled state in the current code; a single-hook design that only fires on `record_fill` leaves `closed_*` stale whenever fills are learned from polling, positions augmentation, or persisted-state restoration. The invariant must hold regardless of which path learned about the fills.

The reconciliation helper:

```python
def _reconcile_closed(self) -> None:
    """Flush any newly-matched pairs from the open bucket into the closed bucket.

    Idempotent: safe to call multiple times. If no new units can close,
    returns without mutation.

    Must be invoked after ANY mutation that increases filled_count or
    filled_total_cost — see section 3a for the exhaustive call-site list.
    """
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

Idempotence is essential: some paths may run reconciliation against state that's already been reconciled by a prior fill event (e.g., sync-after-WS). The `units_to_close == 0` early exit makes repeated calls free.

### 3a. Required invocation sites

Every site listed below must call `_reconcile_closed()` after its mutation block completes. Omitting any one re-introduces the bug on the affected path.

| Site | Line | Mutation |
|------|------|---------|
| `record_fill` | [position_ledger.py:244-256](src/talos/position_ledger.py:244) | Increments `filled_count`, `filled_total_cost`, `filled_fees` from WS fill events |
| `sync_from_orders` | [position_ledger.py:449-453](src/talos/position_ledger.py:449) | Overwrites `filled_count` / `filled_total_cost` / `filled_fees` from REST orders poll |
| `sync_from_positions` | [position_ledger.py:553-576](src/talos/position_ledger.py:553) | Augments `filled_count` / `filled_total_cost` from REST positions endpoint (early-returns for same-ticker; still needs reconcile in the branches that run) |
| `seed_from_saved` | [position_ledger.py:345-370](src/talos/position_ledger.py:345) | Restores `filled_count` / `filled_total_cost` / `filled_fees` from persisted state at startup — **this is the only reconciliation path for same-ticker ledgers** (see section 5 on same-ticker restart) |

Each of the four methods has a natural terminal point; invocation is a single added line. No refactor needed.

An alternative that was considered and rejected: making `filled_count` / `filled_total_cost` setter-properties that auto-reconcile. Rejected because these fields are assigned inside the same mutation block multiple times (e.g., both count and cost updated in sequence), and reconciling mid-block would operate on an inconsistent intermediate state. A terminal call per path is both simpler and correct.

### 3b. Pro-rata flushing rationale

The ledger doesn't track per-fill FIFO, so we can't say "these specific fills closed." Pro-rata keeps the open-bucket average unchanged across a close — the mean of what remains equals the mean of what left. This is the correct behavior: closing a balanced unit should not shift the residual open average.

**Rounding drift caveat.** `round(open_cost * contracts / open_count)` returns an integer. When the division isn't exact, the remaining open bucket carries a ≤1-cent error relative to the "true" fractional split. Over many closes with balanced banker's-rounding, drift is zero-mean and bounded to a few cents across the life of a ledger. Do not "fix" this by switching `filled_total_cost` to floats — the ledger's integer discipline is load-bearing elsewhere (API reconciliation, persistence). If drift ever becomes measurable, switch to per-fill FIFO accounting, which is a separate, larger change.

### 4. Call sites

The call-site table below is **exhaustive** — derived from a grep of `avg_filled_price` and `filled_total_cost` across `src/talos/`. Anyone adding a new decision-path site in the future must also extend this table and the corresponding test coverage.

**Decision path — switch to `open_avg_filled_price` AND update the guard condition:**

| Site | Current | Fix |
|------|---------|-----|
| `PositionLedger.is_placement_safe` [position_ledger.py:220-227](src/talos/position_ledger.py:220) | `if other.filled_count > 0: other_price = other.filled_total_cost / other.filled_count` | `if self.open_count(side.other) > 0: other_price = self.open_avg_filled_price(side.other)` |
| `BidAdjuster.evaluate_jump` [bid_adjuster.py:346-351](src/talos/bid_adjuster.py:346) | `if ledger.filled_count(other_side) > 0: other_effective = fee_adjusted_cost(round(ledger.avg_filled_price(other_side)), ...)` | `if ledger.open_count(other_side) > 0: other_effective = fee_adjusted_cost(round(ledger.open_avg_filled_price(other_side)), ...)` |
| `BidAdjuster._check_post_cancel_safety` [bid_adjuster.py:842-843](src/talos/bid_adjuster.py:842) | `if ledger.filled_count(other_side) > 0: other_price = ledger.filled_total_cost(other_side) / ledger.filled_count(other_side)` | `if ledger.open_count(other_side) > 0: other_price = ledger.open_avg_filled_price(other_side)` |
| `rebalance.compute_rebalance_proposal` fallback [rebalance.py:139-141](src/talos/rebalance.py:139) | `if over_side_state.filled_count > 0: other_avg = over_side_state.filled_total_cost / over_side_state.filled_count` | Use `ledger.open_count(over)` and `ledger.open_avg_filled_price(over)` |
| `engine.check_queue_stress` (propose time) [engine.py:2521-2524](src/talos/engine.py:2521) | `other_avg = ledger.avg_filled_price(ahead_side); if other_avg <= 0: continue` | `other_avg = ledger.open_avg_filled_price(ahead_side); if other_avg <= 0: continue` — the `<= 0` guard already handles the empty-open-unit case |
| `engine` queue-improvement execution recheck [engine.py:3164-3167](src/talos/engine.py:3164) | `other_avg = ledger.avg_filled_price(side.other); if other_avg <= 0: ...` | `other_avg = ledger.open_avg_filled_price(side.other); if other_avg <= 0: ...` — same `<= 0` guard pattern |

Missing any site from this list lets the old blended-avg behavior persist on that code path — an especially subtle regression because only one direction of the bug (the permissive one) would be visible in logs. The restrictive direction (refusing profitable jumps) silently preserves losses.

**Display / PnL — keep `avg_filled_price`:**

| Site | Purpose |
|------|---------|
| `engine.py:1605-1606` | PnL / outcome calculation for event settlement |
| `bid_adjuster.py:873` | Proposal detail string ("5 filled @ 83.2c") shown in review panel |
| `position_ledger.py:589` | Info-level structured log after sync, not a decision |
| `position_ledger.py:630-631` | PnL helper exposing lifetime totals |
| `ui/event_review.py:353-354` | Review-panel position summary display |

The `fees.scenario_pnl` and `fees.fee_adjusted_profit_matched` functions take raw totals as parameters, not a ledger reference, so they don't appear in the grep but are equivalent to the position-wide PnL category above — callers of those functions pass lifetime totals.

**Decision-log `effective_other` field:** write the **open** avg (so replay shows the number the gate actually used). The decision log should explain the decision, not the position aggregate.

### 5. Cold-start reconstruction and migration

With the invariant from section 3 in place, cold-start reconstruction is not a special case — it's an automatic consequence of `_reconcile_closed()` being invoked at the end of every mutation path, including `seed_from_saved`, `sync_from_orders`, and `sync_from_positions`.

**Two ledger categories to consider on startup:**

**(a) Non-same-ticker ledgers** (e.g., two separate Kalshi markets for a sports arb). Startup sequence:

1. `seed_from_saved` restores persisted `filled_*` → `_reconcile_closed()` at method end → `closed_*` populated from the blended totals for whichever balanced portion matches a unit boundary.
2. `sync_from_orders` runs next, may overwrite filled totals with fresh REST data → `_reconcile_closed()` re-runs, idempotent.
3. `sync_from_positions` runs last, augments if positions-endpoint shows more fills than orders-endpoint knew → `_reconcile_closed()` re-runs, idempotent.

The first non-trivial reconciliation wins; subsequent calls are no-ops because the units are already closed.

**(b) Same-ticker ledgers** (YES + NO on one market — the Jeff Probst event that motivated this spec). Startup sequence:

1. `seed_from_saved` restores persisted `filled_*` → `_reconcile_closed()` at method end → `closed_*` populated.
2. `sync_from_orders` runs; may update filled totals → `_reconcile_closed()` re-runs.
3. `sync_from_positions` **early-returns at [position_ledger.py:544](src/talos/position_ledger.py:544) without mutating anything** — the positions endpoint reports net YES-minus-NO for same-ticker, which is useless for pair accounting. No reconciliation needed here.

**Without the invariant from section 3**, same-ticker ledgers would never populate `closed_*` after restart — persistence would hand them unreconciled state and no other path would fix it. This is the migration trap that Codex flagged (P1 #2). The invariant closes it: as long as every mutation path reconciles, same-ticker restart falls out for free via the `seed_from_saved` invocation.

**Approximation caveat.** Reconstruction treats the restored blended avg as uniform across all contracts, which is wrong for a position built unit-by-unit at varying prices. Kalshi only gives us the blend — we can't do better without historical per-fill data. For positions that were well-behaved (all units at similar prices), the approximation is negligible; for positions with a wide spread across units, the residual open avg will be mis-attributed until the next unit closes cleanly. We accept this because there's no data source that would let us do better, and the alternative (defer all decisions until a fresh in-memory fill happens) would brick the system post-restart.

**Paper-trail log.** When `_reconcile_closed()` actually closes anything (non-idempotent path), emit:

```
ledger_reconciled_closed event=<ticker> units_closed=N contracts=M open_a=X open_b=Y avg_a=A avg_b=B
```

### 6. Persistence schema

The persisted ledger schema needs the three new fields per side: `closed_count_a/b`, `closed_total_cost_a/b`, `closed_fees_a/b`.

Migration from existing persisted files: on first read, `seed_from_saved` sees no `closed_*` keys and initializes them to 0. The terminal `_reconcile_closed()` call then populates them from the restored blended totals. No explicit migration script needed; it's a one-shot cold reconciliation on first boot after the change lands.

During implementation, grep for the actual persistence entry point (`save_games_full` / `load_saved_games_full` in `persistence.py`) and confirm the exact key names. Add the new keys to the serialization path. If any test fixtures include serialized ledger state, update the fixtures.

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
- **`_check_post_cancel_safety` uses open avg.** Setup: closed unit at A=92/B=7, open unit A=5 filled @ 82, B=5 filled @ 18. Simulate a cancel+replace on A at price 86. Expect block (82 was already at the safe edge; moving to 86 against open B avg of 18 gives 104). Without the fix: uses lifetime B avg of 12.5, passes.
- **Queue-improvement execution recheck uses open avg.** Setup: a queue-improve proposal was approved when open avg was favorable. Between approval and execution, a fresh unfavorable fill lands on the ahead side (increasing open avg). Execution recheck reads `open_avg_filled_price` and blocks. Without the fix: uses lifetime avg (still favorable due to dilution), executes a losing amend.

### Reconciliation-invariant tests — `tests/test_position_ledger.py`

These guard the section 3 invariant — any future mutation path that forgets to call `_reconcile_closed()` must fail a test.

- `record_fill` triggers reconciliation. Fill enough on both sides to complete a unit; assert `closed_count` increased.
- `sync_from_orders` triggers reconciliation. Empty ledger; one call that overwrites filled totals to a complete unit on each side; assert `closed_count` populated.
- `sync_from_positions` triggers reconciliation on non-same-ticker. Empty ledger, same setup; assert populated.
- `sync_from_positions` no-ops on same-ticker (early return) without mutating `closed_*`. Existing same-ticker ledger with reconciled state; call `sync_from_positions`; assert no change.
- `seed_from_saved` triggers reconciliation. Empty ledger; call with saved totals representing one complete closed unit; assert `closed_*` populated from the saved blend.

### Integration — `tests/test_engine.py` or new `tests/test_ledger_reconstruction.py`

- **Cold-start, non-same-ticker, balanced fills.** Persisted state: A=10, B=10 at unit_size=5. On load, reconciliation closes 10 contracts per side; open bucket empty on both sides.
- **Cold-start, non-same-ticker, imbalanced fills.** Persisted state: A=10, B=5. On load, close 5 each; A open = 5, B open = 0.
- **Cold-start, same-ticker, persisted-only path.** Persisted state for a same-ticker ledger with A=15, B=10. On load, `seed_from_saved` runs reconciliation (closes 10 per side); `sync_from_positions` then early-returns; final state: A open = 5, B open = 0, `closed_count` = 10 per side. **This test proves the same-ticker migration path works.**
- `_reconcile_closed` emits the paper-trail log exactly once per non-idempotent invocation.

## Out of scope (flagged for follow-ups)

- `OpportunityProposer` Gate 6 tightening for fee-free markets
- Manual-sell / close-out handling in the ledger
- UI display of `fee_type` so a fee-free market is visually distinct from a fee-paying one
- Extending the ledger with per-fill FIFO so exact cost-basis accounting becomes possible (would also enable true unwind on cancellation)

## Rollout

This is a pure-logic fix with existing test coverage in place. No migration-gating or flag required. Land it, run the full suite, spot-check the decision log on a live event after deploy.
