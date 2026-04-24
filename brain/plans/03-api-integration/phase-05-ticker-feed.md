# Phase 5 — TickerFeed Handler

Back to [[plans/03-api-integration/overview]]

## Goal

Subscribe to the `ticker` WS channel for real-time volume, open interest, last trade price, and BBA updates. Create a TickerFeed handler and surface the data in the UI.

## Changes

### src/talos/ticker_feed.py (NEW)
- `TickerFeed` class following MarketFeed pattern:
  - Constructor takes `KalshiWSClient`, registers callback for `ticker` channel
  - `on_ticker: Callable[[TickerMessage], None] | None` — fired on each ticker update
  - `subscribe(tickers: list[str])` — subscribes with `send_initial_snapshot: true` and `skip_ticker_ack: true`
  - `add_markets(tickers: list[str])` / `remove_markets(tickers: list[str])` — uses `update_subscription`
  - `_latest: dict[str, TickerMessage]` — cache of latest ticker data per market (for UI polling)
  - `get_ticker(ticker: str) -> TickerMessage | None` — query latest state

### src/talos/ws_client.py
- Add `skip_ticker_ack` support to subscribe params (only for ticker channel)
- Add `send_initial_snapshot` support to subscribe params

### src/talos/models/ws.py
- Add `open_interest: int | None = None` to existing `TickerMessage` (from `open_interest_fp`)
- Add `dollar_volume: int | None = None` and `dollar_open_interest: int | None = None` fields
- Note: `TickerMessage` model already exists and handles `_dollars` migration — just needs the missing fields

### src/talos/engine.py
- Accept `ticker_feed: TickerFeed | None` in constructor
- Expose `get_ticker_data(ticker: str) -> TickerMessage | None` for UI access
- Wire `ticker_feed.subscribe()` in `start_feed()` alongside orderbook subscriptions

### src/talos/__main__.py
- Create `TickerFeed(ws_client)` and pass to engine
- Wire subscription alongside market feed subscriptions in game_manager

### UI surfacing
- Add volume and OI columns to OpportunitiesTable (or enrich existing columns)
- Display last trade price alongside orderbook-derived BBA

## Data Structures

- `TickerFeed`: owns `_ws`, `_latest: dict[str, TickerMessage]`, `_sid: int | None`
- `TickerMessage` gains: `open_interest: int | None`, `dollar_volume: int | None`, `dollar_open_interest: int | None`

## Verification

### Static
- `pyright` passes
- `ruff` passes

### Runtime
- Unit test: construct TickerFeed with mock WS, fire TickerMessage, verify `_latest` cache updates
- Unit test: `get_ticker()` returns latest data, returns None for unknown ticker
- Unit test: subscribe sends correct params including `skip_ticker_ack` and `send_initial_snapshot`
- Unit test: TickerMessage model parses `open_interest_fp`, `dollar_volume` correctly
- Manual smoke test: start Talos, add game, verify volume/OI appear in table
