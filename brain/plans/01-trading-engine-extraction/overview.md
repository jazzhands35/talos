# Trading Engine Extraction — Plan

Back to [[plans/index]]

## Context

`TalosApp` is both the UI layer and the application logic layer (490 lines, fan-out of 7). As Talos grows, this god class will become the bottleneck for every new feature. Additionally, two systems derive position state from orders independently (`compute_event_positions()` and `PositionLedger`), creating a dual source of truth.

Design doc: `docs/plans/2026-03-08-trading-engine-extraction-design.md`

## Scope

**In scope:**
1. Extract `TradingEngine` from `TalosApp`
2. Unify position computation (PositionLedger + formatter replaces `compute_event_positions`)
3. Fix BidAdjuster encapsulation (`_sides` access → public accessors)
4. Move queue cache logic to engine

**Out of scope:**
- New features or behavior changes
- Changing the `models/` package structure
- Refactoring scanner, orderbook, or feed internals

## Constraints

- All existing tests must pass after each phase (or be rewritten to test equivalent behavior)
- Test mode (`TalosApp(scanner=scanner)`) must continue working for widget tests
- The callback wiring pattern (already proven in `MarketFeed`, `BidAdjuster`) is used for engine→UI communication
- Pyright and ruff must pass clean after each phase

## Applicable Skills

- `test-runner` — after each phase
- `lint-check` — after each phase
- `safety-audit` — after phases touching position or bid adjustment code
- `position-scenarios` — after position unification phase

## Phases

Order: encapsulation fix first (zero test impact), then position unification (isolated), then engine extraction (largest), then cleanup.

1. [[plans/01-trading-engine-extraction/phase-1-encapsulation-fix]]
2. [[plans/01-trading-engine-extraction/phase-2-ledger-display-formatter]]
3. [[plans/01-trading-engine-extraction/phase-3-position-unification]]
4. [[plans/01-trading-engine-extraction/phase-4-engine-scaffold]]
5. [[plans/01-trading-engine-extraction/phase-5-engine-polling]]
6. [[plans/01-trading-engine-extraction/phase-6-engine-actions]]
7. [[plans/01-trading-engine-extraction/phase-7-slim-app]]
8. [[plans/01-trading-engine-extraction/phase-8-wiring-and-cleanup]]

## Verification

After every phase:
```bash
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check src/ tests/
.venv/Scripts/python -m pyright
```

After phase 8 (final):
- Manual launch: `python -m talos` — verify dashboard loads, games can be added, orders display correctly
- Verify `TalosApp` is ~150 lines, `TradingEngine` is ~300 lines
- Verify `position.py` is deleted and no imports reference it
