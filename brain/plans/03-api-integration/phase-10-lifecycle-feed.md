# Phase 10 — Market Lifecycle WebSocket Channel

Back to [[plans/03-api-integration/overview]]

## Goal

Subscribe to `market_lifecycle_v2` for real-time notifications when markets are determined (result known), settled (cash distributed), paused, or created. Enables auto-detection of settlements and market pauses.

## Changes

### src/talos/lifecycle_feed.py (NEW)
- `LifecycleFeed` class:
  - Constructor takes `KalshiWSClient`, registers callback for `market_lifecycle_v2` channel
  - Global subscription (no market filter — all lifecycle events)
  - Callbacks by event type:
    - `on_determined: Callable[[str, str, int], None] | None` — (ticker, result, settlement_value_cents)
    - `on_settled: Callable[[str], None] | None` — (ticker)
    - `on_paused: Callable[[str, bool], None] | None` — (ticker, is_deactivated)
    - `on_created: Callable[[str, dict], None] | None` — (ticker, metadata) for new market discovery
  - `subscribe()` — single global subscription
  - Routes `MarketLifecycleMessage` to appropriate callback based on `event_type`

### src/talos/engine.py
- Accept `lifecycle_feed: LifecycleFeed | None` in constructor
- Wire callbacks:
  - `on_determined`: log result, trigger settlement fetch (Phase 6) for the event, notify UI
  - `on_settled`: remove game from monitoring (or flag as settled), notify UI with P&L
  - `on_paused`: flag market as paused — prevent order placement on paused markets, notify UI
  - `on_created`: log for awareness (auto-add deferred to future plan)
- Add `_paused_markets: set[str]` — markets currently paused
- In `place_bids()` and `amend_order()` paths: check `_paused_markets` before sending orders

### src/talos/__main__.py
- Create `LifecycleFeed(ws_client)` and pass to engine
- Subscribe during `start_feed()`

## Data Structures

- `LifecycleFeed`: owns `_ws`, `_sid: int | None`, four optional callbacks
- Engine gains `_paused_markets: set[str]`
- Uses `MarketLifecycleMessage` from Phase 1

## Verification

### Static
- `pyright` passes
- `ruff` passes

### Runtime
- Unit test: fire `determined` event, verify `on_determined` callback with correct args
- Unit test: fire `deactivated` event with `is_deactivated=true`, verify market added to `_paused_markets`
- Unit test: fire `deactivated` with `is_deactivated=false` (unpause), verify market removed from set
- Unit test: `place_bids()` refuses to place on paused market
- Unit test: `settled` event triggers settlement fetch
- Manual test: wait for a market to settle in demo, verify lifecycle events appear in logs
