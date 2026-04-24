# Phase 4 — Wire PortfolioFeed into Engine

Back to [[plans/03-api-integration/overview]]

## Goal

Connect PortfolioFeed to the TradingEngine so that real-time order updates and fills flow into the PositionLedger. This is where the 10-second sync gap gets eliminated — fills update the ledger within milliseconds instead of waiting for the next polling cycle.

## Changes

### src/talos/engine.py
- Accept `portfolio_feed: PortfolioFeed` in constructor (optional, for backward compat with tests)
- Add `_on_order_update(msg: UserOrderMessage)` handler:
  - Update `_orders_cache` — find order by `order_id`, update fill_count/remaining_count/status/fees/fill_cost
  - If order not in cache (new order placed externally), add it
  - If order status changed to `executed` or `canceled`, update ledger via existing `sync_from_orders` flow
  - Trigger `TopOfMarketTracker.update_orders()` with updated cache
  - Notify UI if a fill occurred (toast: "Filled 3 @ 45¢ on KXMLB-NYY-25")
- Add `_on_fill(msg: FillMessage)` handler:
  - Cross-check `post_position` (from FillMessage) against ledger's current fill count for the affected side
  - Log any discrepancy between WS-reported position and ledger state
  - Update `_cpm` (contracts per minute) tracker
  - This is supplementary to `_on_order_update` — the order update already captures aggregate fills, the fill message provides per-trade detail and the authoritative `post_position`

### src/talos/__main__.py
- Create `PortfolioFeed(ws_client)` during component assembly
- Wire callbacks: `portfolio_feed.on_order_update = engine._on_order_update`
- Wire callbacks: `portfolio_feed.on_fill = engine._on_fill`
- Call `portfolio_feed.subscribe()` (global — all markets) during `start_feed()`
- Call `portfolio_feed.add_markets()` when new games are added

### src/talos/engine.py — start_feed()
- After WS connect, subscribe PortfolioFeed globally (no market filter — we want all order updates)
- When adding games, call `portfolio_feed.add_markets()` if using market-filtered subscription

### Reconciliation strategy
- WS updates are **optimistic** — they update the ledger cache immediately
- REST polling continues on its 10s cycle as **reconciliation** — catches anything WS missed
- `sync_from_orders` remains the authoritative reconciliation path (P7/P15)
- WS fills that arrive between polls will be confirmed (or corrected) on the next poll
- No data source can decrease fill counts (monotonic update rule preserved)

## Data Structures

- Engine gains: `_portfolio_feed: PortfolioFeed | None`
- No new state — updates flow into existing `_orders_cache` and through existing `sync_from_orders`

## Verification

### Static
- `pyright` passes
- `ruff` passes

### Runtime
- Unit test: mock PortfolioFeed, fire UserOrderMessage with fill_count increase, verify ledger updates
- Unit test: fire FillMessage, verify post_position cross-check logs correctly
- Unit test: engine without PortfolioFeed still works (backward compat)
- Unit test: WS fill followed by REST poll — verify monotonic rule (REST doesn't decrease WS-set fills)
- Manual smoke test: place bid in demo, watch for WS fill notification vs polling-based detection
- Invoke `safety-audit` skill — this phase touches order placement paths
- Invoke `position-scenarios` skill — verify fill tracking scenarios with WS + REST
