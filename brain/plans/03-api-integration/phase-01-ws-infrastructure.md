# Phase 1 — WebSocket Infrastructure Upgrades

Back to [[plans/03-api-integration/overview]]

## Goal

Upgrade the WS client to support multiple channel types, the `update_subscription` command, and automatic sequence gap recovery. This is the foundation for all subsequent WS phases.

## Changes

### src/talos/models/ws.py
- Add new Pydantic models for WS messages that will arrive from new channels:
  - `UserOrderMessage` — maps to `user_order` type from `user_orders` channel
  - `FillMessage` — maps to `fill` type from `fill` channel (distinct from REST `Fill` model — different field set)
  - `MarketPositionMessage` — maps to `market_position` type from `market_positions` channel
  - `MarketLifecycleMessage` — maps to `market_lifecycle_v2` type
- Each model gets a `_migrate_fp` validator following the existing pattern (convert `_dollars` to cents, `_fp` to int)

### src/talos/ws_client.py
- Expand `_MESSAGE_MODELS` registry with all new message types
- Add `_build_update_subscription()` method — constructs `update_subscription` command with `add_markets`/`delete_markets` action
- Add `update_subscription(sid, tickers, action)` public method
- Add `_build_list_subscriptions()` and `list_subscriptions()` for debugging
- **Sequence gap recovery:** In `_dispatch()`, when a gap is detected, instead of just logging, call a new `on_seq_gap` callback. The callback (wired by MarketFeed or engine) triggers resubscribe for the affected ticker

### src/talos/market_feed.py
- Wire `on_seq_gap` callback to trigger `unsubscribe` + `subscribe` for the stale ticker (immediate recovery instead of waiting for polling cycle)

## Data Structures

- `UserOrderMessage`: `order_id`, `ticker`, `status`, `side`, `is_yes`, `yes_price` (cents), `fill_count` (int), `remaining_count` (int), `initial_count` (int), `maker_fill_cost` (cents), `taker_fill_cost` (cents), `maker_fees` (cents), `taker_fees` (cents), `client_order_id`, `created_time`, `last_update_time`
- `FillMessage`: `trade_id`, `order_id`, `market_ticker`, `is_taker`, `side`, `action`, `yes_price` (cents), `count` (int), `fee_cost` (cents), `post_position` (int, signed), `purchased_side`, `ts`
- `MarketPositionMessage`: `market_ticker`, `position` (int), `position_cost` (cents), `realized_pnl` (cents), `fees_paid` (cents), `volume` (int)
- `MarketLifecycleMessage`: `event_type`, `market_ticker`, `result`, `settlement_value` (cents), `is_deactivated`, `close_ts`, `settled_ts`

## Verification

### Static
- `pyright` passes — all new models type-check
- `ruff` passes
- No changes to existing behavior — purely additive

### Runtime
- Unit tests for each new model: construct from raw dict mimicking WS payload, verify validators convert correctly
- Unit test for `_build_update_subscription` — verify command structure
- Unit test for seq gap callback — mock the callback, feed a gap, verify callback fires with correct ticker
- Integration test: existing orderbook snapshot/delta tests still pass (no regression)
