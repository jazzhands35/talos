# Phase 8 — Leaner Polling with event_ticker Filter

Back to [[plans/03-api-integration/overview]]

## Goal

Use the `event_ticker` filter on `GET /portfolio/orders` to fetch only orders for monitored events instead of all 200. Reduces response size, parsing time, and API load — especially valuable now that real-time data comes via WS.

## Changes

### src/talos/rest_client.py
- Add `event_ticker: str | None = None` parameter to `get_orders()`
  - Supports comma-separated values (since Oct 2025): `"KXMLB-NYY-26,KXNBA-LAL-26"`
  - Include in params dict when provided

### src/talos/engine.py
- In `refresh_account()`:
  - Build comma-separated event_ticker string from monitored pairs: `",".join(pair.event_ticker for pair in self._scanner.pairs)`
  - Pass to `get_orders(event_ticker=..., limit=200)`
  - This targets the reconciliation poll to only relevant events
- Keep the unfilterable global order fetch as a fallback for `_proposal_queue.tick(active_order_ids=...)` — this needs all active order IDs
  - Option A: Use WS `user_orders` (from Phase 4) for global order awareness, REST filtered for reconciliation
  - Option B: Periodically (every N cycles) do an unfiltered fetch for proposal queue housekeeping
  - Decision: Option A is cleaner — WS gives real-time order state for all orders, REST poll only needs to reconcile monitored events

### Consideration
- After Phase 4 (PortfolioFeed), `_orders_cache` is maintained by both WS updates and REST polling
- The proposal queue's `tick(active_order_ids=...)` can use WS-maintained state
- REST polling becomes a targeted reconciliation mechanism, not the primary data source

## Data Structures

No new types. `get_orders()` gains an optional `event_ticker` parameter.

## Verification

### Static
- `pyright` passes
- `ruff` passes

### Runtime
- Unit test: `get_orders(event_ticker="EVT-A,EVT-B")` includes event_ticker in request params
- Unit test: engine builds correct comma-separated event_ticker string from monitored pairs
- Unit test: proposal queue tick still works with WS-maintained order state
- Manual test: verify Talos functions correctly with filtered polling — orders for non-monitored events still appear via WS
