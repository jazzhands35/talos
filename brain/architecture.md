# Architecture

Talos is a Kalshi arbitrage trading system designed for progressive automation.

## Design Philosophy

**Manual-first:** The system starts with full manual control over trade decisions. Automation is added incrementally as confidence grows. The human is always able to override or intervene.

## Layers

1. **API Client** (Layer 1) — **COMPLETE**
   - `config.py` — environment config (demo/production), loaded from env vars
   - `auth.py` — RSA-PSS request signing (SHA-256, MGF1)
   - `errors.py` — typed exception hierarchy (auth, API, rate limit, connection)
   - `models/` — Pydantic v2 models for all API objects (market, order, portfolio, WS messages)
   - `rest_client.py` — async httpx client, all REST endpoints (markets, orders, portfolio, exchange)
   - `ws_client.py` — WebSocket client (subscribe, dispatch, seq tracking, keepalive)

2. **Market Data** (Layer 2) — **COMPLETE**
   - `orderbook.py` — pure state machine: `LocalOrderBook` model, `OrderBookManager` (apply snapshot/delta, seq tracking, staleness)
   - `market_feed.py` — async orchestrator: subscribes to markets via WS, routes snapshots/deltas to book manager
3. **Strategy Engine** — identifies arbitrage opportunities
4. **Execution** — places and manages orders
5. **UI (Textual TUI)** — dashboard for monitoring and manual control
6. **Automation** — progressively takes over decision-making from the human

## Key Technical Decisions

- **Demo by default** — production requires `KALSHI_ENV=production`
- **All money as int (cents)** — no floats for currency
- **Async-first** — httpx.AsyncClient, websockets, all I/O awaited
- **Pydantic v2** — all API responses are typed models, never raw dicts
- **Structured logging** — structlog with key-value pairs on all API calls
- **Trust but log** — API responses are trusted but fully logged at DEBUG level

## API Reference

- REST: `https://api.elections.kalshi.com/trade-api/v2` (prod) / `https://demo-api.kalshi.co/trade-api/v2` (demo)
- WS: `wss://api.elections.kalshi.com/` (prod) / `wss://demo-api.kalshi.co/` (demo)
- Auth: RSA-PSS SHA-256 signing of `timestamp_ms + method + path`
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`
