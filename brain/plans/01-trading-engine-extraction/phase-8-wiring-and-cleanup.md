# Phase 8 — Wiring and Cleanup

Back to [[plans/01-trading-engine-extraction/overview]]

## Goal

Update `__main__.py` to construct `TradingEngine` and pass it to `TalosApp`. Update brain vault. Final verification.

## Changes

### `src/talos/__main__.py`

Update `main()`:
- Construct `TradingEngine` with all subsystem dependencies
- Pass `engine` to `TalosApp` instead of individual dependencies
- Remove direct callback wiring that's now handled by the engine

### Brain vault updates

- `brain/architecture.md` — update layer descriptions, note engine extraction
- `brain/codebase/index.md` — add `engine.py` to module map, remove `position.py`
- `brain/decisions.md` — add decision record for engine extraction and position unification
- `brain/patterns.md` — update "TUI dependency injection" pattern to reflect engine-based DI

### Cleanup

- Delete `src/talos/position.py` if not already deleted in phase 3
- Verify no stale imports remain: `grep -r "from talos.position import" src/ tests/`
- Verify no `_sides` access remains: `grep -r "_sides" src/talos/bid_adjuster.py`

## Verification

### Static
- `pyright` passes
- `ruff check` passes
- No dead imports or unused files

### Runtime
- `pytest` — full suite passes
- Manual launch: `python -m talos` — dashboard loads, add a game, verify opportunities table populates, verify order panel works
- Invoke `safety-audit` skill — final audit of all money-touching code paths
- Invoke `position-scenarios` skill — final scenario walkthrough
