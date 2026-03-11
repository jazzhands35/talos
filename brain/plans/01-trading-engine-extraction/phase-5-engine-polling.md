# Phase 5 — Engine Polling Logic

Back to [[plans/01-trading-engine-extraction/overview]]

## Goal

Move polling logic from `TalosApp` to `TradingEngine`. This is the largest single move — `refresh_account()`, `refresh_queue_positions()`, `refresh_trades()`, queue cache management, ledger sync, and position summary computation.

## Changes

### `src/talos/engine.py`

Implement the polling methods (moved from `app.py`):
- `refresh_account()` — fetch balance + orders, update tracker, sync ledgers, compute display positions, build order data dicts. Emits notifications via callback on error.
- `refresh_queue_positions()` — fast-cadence queue poll with conservative merge. Uses `_merge_queue()` (moved from app.py module level).
- `refresh_trades()` — trade ingestion for CPM tracking.
- `start_feed()` — WS connect, game restore, listen loop.
- `_active_market_tickers()` — helper moved from app.
- `_merge_queue()` — helper moved from app module level.
- Top-of-market callback handler (`_on_top_of_market_change`) — evaluates jumps via adjuster.

State exposed after polling: `balance`, `orders`, `order_data`, `position_summaries`.

### `tests/test_engine.py`

Add tests for polling methods:
- `refresh_account` fetches balance and orders, produces position summaries
- `refresh_queue_positions` merges queue data conservatively
- `refresh_trades` ingests trades into CPM tracker
- Queue cache prune removes inactive orders
- Ledger sync is called for each registered pair

Use `AsyncMock` for REST client, real `ArbitrageScanner` + `PositionLedger` for state.

## Verification

### Static
- `pyright` passes
- `ruff check` passes

### Runtime
- `pytest tests/test_engine.py` — new polling tests pass
- Existing tests still pass (app.py still has its own copies at this point — dual implementation is temporary)
