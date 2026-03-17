# Phase 2 — Thread through ArbPair and GameManager

Back to [[plans/05-expiration-start-time/overview]]

## Goal

Make `expected_expiration_time` available on `ArbPair` so downstream consumers (GameStatusResolver, engine) can use it. Thread the value from Market → GameManager → ArbPair → Scanner.

## Changes

**`src/talos/models/strategy.py`** — Add `expected_expiration_time: str | None = None` to `ArbPair`.

**`src/talos/game_manager.py`** — In `add_game()`, extract `expected_expiration_time` from the fetched markets (take the min of the two sides, same as `close_time` extraction). Pass to `ArbPair` constructor.

**`src/talos/scanner.py`** — Accept `expected_expiration_time` in `add_pair()` and store it on the `ArbPair`. No changes needed to `Opportunity` — start time is computed downstream, not stored per-snapshot.

**Tests** — Verify that `ArbPair` round-trips the field, and that `add_game()` extracts it from markets.

## Data Structures

- `ArbPair.expected_expiration_time: str | None` — ISO 8601 string, carried from Market

## Verification

### Static
- `pyright` passes — new field has correct type
- `ruff check` clean

### Runtime
- Test: `ArbPair(expected_expiration_time="2026-03-19T04:30:00Z", ...)` stores correctly
- Test: `GameManager.add_game()` with mock markets containing `expected_expiration_time` threads it to the returned `ArbPair`
- Test: `Scanner.add_pair()` stores the value on the pair
