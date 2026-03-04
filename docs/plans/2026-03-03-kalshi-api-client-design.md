# Kalshi API Client — Design

**Date:** 2026-03-03
**Status:** Approved
**Scope:** Full Kalshi REST + WebSocket client (Layer 1 of Talos architecture)

## Goal

Build the foundational API client that all other Talos layers depend on. Covers authentication, all REST endpoints needed for trading (markets, orders, portfolio), and WebSocket real-time feeds.

## Module Structure

```
src/talos/
├── __init__.py
├── config.py              # Environment config (base URLs, key paths, timeouts)
├── auth.py                # RSA-PSS signing + header generation
├── models/
│   ├── __init__.py        # Re-exports all models
│   ├── market.py          # Market, Event, Series, OrderBook, Trade
│   ├── order.py           # Order, Fill, BatchOrderResult
│   ├── portfolio.py       # Position, Balance, Settlement
│   └── ws.py              # WebSocket message types (snapshot, delta, ticker)
├── rest_client.py         # Async REST client (httpx)
└── ws_client.py           # WebSocket client (websockets)
```

## config.py

Loads from environment variables. Two profiles:

| Setting | Demo (default) | Production |
|---------|---------------|------------|
| REST base URL | `https://demo-api.kalshi.co/trade-api/v2` | `https://api.elections.kalshi.com/trade-api/v2` |
| WS URL | `wss://demo-api.kalshi.co/` | `wss://api.elections.kalshi.com/` |
| Key ID | from `KALSHI_KEY_ID` | from `KALSHI_KEY_ID` |
| Key path | from `KALSHI_PRIVATE_KEY_PATH` | from `KALSHI_PRIVATE_KEY_PATH` |
| Environment | `KALSHI_ENV=demo` | `KALSHI_ENV=production` |

Production must be explicitly opted into (Principle 2: Human in the Loop).

## auth.py — RSA-PSS Authentication

```python
class KalshiAuth:
    def __init__(self, key_id: str, private_key_path: Path): ...
    def headers(self, method: str, path: str) -> dict[str, str]: ...
```

- Loads RSA private key from PEM file once at init
- Signs: `timestamp_ms + method + path` (path has no query params)
- Algorithm: RSA-PSS, SHA-256, MGF1 with SHA-256, digest-length salt
- Returns: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`
- Timestamp is current time in milliseconds since epoch

## models/ — Pydantic v2 Models

### market.py

- `Market` — ticker, event_ticker, title, status, yes/no bid/ask, volume, open_interest, settlement fields, fee fields
- `Event` — event_ticker, series_ticker, title, category, status, markets list
- `Series` — series_ticker, title, category, tags
- `OrderBook` — yes_bids, no_bids as list of `[price_cents, count]`
- `Trade` — ticker, price, count, side, timestamp

### order.py

- `Order` — order_id, ticker, side, type, price, count, remaining_count, status, timestamps
- `Fill` — trade_id, order_id, ticker, side, price, count, timestamp
- `BatchOrderResult` — order_id, success, error

### portfolio.py

- `Balance` — balance_cents, portfolio_value_cents
- `Position` — ticker, position (signed int), total_traded, market_exposure
- `Settlement` — ticker, settlement_price, payout

### ws.py

- `OrderBookSnapshot` — market_ticker, yes/no price-quantity arrays, seq
- `OrderBookDelta` — market_ticker, side, price, delta, seq, timestamp
- `Ticker` — market_ticker, price data
- `WSSubscribed` — channel, sid
- `WSError` — code, message

All monetary values stored as `int` (cents). No floats for money.

## rest_client.py — Async REST Client

```python
class KalshiRESTClient:
    def __init__(self, auth: KalshiAuth, config: KalshiConfig): ...

    # Market data
    async def get_events(self, status=None, series_ticker=None, ...) -> list[Event]
    async def get_event(self, event_ticker: str, with_markets: bool = False) -> Event
    async def get_series(self, series_ticker: str) -> Series
    async def get_market(self, ticker: str) -> Market
    async def get_orderbook(self, ticker: str, depth: int = 0) -> OrderBook
    async def get_trades(self, ticker: str, ...) -> list[Trade]

    # Orders
    async def create_order(self, ticker, side, type, price, count, ...) -> Order
    async def cancel_order(self, order_id: str) -> Order
    async def amend_order(self, order_id: str, ...) -> Order
    async def batch_create_orders(self, orders: list[...]) -> list[BatchOrderResult]
    async def batch_cancel_orders(self, order_ids: list[str]) -> list[BatchOrderResult]
    async def get_orders(self, ticker=None, status=None, ...) -> list[Order]
    async def get_order(self, order_id: str) -> Order

    # Portfolio
    async def get_balance(self) -> Balance
    async def get_positions(self, ...) -> list[Position]
    async def get_fills(self, ...) -> list[Fill]

    # Exchange
    async def get_exchange_status(self) -> ExchangeStatus
```

Design choices:
- Built on `httpx.AsyncClient` with connection pooling
- Every method returns typed Pydantic models, never raw dicts
- Pagination: methods accept `limit`/`cursor` params; also offer async iterators for auto-pagination
- All responses logged at DEBUG level with full payload (Principle 13: Trust But Log)
- Auth headers injected per-request via `KalshiAuth.headers()`

### Error Hierarchy

```
KalshiError (base)
├── KalshiAuthError        — key/signature issues
├── KalshiAPIError         — non-2xx, includes status + body
├── KalshiRateLimitError   — 429, includes retry-after
└── KalshiConnectionError  — network/connection failures
```

All errors include the raw response body for debugging.

## ws_client.py — WebSocket Client

```python
class KalshiWSClient:
    def __init__(self, auth: KalshiAuth, config: KalshiConfig): ...

    async def connect(self) -> None
    async def disconnect(self) -> None

    async def subscribe(self, channel: str, market_ticker: str) -> int  # sid
    async def unsubscribe(self, sids: list[int]) -> None

    def on_message(self, channel: str, callback: Callable) -> None
```

Design choices:
- Auth via WebSocket handshake headers (same 3 headers as REST)
- Message IDs: auto-incrementing integers starting at 1
- Seq tracking per subscription for orderbook consistency; log warnings on gaps
- Keepalive: respond to server pings (every ~10s); reconnect if no ping in 30s
- Reconnection: automatic for read-only data channels; NOT automatic during active trading (Principle 1: Safety Above All)
- Callbacks receive parsed Pydantic models, not raw dicts

### Channels Used

| Channel | Type | Purpose |
|---------|------|---------|
| `orderbook_delta` | Market data | Real-time orderbook snapshots + deltas |
| `ticker` | Market data | Price ticker updates |
| `trade` | Market data | Public trade feed |
| `fill` | User-specific | Our fill confirmations |
| `user_orders` | User-specific | Our order status updates |

## Testing Strategy

Per Principle 3: Prove It Works — every behavior gets a test.

| Test file | What it tests |
|-----------|--------------|
| `test_auth.py` | Signing produces correct headers with known test vectors |
| `test_models.py` | Round-trip JSON parsing for every Pydantic model |
| `test_rest_client.py` | Mock httpx: URL construction, headers, model parsing, pagination, errors |
| `test_ws_client.py` | Mock WS: subscribe/unsub messages, keepalive, seq gaps, callback dispatch |

No live API integration tests in this step. Those come later with the TUI.

## Principles Applied

| Principle | How |
|-----------|-----|
| 1. Safety Above All | No auto-reconnect for order channels during trading |
| 2. Human in the Loop | Demo by default, production requires explicit env var |
| 3. Prove It Works | Full test coverage for every module |
| 4. Subtract Before You Add | Minimal layers, no premature abstractions |
| 6. Boring and Proven | httpx + websockets + Pydantic, standard patterns |
| 7. Audit Everything | All responses logged with full payload |
| 10. Correctness Over Speed | Seq validation over throughput |
| 12. Single Strategy | No generic strategy abstractions in the client |
| 13. Trust But Log | API responses trusted but fully logged |

## API Reference

- REST base: `https://api.elections.kalshi.com/trade-api/v2`
- WS: `wss://api.elections.kalshi.com/`
- Auth: RSA-PSS SHA-256 signing of `timestamp_ms + method + path`
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`
- All currency in cents (int), all timestamps in seconds or milliseconds
- Pagination: cursor-based with `limit` + `cursor` params

Sources:
- https://docs.kalshi.com/getting_started/api_keys
- https://docs.kalshi.com/websockets/websocket-connection
- https://docs.kalshi.com/websockets/orderbook-updates
- https://docs.kalshi.com/websockets/connection-keep-alive
