# Phase 11 — Market Model Enrichment and Bulk Subscribe

Back to [[plans/03-api-integration/overview]]

## Goal

Capture `settlement_ts` and `fractional_trading_enabled` from Market responses. Implement bulk WS subscribe using `market_tickers` array for faster startup.

## Changes

### src/talos/models/market.py
- Add to `Market` model:
  - `settlement_ts: str | None = None` — when the market settles
  - `close_time: str | None = None` — when the market closes
  - `open_time: str | None = None` — when the market opened
  - `fractional_trading_enabled: bool = False`
  - `market_type: str = "binary"` — binary vs scalar
  - `result: str = ""` — settlement outcome if determined
  - `is_provisional: bool = False` — may be removed if inactive

### src/talos/ws_client.py
- Modify `_build_subscribe()` to accept `market_tickers: list[str] | None`:
  - If single ticker: use `market_ticker` (singular) — existing behavior
  - If multiple tickers: use `market_tickers` (plural array)
- Subscriptions are idempotent since Sep 2025 — duplicate tickers are handled server-side

### src/talos/market_feed.py
- Add `subscribe_bulk(tickers: list[str])` method:
  - Single WS command for all tickers instead of N individual subscribes
  - Still track in `_subscribed_tickers` set
  - SID learning happens from first message per ticker (unchanged)
- Update `GameManager` or engine to use bulk subscribe on startup when adding multiple games

### UI surfacing
- Show time-to-settlement in OpportunitiesTable (e.g., "2h 15m", "settled", "closed")
- Flag `fractional_trading_enabled` markets if encountered (informational)

## Data Structures

- `Market` gains: `settlement_ts`, `close_time`, `open_time`, `fractional_trading_enabled`, `market_type`, `result`, `is_provisional`
- `_build_subscribe()` gains `market_tickers` parameter

## Verification

### Static
- `pyright` passes
- `ruff` passes

### Runtime
- Unit test: `Market` model parses `settlement_ts` and `fractional_trading_enabled` from API response
- Unit test: `_build_subscribe(market_tickers=["A", "B"])` uses plural key
- Unit test: `subscribe_bulk` sends single command
- Unit test: existing single-ticker subscribe still works (backward compat)
- Manual test: start Talos, verify settlement time appears in table
