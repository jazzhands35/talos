# Phase 6 — Engine Actions

Back to [[plans/01-trading-engine-extraction/overview]]

## Goal

Move user-triggered actions from `TalosApp` to `TradingEngine`: order placement, game management, and bid adjustment approval/rejection.

## Changes

### `src/talos/engine.py`

Implement action methods (moved from `app.py`):
- `place_bids(bid: BidConfirmation)` — place NO orders on both legs via REST
- `add_games(urls: list[str])` — delegate to game_manager
- `remove_game(event_ticker: str)` — delegate to game_manager
- `clear_games()` — delegate to game_manager
- `approve_adjustment(event_ticker: str, side: str)` — verify proposal, execute amend via REST
- `reject_adjustment(event_ticker: str, side: str)` — clear proposal from adjuster

All methods emit notifications via `on_notification` callback for success/error feedback.

### `tests/test_engine.py`

Add tests for action methods:
- `place_bids` calls `rest_client.create_order` twice with correct params
- `add_games` delegates to `game_manager.add_games`
- `approve_adjustment` executes amend and clears proposal
- `reject_adjustment` clears proposal without API call
- Error paths: REST failure emits error notification

## Verification

### Static
- `pyright` passes
- `ruff check` passes

### Runtime
- `pytest tests/test_engine.py` — action tests pass
- Invoke `safety-audit` skill (order placement and bid adjustment are money-touching code)
