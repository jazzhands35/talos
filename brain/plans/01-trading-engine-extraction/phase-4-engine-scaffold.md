# Phase 4 — Engine Scaffold

Back to [[plans/01-trading-engine-extraction/overview]]

## Goal

Create `TradingEngine` class with constructor, dependency injection, and callback infrastructure. No logic moved yet — just the skeleton.

## Changes

### `src/talos/engine.py` (new)

Create `TradingEngine` class with:
- Constructor accepting all subsystem dependencies (scanner, game_manager, rest_client, market_feed, tracker, adjuster, initial_games)
- Internal state: `_queue_cache`, `_orders_cache`, `_cpm`
- Callback attributes: `on_notification`, `on_proposal`
- Empty method stubs (or `pass` bodies) for: `start_feed()`, `refresh_account()`, `refresh_queue_positions()`, `refresh_trades()`, `place_bids()`, `add_games()`, `remove_game()`, `clear_games()`, `approve_adjustment()`, `reject_adjustment()`
- Read-only properties: `scanner`, `tracker`, `adjuster`, `orders`, `position_summaries`, `balance`

### `tests/test_engine.py` (new)

Test that `TradingEngine` can be constructed with dependencies and that properties return expected defaults.

## Verification

### Static
- `pyright` passes (all type annotations present)
- `ruff check` passes
- No existing tests affected (nothing imports engine.py yet)

### Runtime
- `pytest tests/test_engine.py` — scaffold tests pass
