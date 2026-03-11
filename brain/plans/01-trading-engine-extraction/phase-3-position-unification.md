# Phase 3 — Position Unification

Back to [[plans/01-trading-engine-extraction/overview]]

## Goal

Switch all callers from `compute_event_positions()` to `compute_display_positions()`. Delete `position.py`. Rewrite `test_position.py` to test the new function.

## Changes

### `src/talos/ui/app.py`

Replace `compute_event_positions` import and both call sites (`refresh_account`, `refresh_queue_positions`) with `compute_display_positions`. The new function takes ledgers + pairs + queue_cache + cpm_tracker instead of raw orders + pairs.

Note: CPM enrichment (`_enrich_with_cpm`) is absorbed into `compute_display_positions`, so the separate enrichment step is removed.

### `src/talos/position.py`

Delete this file entirely.

### `tests/test_position.py`

Rewrite to test `compute_display_positions()`. Tests construct `PositionLedger` instances with `record_fill()` / `record_resting()` instead of building raw `Order` objects. Same scenarios covered: empty, both matched, one ahead, multiple fills, queue positions.

## Data Structures

No new types. `EventPositionSummary` and `LegSummary` are unchanged in `models/position.py`.

## Verification

### Static
- `pyright` passes (no remaining imports of `talos.position`)
- `ruff check` passes
- `grep -r "from talos.position import" src/` returns zero results
- `grep -r "from talos.position import" tests/` returns zero results

### Runtime
- `pytest tests/test_position.py` — rewritten tests pass
- `pytest tests/test_ui.py` — existing UI tests pass
- Invoke `position-scenarios` skill to verify behavioral correctness
- Invoke `safety-audit` skill since this touches position tracking
