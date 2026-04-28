# DRIP/BLIP Redesign — Insertion-Strategy-Only Scope

**Date:** 2026-04-28
**Author:** Sean (with Claude)
**Supersedes:** [2026-04-26-drip-staggered-arb-redesign.md](2026-04-26-drip-staggered-arb-redesign.md) (the POC merged in PR #6)
**Status:** Spec — pending plan-writing

## Context

PR #6 shipped the first DRIP/BLIP POC. On 2026-04-28, soak-testing on a low-flow political market (KXTRUMP-…-PELO, talos id 143) exposed a fundamental scope mismatch with the operator's mental model.

**What the POC built:** DRIP toggling on an event hands the entire event lifecycle to a parallel controller (`DripController` + `_drive_drip` loop). Standard Talos behavior — jump-following, rebalancing, opportunity proposal generation, profitability re-checks, manual-bid acceptance — is gated off by `is_drip` early-returns scattered across `engine.py` and `opportunity_proposer.py`. The controller's only behaviors are seed (place a drip when resting count is zero), pair-completion replenish, and BLIP (cancel-replace ahead side when ETA gap > threshold).

**What the operator expected:** DRIP is purely a *placement strategy*. Standard Talos keeps managing the event end-to-end; toggling DRIP only changes how new contracts enter the book — fed in slowly with a per-side resting cap, plus a BLIP overlay to keep legs balanced during entry. Catch-up, exit, monitoring, and pricing all behave identically to a non-DRIP event.

**The frozen-row symptom on 2026-04-28:** Row 143 had 3 yes-resting + 5 no-resting (placed by the standard pipeline before DRIP was toggled), 1 yes filled, 0 no filled. After DRIP toggle, the standard pipeline stopped running on the event. DRIP's seed gate is `resting_count == 0`, so DRIP didn't add anything. DRIP's BLIP gate needed an ETA gap > 5 min, but per-side ETAs both fell back to the same 1m default on a market with low flow history. Result: every cycle was a `NoOp`, and the queue-bumped no-side just sat there frozen.

## The Four-Mode Mental Model

The operator's conceptual frame — captured in [memory: project_talos_modes_framework.md](../../../C--Users-Sean-Documents-Python-Talos/memory/project_talos_modes_framework.md) and worth restating for this spec:

Talos operates in four distinct modes across a ticker's lifecycle:

1. **Monitoring** — scanning, no orders yet
2. **Entry / Insertion** — placing initial orders
   - *Standard:* place full target as one order on each side
   - *DRIP:* drip in `drip_size` contracts at a time, max `drip_size × max_drips` resting per side, with BLIP overlay
3. **Catch-up** — recovering from fill imbalance
4. **Exit** — exit-only mode, unwind

DRIP is only a variant of mode 2. It does not change modes 1, 3, or 4.

The modes are not yet explicit in code (the eventual refactor — "Approach 3" below — is deferred). This spec restores the *behavioral* boundary by making DRIP a sizing parameter consumed by the standard pipeline, rather than a parallel pipeline.

## Design

### Section 1 — Architecture: DRIP becomes a strategy parameter

Every place in the engine that today asks "how many contracts can be resting on this side?" gets routed through a single helper that returns the strategy's `allowed_resting` *before* the catch-up exception:

```python
def per_side_max_ahead(self, event_ticker: str, side: Side) -> int:
    """Strategy-aware 'allowed resting' for one side, pre-catch-up.

    DRIP events return their absolute resting cap (drip_size × max_drips).
    Non-DRIP events return the standard 'room left in current unit' value.
    """
    drip = self._drip_events.get(event_ticker)
    if drip is not None:
        return drip.max_ahead_per_side
    ledger = self._adjuster.get_ledger(event_ticker)
    filled_in_unit = ledger.filled_count(side) % ledger.unit_size
    return max(0, ledger.unit_size - filled_in_unit)
```

The helper is the seam between strategies. Each strategy auto-derives its own value from its existing configuration — DRIP from `drip_size × max_drips`, the standard strategy from `unit_size - filled_in_unit`. No new global, no new user-facing input. Future strategies plug in by adding a new config object that exposes a `max_ahead_per_side` property.

The catch-up exception lives unchanged in the standard rebalancer ([rebalance.py:274](../../src/talos/rebalance.py)):

```python
allowed_resting = max(per_side_max_ahead(event, side), fill_gap)
```

Catch-up is mode 3, identical for every strategy. This `max(...)` is intentionally not duplicated into DRIP-specific code.

### Section 2 — Helper plumbing & call sites

The redesign hooks in by replacing `ledger.unit_size`-based math (the existing "max ahead" for the standard strategy) with the new helper at every site that computes a per-side target.

| Site | Today | After |
|---|---|---|
| [rebalance.py:274](../../src/talos/rebalance.py) `compute_unit_overcommit_proposal` | `unit_size - filled_in_unit` | `per_side_max_ahead(event, side)` (helper internalizes the subtraction for the standard case) |
| [rebalance.py:361](../../src/talos/rebalance.py) top-up qty in `compute_rebalance_proposal` | `unit_size - filled_in_unit` | `per_side_max_ahead(event, side)` |
| [bid_adjuster.py:762-767](../../src/talos/bid_adjuster.py) pre-place safety check | `filled_in_unit + new_count > unit_size` | `new_count > per_side_max_ahead(event, side)` (post-cancel state — `filled_in_unit` is already baked into the helper for standard, and DRIP caps `new_count` directly against `drip_cap`) |
| [engine.py:2439](../../src/talos/engine.py) reconciliation derivation | `auth_fills[side] % unit_size` | route through helper if it represents a per-side target; the implementation plan should verify whether this site is a true target computation or unrelated `unit_size` arithmetic |

Roughly 4–6 call sites. The expectation is that the implementation plan will identify any additional sites by grepping for `unit_size` in the rebalance/proposer/adjuster paths and routing each through the helper where it represents a per-side target (vs. an unrelated use of `unit_size`).

**Naming choices locked in:**

- Helper name: **`per_side_max_ahead(event_ticker, side)`**
- Property name on each strategy config: **`max_ahead_per_side`**
- `DripConfig.per_side_contract_cap` is renamed to **`max_ahead_per_side`** for cross-strategy consistency.
- No new `StandardStrategyConfig` class is introduced for now — the helper reads `unit_size` directly when DRIP is not enabled. A proper config class arrives with mode separation (Approach 3, deferred).

### Section 3 — Snap-to-cap on toggle

Pressing `d` (or restoring DRIP from saved state) is a hard transition: the next rebalance pass should bring the event into compliance with the new cap.

Implementation: `enable_drip` marks the event dirty:

```python
def enable_drip(self, event_ticker: str, config: DripConfig) -> bool:
    # ... existing setup ...
    self._drip_events[event_ticker] = config
    self._dirty_events.add(event_ticker)  # forces next rebalance cycle to evaluate
    return True
```

No special "shrink to cap" code path is written. The standard rebalancer's `compute_unit_overcommit_proposal` already cancels surplus resting orders down to a target. After Section 2's call-site swap, the proposal it generates uses the new helper, so the very next cycle sees "resting=5, allowed=1, cancel 4." The catch-up exception inside `max(deficit, max_ahead)` automatically protects in-progress catch-up.

Behavior under common scenarios:

| State at toggle | What happens next cycle |
|---|---|
| Balanced, 5 resting per side, drip_cap=1 | `allowed = max(0, 1) = 1`. Rebalancer cancels 4 surplus per side. |
| Imbalanced (A=3 fills, B=0 fills, 5 resting on B), drip_cap=1 | `allowed_B = max(3, 1) = 3`. Rebalancer cancels 2 surplus on B. A's resting follows standard catch-up rules. |
| Behind side has the right number of resting | Nothing to do; catch-up proceeds unchanged. |

### Section 4 — BLIP overlay (slimmed `_drive_drip`)

With seeding, replenishment, market-following, and rebalancing all flowing through the standard pipeline, the DRIP-specific loop shrinks to just BLIP evaluation.

The new shape (rough):

```python
async def _drive_drip(self, event_ticker: str) -> None:
    config = self._drip_events.get(event_ticker)
    pair = self.find_pair(event_ticker)
    if config is None or pair is None or not self._initial_sync_done:
        return
    eta_a, front_a = self._drip_eta_and_front(event_ticker, pair, Side.A)
    eta_b, front_b = self._drip_eta_and_front(event_ticker, pair, Side.B)
    action = evaluate_blip(config, eta_a_min=eta_a, eta_b_min=eta_b,
                           front_a_id=front_a, front_b_id=front_b)
    if isinstance(action, BlipAction):
        await self._execute_blip(event_ticker, pair, action)
```

**Removed:**

| Today | Why it's gone |
|---|---|
| `DripController` class | No longer tracks state. Fills hit the standard ledger; BLIP just reads ledger state on each cycle. Becomes a free function `evaluate_blip(config, …)` in `drip.py`. |
| `record_fill` + trade-id dedup in controller | Fills flow through `record_fill_from_ws` to the standard ledger (same as non-DRIP events). DRIP doesn't need a parallel fill-tracking path. |
| `_drip_pending_actions` queue | No multi-step sequenced actions to defer. BLIP's cancel-then-place runs inline in `_execute_blip`. |
| Seed logic at [engine.py:3107-3110](../../src/talos/engine.py) | Standard pipeline seeds. |
| Per-pair-completion replenishment | Standard pipeline replenishes (it sees `resting < target` and fills the gap). |

**Kept:**

| Stays | Why |
|---|---|
| 60s BLIP cooldown ([engine.py:78](../../src/talos/engine.py)) | Throttles thrash. Documented as known limitation; revisit when adding queue-movement gate. |
| Cancel-then-place at same price | Canonical "back of queue at this price level" via Kalshi FIFO. |
| `_drip_eta_and_front` ([engine.py:3124](../../src/talos/engine.py)) | Still feeds `evaluate_blip`. |

**Ordering note:** `_drive_drip` runs at the end of `refresh_account` ([engine.py:1724-1725](../../src/talos/engine.py)) — *after* the standard pipeline has done its work for the cycle. BLIP must not fire on an order the rebalancer is about to cancel.

### Section 5 — Deletion list

The redesign restores standard-pipeline behavior on DRIP events by removing the early-return gates that block it today.

#### Hard deletes

| Site | What it does today | Action |
|---|---|---|
| [opportunity_proposer.py:111-113](../../src/talos/opportunity_proposer.py) | Returns `None` with `block_drip` for any DRIP event | **Delete** the gate; proposer generates proposals as usual |
| [engine.py:2572](../../src/talos/engine.py) | Skips `_jump_internal` (top-of-market jumps) | **Delete** |
| [engine.py:2660](../../src/talos/engine.py) | Skips `_reevaluate_jumps_for` per-event | **Delete** |
| [engine.py:2714](../../src/talos/engine.py) | Skips `_check_imbalance_for` per-event | **Delete** |
| [engine.py:2787](../../src/talos/engine.py) | Skips `reevaluate_jumps` full-sweep | **Delete** |
| [engine.py:2871](../../src/talos/engine.py) | Skips `check_imbalances` poll-driven rebalance | **Delete** |
| [engine.py:3311](../../src/talos/engine.py) | `if exit_only or is_drip: continue` in proposal generation | **Drop the `or is_drip` clause** |
| [engine.py:3520-3526](../../src/talos/engine.py) | Manual-bid UI gate "DRIP owns this event" | **Delete** — standard pre-place safety check covers cap enforcement |

#### Kept as-is

- [engine.py:549-550](../../src/talos/engine.py) `is_drip()` method — still needed for sizing dispatch and UI
- [engine.py:5287](../../src/talos/engine.py), [engine.py:5419](../../src/talos/engine.py) — UI status string ("DRIP" label in the Status column). Cosmetic.
- 60s BLIP cooldown plumbing ([engine.py:78](../../src/talos/engine.py), [engine.py:181](../../src/talos/engine.py), [engine.py:656](../../src/talos/engine.py))

#### Restructured

| Site | Today | After |
|---|---|---|
| [engine.py:2023-2034](../../src/talos/engine.py) WS fill handler | Routes fill into `DripController.record_fill` for DRIP events | **Remove the DRIP branch entirely.** Fills hit the standard ledger via the existing `record_fill_from_ws` path. |
| [engine.py:3066](../../src/talos/engine.py) | Passes `drip=is_drip(event)` flag into proposer | **Remove the `drip` parameter from the proposer signature.** Proposer reads `engine.per_side_max_ahead(event)` for sizing. |
| [drip.py:84-128](../../src/talos/drip.py) `DripController` class | Tracks fills, emits replenish actions | **Replace with a free function** `evaluate_blip(config, …) -> Action`. |
| [drip.py:32-34](../../src/talos/drip.py) `per_side_contract_cap` property | DRIP-internal name | **Rename to `max_ahead_per_side`** |

#### Why deleting the manual-bid gate ([engine.py:3520](../../src/talos/engine.py)) is safe

That gate was added because "DRIP owns the event." Under the new model, DRIP doesn't own anything — it's just a sizing parameter. The standard pre-place safety check at [bid_adjuster.py:762-767](../../src/talos/bid_adjuster.py) already enforces `filled_in_unit + new_count <= max_ahead`, so a manual bid that would exceed the DRIP cap is rejected by the same safety code that protects standard events.

## Testing Strategy

### New tests

| Test | What it locks in |
|---|---|
| `per_side_max_ahead` returns DRIP cap for DRIP events, `unit_size` for non-DRIP | Dispatch correctness |
| `per_side_max_ahead` reflects updated `unit_size` when global is changed mid-session | No stale snapshot bugs |
| Snap-to-cap on toggle: balanced state with surplus → next rebalance cancels surplus down to cap | Section 3 behavior |
| Snap-to-cap on toggle: imbalanced state with behind-side surplus → catch-up exception preserves deficit count | Section 3 catch-up rule |
| Standard pipeline runs on DRIP events: `reevaluate_jumps`, `check_imbalances`, opportunity proposal all fire | Regression — proves the gate deletions worked |
| `evaluate_blip` as a free function with same input scenarios as today's controller tests | Section 4 behavior preservation |

### Existing tests to update

- [tests/test_drip_controller.py](../../tests/test_drip_controller.py) — `DripController` is gone; restructure to test `evaluate_blip` as a free function. State-tracking tests (filled counters, pair completion) are deleted with the class.
- [tests/test_drip_modal.py](../../tests/test_drip_modal.py) — modal UI unchanged; field-rename test (`per_side_contract_cap` → `max_ahead_per_side`) if any test asserts on the property name.
- Any test asserting `block_drip` behavior in the proposer → inverts to "proposer generates proposals for DRIP events."
- Any test asserting `is_drip` blocks `check_imbalances` / jump-follow → inverts.

## Known Limitations / Future Work

### High priority — BLIP queue-thrash on deep-queue / back-of-queue orders

Current implementation only throttles BLIP via the 60s cooldown. When ETA gap > threshold but the BLIP cancel-replace produces no meaningful queue movement, BLIP fires every 60s with zero effect. Two failure modes:

- Both sides deep in queue (e.g., position 499 of 501): cancel-replace nudges position from 499 to 501, but the gap is unchanged so BLIP fires again next cooldown window. Sustained API churn for negligible movement.
- Both sides at the very back of queue: cancel-replace puts you at the back (where you already are). Zero movement, zero effect, perpetual fire.

Possible mitigations to evaluate (none picked yet — the eventual fix may use a combination of factors):

- **Queue-movement gate** — only BLIP if there are ≥ N orders behind the current position at this price level
- **Position-delta gate** — track position before/after BLIP; suppress further BLIPs if last BLIP didn't appreciably change position, until book state changes
- **Top-N gate** — only BLIP if the ahead side is in the top X% of the queue (so going to back creates meaningful delay)
- **Other ETA / CPM-derived signals** — combine fill-rate forecasts, time-to-fill estimates, and queue depth into a single "is BLIP worth firing" decision

This work is gated on having queue-depth-at-price-level exposed in the tracker, which the current implementation doesn't compute. High priority because the symptom is wasted API calls on every quiet-market DRIP event with non-trivial ETA gap.

### Medium priority — `MAX_DRIPS=1` POC restriction

[engine.py:560-562](../../src/talos/engine.py) rejects `max_drips != 1`. The redesign doesn't depend on this restriction structurally — it can be lifted when ready.

### Low priority — BLIP cooldown is hardcoded

60s constant at [engine.py:78](../../src/talos/engine.py). Could move to `DripConfig` if tuning becomes useful.

### Long-term — Full mode separation (Approach 3)

Make Monitoring / Entry / Catch-up / Exit explicit modes in the codebase. The `per_side_max_ahead` helper is the natural seam for plugging in strategy interfaces — when this work lands, the helper becomes part of an `EntryStrategy` protocol; other modes are unchanged. Deferred per operator direction; the redesign in this spec is a stepping-stone, not a substitute.
