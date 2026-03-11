# Phase 2 — Ledger Display Formatter

Back to [[plans/01-trading-engine-extraction/overview]]

## Goal

Add a pure function `compute_display_positions()` to `position_ledger.py` that reads ledger state and produces `EventPositionSummary` for display. This is the replacement for `compute_event_positions()` — but both coexist during this phase.

## Changes

### `src/talos/position_ledger.py`

Add function:

```
compute_display_positions(
    ledgers: dict[str, PositionLedger],
    pairs: list[ArbPair],
    queue_cache: dict[str, int],
    cpm_tracker: CPMTracker,
) -> list[EventPositionSummary]
```

Logic: for each pair, read filled/resting/prices from the ledger (not from raw orders), compute matched pairs, locked profit (via `fee_adjusted_profit_matched`), exposure, and enrich with queue and CPM data.

Key types: `ArbPair`, `EventPositionSummary`, `LegSummary`, `CPMTracker` — all existing.

### `tests/test_position_ledger.py`

Add tests for `compute_display_positions()`:
- Empty ledger → empty list
- Both sides filled equally → matched pairs, locked profit computed
- One side ahead → unmatched count, exposure computed
- Queue and CPM enrichment applied correctly

## Verification

### Static
- `pyright` passes (new function has full type annotations)
- `ruff check` passes
- Existing `test_position_ledger.py` tests still pass

### Runtime
- New tests pass: `pytest tests/test_position_ledger.py -k compute_display`
- Old `compute_event_positions` tests still pass (it hasn't been removed yet)
