# Phase 7 — EventPosition Enrichment and min_close_ts Filter

Back to [[plans/03-api-integration/overview]]

## Goal

Capture rich fields from EventPosition responses (Kalshi's live P&L per event) and add `min_close_ts` filtering to `GET /events` to avoid trading soon-to-expire or already-active events.

## Changes

### src/talos/models/portfolio.py
- Enrich `EventPosition` model with fields currently dropped:
  - `total_cost: int = 0` (cents, from `total_cost_dollars`)
  - `total_cost_shares: int = 0` (contract count, from `total_cost_shares_fp`)
  - `event_exposure: int = 0` (cents, from `event_exposure_dollars`)
  - `realized_pnl: int = 0` (cents, from `realized_pnl_dollars`)
  - `resting_orders_count: int = 0`
  - `fees_paid: int = 0` (cents, from `fees_paid_dollars`)
  - Add `_migrate_fp` validator for all new fields

### src/talos/rest_client.py
- Add `min_close_ts: int | None = None` parameter to `get_events()`
  - Include in params dict when provided
  - This filters out events where all markets close before the given timestamp
- Expose `get_event_positions()` return values — currently only extracts `event_ticker`, now returns full `EventPosition` with rich fields

### src/talos/engine.py
- In `_discover_active_events()`: use `min_close_ts` set to current time + configurable buffer (e.g., 2 hours) when discovering new events
- Store rich `EventPosition` data for UI access — `_event_positions: dict[str, EventPosition]`
- Surface `realized_pnl` from EventPosition in position summaries

### src/talos/automation_config.py
- Add `min_close_buffer_seconds: int = 7200` — minimum time-to-close for new event discovery (default 2 hours)

## Data Structures

- `EventPosition` gains: `total_cost`, `total_cost_shares`, `event_exposure`, `realized_pnl`, `resting_orders_count`, `fees_paid` (all ints, cents or counts)

## Verification

### Static
- `pyright` passes
- `ruff` passes

### Runtime
- Unit test: `EventPosition` model parses all new `_dollars`/`_fp` fields correctly
- Unit test: `get_events(min_close_ts=X)` includes parameter in request
- Unit test: `_discover_active_events` applies min_close_ts buffer
- Unit test: backward compat — `EventPosition` with only `event_ticker` still works
- Manual test: start Talos, verify position display includes Kalshi's realized_pnl where available
