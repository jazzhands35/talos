# Codebase Knowledge

## Module Map

| Module | Purpose | Key Classes |
|--------|---------|-------------|
| `config.py` | Environment config from env vars | `KalshiConfig`, `KalshiEnvironment` |
| `auth.py` | RSA-PSS request signing | `KalshiAuth` |
| `errors.py` | Typed exception hierarchy | `KalshiError`, `KalshiAPIError`, `KalshiRateLimitError`, `KalshiAuthError`, `KalshiConnectionError` |
| `models/market.py` | Market data models | `Market`, `Event`, `Series`, `OrderBook`, `OrderBookLevel`, `Trade` |
| `models/order.py` | Order models | `Order` (incl. `queue_position`), `Fill`, `BatchOrderResult` |
| `models/portfolio.py` | Portfolio models | `Balance`, `Position`, `Settlement`, `ExchangeStatus` |
| `models/ws.py` | WebSocket messages | `OrderBookSnapshot`, `OrderBookDelta`, `TickerMessage`, `TradeMessage`, `WSSubscribed`, `WSError` |
| `rest_client.py` | Async REST client | `KalshiRESTClient` |
| `ws_client.py` | WebSocket client | `KalshiWSClient` |
| `orderbook.py` | Local orderbook state management | `LocalOrderBook`, `OrderBookManager` |
| `market_feed.py` | WS subscription orchestrator | `MarketFeed` |
| `models/strategy.py` | Strategy data models | `ArbPair`, `Opportunity`, `BidConfirmation` |
| `models/position.py` | Position tracking models | `LegSummary`, `EventPositionSummary` |
| `models/adjustment.py` | Bid adjustment proposal model | `ProposedAdjustment` |
| `position_ledger.py` | Per-event position state machine (fills, resting, safety gates, display) | `PositionLedger`, `Side`, `compute_display_positions` |
| `bid_adjuster.py` | Async orchestrator for bid adjustment on jumps | `BidAdjuster` |
| `engine.py` | Central trading orchestrator (polling, actions, caches) | `TradingEngine` |
| `fees.py` | Pure fee calculations (maker fee, American odds, scenario P&L) | `fee_adjusted_edge`, `scenario_pnl`, `american_from_win_risk` |
| `cpm.py` | Contracts-per-minute and ETA formatting | `format_cpm`, `format_eta` |
| `scanner.py` | NO+NO arbitrage detection | `ArbitrageScanner` |
| `game_manager.py` | Game lifecycle from URLs | `GameManager`, `parse_kalshi_url` |
| `persistence.py` | Save/load game list to `games.json` | `load_saved_games`, `save_games` |
| `top_of_market.py` | Top-of-market detection for resting NO bids | `TopOfMarketTracker` |
| `ui/theme.py` | Catppuccin Mocha colors + TCSS | Color constants, `APP_CSS` |
| `ui/widgets.py` | Dashboard widgets | `OpportunitiesTable`, `AccountPanel`, `OrderLog` |
| `ui/screens.py` | Modal dialogs | `AddGamesScreen`, `BidScreen` |
| `ui/app.py` | Thin UI shell (delegates to TradingEngine) | `TalosApp` |
| `__main__.py` | Entry point | `python -m talos` |

## Gotchas

- **OrderBook raw arrays:** The Kalshi API returns orderbook levels as `[[price, qty], ...]` arrays. The `OrderBook` model uses a `model_validator(mode="before")` to convert these to `OrderBookLevel` objects before Pydantic validation. Using `model_post_init` doesn't work because Pydantic v2 validates types before post-init runs.
- **Auth signs path only:** The RSA-PSS signature covers `timestamp + method + path` — query parameters must be stripped before signing.
- **Seq gaps in WS:** If orderbook delta `seq` numbers have gaps, the local orderbook state may be inconsistent. The client logs a warning but continues — reconnection logic should be handled at a higher layer.
- **WS callback kwargs:** Callbacks receive `(parsed, sid=int, seq=int)` as keyword args. The `sid` is used by `MarketFeed` to track ticker-to-subscription mappings for unsubscribe.
- **best_ask returns NO side:** `OrderBookManager.best_ask()` returns the top NO level. The implied YES ask price is `100 - level.price`. Conversion is left to the strategy layer.
- **NO pricing uses YES bids:** To get the NO ask price (cheapest you can buy NO), use `100 - best_bid(ticker).price`. The `best_bid()` method returns the top of the YES side. `raw_edge = best_bid_a + best_bid_b - 100`.
- **Game events have exactly 2 markets:** Each game event on Kalshi has one contract per team. `GameManager.add_game()` validates this and raises `ValueError` if not.
- **Kalshi `queue_position` on orders is deprecated:** `GET /portfolio/orders` always returns `queue_position: 0`. Use the dedicated `GET /portfolio/orders/queue_positions` batch endpoint. **This endpoint requires `market_tickers` or `event_ticker` param** — omitting both returns 400 `"Need to specify market_tickers or event_ticker"`, silently swallowed by try/except. Response key varies across API versions — check `queue_positions`, `data`, `results`. Prefer `queue_position_fp` over `queue_position` (int), but **`queue_position_fp` is a STRING** (e.g., `"2835.00"`) — must `float(fp)` before comparison. **Zero means "no data", not "front of queue"** — only cache and display positive values.
- **Kalshi Trade response field names differ from docs:** API returns `taker_side` (not `side`) and `price` as a dollar float (e.g., `0.52`) not cents int. Also provides `yes_price`/`no_price` as ints in cents. The `Trade` model uses a `model_validator(mode="before")` to normalize both formats. Always verify Pydantic models against actual API responses, not just docs — mock-based tests can't catch schema drift.
- **structlog `event` is reserved:** Use `event_ticker=` instead of `event=` in structlog calls. The `event` parameter is structlog's reserved name for the log message itself.
- **Textual table refresh:** Poll `scanner.opportunities` every 500ms via `set_interval(0.5, callback)` — don't refresh on every WS delta (10-50/sec). Use `add_column(label, width=N)` individually — `add_columns()` silently truncates wider values.
- **Textual test mode:** Inject dependencies via `TalosApp(scanner=scanner)`. In headless mode, `pilot.click("#id")` is unreliable inside modals — use keyboard interaction instead.
- **Textual ModalScreen escape:** No default escape binding. Add `BINDINGS = [("escape", "cancel", "Cancel")]` and `action_cancel` to every modal.
- **WS startup ordering for game restore:** In `_start_feed`, games must be restored AFTER `feed.connect()` (subscribe sends WS messages) but BEFORE `feed.start()` (which blocks in a listen loop). The restore goes between connect and start. Subscribe is a send-only operation; the server queues responses until the listen loop picks them up. See [[decisions#2026-03-06 — Game persistence: tickers only, re-fetch on startup]].
- **Net position display has three states.** Compute both scenario P&Ls via `scenario_pnl(filled_a, total_cost_a, filled_b, total_cost_b)` using **exact total costs** (not per-contract averages — integer division truncation compounds at scale). Then branch: (1) **Both positive** → guaranteed profit, show `GTD $X.XX` where X is the worst-case profit. (2) **Mixed signs** → directional bet, show `$X [side] [odds]` in sports-betting style (base wager = loss if positive odds, win if negative odds). (3) **Both negative** → underwater, show `-$X.XX`. Never feed two positive P&Ls into `american_from_win_risk` — it treats the smaller profit as "risk" and produces misleading odds like +100 for a guaranteed arb. See [[patterns#Financial calculation precision]].
- **Don't gate UI actions on volatile data:** Row click handlers should not check `raw_edge > 0` before opening a modal — edge fluctuates in live markets, causing silent no-ops. Always allow the action; let the modal show current state and the user decide.
