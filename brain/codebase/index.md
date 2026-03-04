# Codebase Knowledge

## Module Map

| Module | Purpose | Key Classes |
|--------|---------|-------------|
| `config.py` | Environment config from env vars | `KalshiConfig`, `KalshiEnvironment` |
| `auth.py` | RSA-PSS request signing | `KalshiAuth` |
| `errors.py` | Typed exception hierarchy | `KalshiError`, `KalshiAPIError`, `KalshiRateLimitError`, `KalshiAuthError`, `KalshiConnectionError` |
| `models/market.py` | Market data models | `Market`, `Event`, `Series`, `OrderBook`, `OrderBookLevel`, `Trade` |
| `models/order.py` | Order models | `Order`, `Fill`, `BatchOrderResult` |
| `models/portfolio.py` | Portfolio models | `Balance`, `Position`, `Settlement`, `ExchangeStatus` |
| `models/ws.py` | WebSocket messages | `OrderBookSnapshot`, `OrderBookDelta`, `TickerMessage`, `TradeMessage`, `WSSubscribed`, `WSError` |
| `rest_client.py` | Async REST client | `KalshiRESTClient` |
| `ws_client.py` | WebSocket client | `KalshiWSClient` |
| `orderbook.py` | Local orderbook state management | `LocalOrderBook`, `OrderBookManager` |
| `market_feed.py` | WS subscription orchestrator | `MarketFeed` |
| `models/strategy.py` | Strategy data models | `ArbPair`, `Opportunity` |
| `scanner.py` | NO+NO arbitrage detection | `ArbitrageScanner` |
| `game_manager.py` | Game lifecycle from URLs | `GameManager`, `parse_kalshi_url` |

## Gotchas

- **OrderBook raw arrays:** The Kalshi API returns orderbook levels as `[[price, qty], ...]` arrays. The `OrderBook` model uses a `model_validator(mode="before")` to convert these to `OrderBookLevel` objects before Pydantic validation. Using `model_post_init` doesn't work because Pydantic v2 validates types before post-init runs.
- **Path on Windows:** `Path("/tmp/test.pem")` renders differently when stringified on Windows vs Unix. Compare `Path` objects directly, not their string representations.
- **Auth signs path only:** The RSA-PSS signature covers `timestamp + method + path` — query parameters must be stripped before signing.
- **WS message IDs must be integers:** Starting at 1, auto-incrementing. Setting `id` to 0 is treated as absent by Kalshi.
- **Seq gaps in WS:** If orderbook delta `seq` numbers have gaps, the local orderbook state may be inconsistent. The client logs a warning but continues — reconnection logic should be handled at a higher layer.
- **WS callback kwargs:** Callbacks receive `(parsed, sid=int, seq=int)` as keyword args. The `sid` is used by `MarketFeed` to track ticker-to-subscription mappings for unsubscribe.
- **best_ask returns NO side:** `OrderBookManager.best_ask()` returns the top NO level. The implied YES ask price is `100 - level.price`. Conversion is left to the strategy layer.
- **NO pricing uses YES bids:** To get the NO ask price (cheapest you can buy NO), use `100 - best_bid(ticker).price`. The `best_bid()` method returns the top of the YES side. `raw_edge = best_bid_a + best_bid_b - 100`.
- **Game events have exactly 2 markets:** Each game event on Kalshi has one contract per team. `GameManager.add_game()` validates this and raises `ValueError` if not.
- **structlog `event` is reserved:** Use `event_ticker=` instead of `event=` in structlog calls. The `event` parameter is structlog's reserved name for the log message itself.
