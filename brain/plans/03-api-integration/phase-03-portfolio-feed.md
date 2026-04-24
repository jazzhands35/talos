# Phase 3 — PortfolioFeed Handler

Back to [[plans/03-api-integration/overview]]

## Goal

Create `PortfolioFeed` — a new async orchestrator (following the MarketFeed pattern) that manages subscriptions to `user_orders` and `fill` WS channels. Pure handler with callbacks — no business logic.

## Changes

### src/talos/portfolio_feed.py (NEW)
- `PortfolioFeed` class following the MarketFeed pattern:
  - Constructor takes `KalshiWSClient`, registers callbacks for `user_orders` and `fill` channels
  - `on_order_update: Callable[[UserOrderMessage], None] | None` — fired on every user_order message
  - `on_fill: Callable[[FillMessage], None] | None` — fired on every fill message
  - `subscribe(tickers: list[str] | None = None)` — subscribes to both channels. `None` = all markets (global subscription)
  - `add_markets(tickers: list[str])` — uses `update_subscription` to add tickers to existing subscriptions
  - `remove_markets(tickers: list[str])` — uses `update_subscription` to remove tickers
  - Internal tracking: `_order_sid: int | None`, `_fill_sid: int | None` for managing subscriptions
- No state accumulation — just message routing. State lives in PositionLedger (wired in Phase 4)

### src/talos/ws_client.py
- Ensure `_dispatch` handles `user_order` and `fill` message types (registered in Phase 1 model registry)
- Note: `user_orders` channel sends messages with type `user_order` (singular). `fill` channel sends type `fill`. The channel name vs message type distinction matters

## Data Structures

- `PortfolioFeed`: owns `_ws: KalshiWSClient`, `_order_sid: int | None`, `_fill_sid: int | None`
- Callbacks are optional (None-safe), matching MarketFeed pattern

## Verification

### Static
- `pyright` passes
- `ruff` passes

### Runtime
- Unit test: construct PortfolioFeed with mock WS client, fire a `UserOrderMessage`, verify `on_order_update` callback fires
- Unit test: fire a `FillMessage`, verify `on_fill` callback fires
- Unit test: `subscribe()` sends correct WS commands for both channels
- Unit test: `add_markets()` sends `update_subscription` with `add_markets` action
- Unit test: messages with no registered callback are silently ignored
