# Phase 1 — Fix BidAdjuster Encapsulation

Back to [[plans/01-trading-engine-extraction/overview]]

## Goal

Replace all direct `ledger._sides[side]` accesses in `BidAdjuster` with public accessor methods on `PositionLedger`. Zero test impact — all needed accessors already exist.

## Changes

### `src/talos/bid_adjuster.py`

Replace 4 locations where `ledger._sides[side]` is accessed:

- `execute()` method: reads `s.filled_count`, `s.resting_count` → use `ledger.filled_count(side)`, `ledger.resting_count(side)`
- `_check_post_cancel_safety()`: reads `s.filled_count`, `s.resting_count` from both sides, plus `other.filled_total_cost`, `other.resting_price` → use public accessors
- `_format_position_after()`: reads `s.filled_count` → use `ledger.filled_count(side)`

No new types or data structures needed. Public accessors already exist at `position_ledger.py:74-87`.

## Verification

### Static
- `pyright` passes clean (no type errors introduced)
- `ruff check` passes
- No `_sides` references remain in `bid_adjuster.py`

### Runtime
- `pytest tests/test_bid_adjuster.py` — all 13 tests pass unchanged
- `pytest tests/test_position_ledger.py` — all tests pass (no changes to ledger)
