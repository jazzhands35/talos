# Phase 1 — Parse `expected_expiration_time` into Market model

Back to [[plans/05-expiration-start-time/overview]]

## Goal

Add `expected_expiration_time` as an optional field on the `Market` Pydantic model so it's parsed from every Kalshi Market API response at the boundary (P14).

## Changes

**`src/talos/models/market.py`** — Add `expected_expiration_time: str | None = None` to the `Market` model. No validator needed — it's an ISO 8601 string that Kalshi provides as-is.

**`tests/test_models_strategy.py`** (or new `tests/test_market_model.py`) — Test that `Market` parses a dict containing `expected_expiration_time` and stores it, and that missing/null values default to `None`.

## Data Structures

- `Market.expected_expiration_time: str | None` — raw ISO 8601 timestamp string from Kalshi (e.g., `"2026-03-19T04:30:00Z"`)

## Verification

### Static
- `pyright` passes — new field has correct type annotation
- `ruff check` clean

### Runtime
- Test: `Market(**{"ticker": "T", ..., "expected_expiration_time": "2026-03-19T04:30:00Z"})` parses correctly
- Test: `Market(**{"ticker": "T", ...})` (field absent) defaults to `None`
- Test: existing Market tests still pass (no regression)
