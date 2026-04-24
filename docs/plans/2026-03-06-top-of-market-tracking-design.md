# Top-of-Market Tracking Design

## Problem

When resting NO bids get "penny jumped" (someone posts a higher NO bid that takes fill priority), the user has no way to know from within Talos. They must switch to the Kalshi app to check.

## Solution

A `TopOfMarketTracker` pure state machine that compares resting order prices against the live orderbook best price, fires callbacks on state transitions, and surfaces alerts in the TUI.

## Data Model

`TopOfMarketTracker` — pure state, no async, no I/O.

**State:**
- `_resting: dict[str, int]` — `{market_ticker: no_price}` for our highest resting NO bid on each ticker
- `_at_top: dict[str, bool]` — `{market_ticker: is_at_top}` current state per ticker

**Inputs:**
- `update_orders(orders, pairs)` — called from 10s order poll. Filters to resting NO buys on tracked tickers. Uses highest resting price when multiple orders exist on the same ticker. Clears tickers with no resting orders.
- `check(ticker)` — called on every WS orderbook delta. Compares `_resting[ticker]` against `OrderBookManager.best_ask(ticker).price`. Fires callback only on state transitions.

**Outputs:**
- `on_change: Callable[[str, bool], None] | None` — callback fired with `(ticker, is_at_top)` on transitions
- `is_at_top(ticker) -> bool | None` — query for table refresh. `None` = no resting orders.
- `resting_price(ticker) -> int | None` — query for toast message context.

## Wiring

**Construction:** Created in `__main__.py` with reference to same `OrderBookManager`. Injected into `TalosApp` as optional dependency.

**Real-time path (WS delta -> check):**
```
WS delta -> MarketFeed -> OrderBookManager.apply_delta()
                        -> on_book_update callback
                        -> scanner.scan(ticker)    [existing]
                        -> tracker.check(ticker)   [new]
```

`on_book_update` is currently a single callback. Wire a dispatcher in `__main__.py`:
```python
def on_book_update(ticker: str) -> None:
    scanner.scan(ticker)
    tracker.check(ticker)
```

**Order poll path (10s -> update tracker):**
```
TalosApp.refresh_account() -> rest.get_orders()
                            -> tracker.update_orders(orders, scanner.pairs)
```

Order data is 10s stale, but price movement detection is real-time once orders are loaded.

**Alert path (tracker -> TUI):**
```
tracker.on_change(ticker, is_at_top)
  -> TalosApp callback:
      - toast: "TICKER-A: jumped (you: 47c, top: 48c)" [warning]
      - or:    "TICKER-A: back at top (47c)" [information]
      - store state for table refresh
```

## Visual Indicators

**Toast notification** (one-time on state transition):
- Lost top: severity `warning`, includes your price and the new top price
- Regained top: severity `information`

**Table indicators** (persistent, 500ms refresh):
- Q-A / Q-B column: prefix with warning symbol when not at top of market
- No resting orders: stays "---"
- At top: no change to existing display

## Edge Cases

- No resting orders on a ticker: `is_at_top` returns `None`, no indicator, no alert
- Same price level but behind in queue: still "at top" (queue position handles intra-level priority)
- Order fully filled: `update_orders` removes it, no spurious alerts
- Market data before first order poll: `_resting` empty, `check()` is no-op
- Multiple orders same ticker at different prices: use highest (closest to top)

## Testing

Pure unit tests in `tests/test_top_of_market.py`:

1. Basic detection: resting at 47, book top at 48 -> `is_at_top` False
2. At top: resting at 47, book top at 47 -> `is_at_top` True
3. Callback on transition: was at top, book moves -> fires `(ticker, False)`
4. No duplicate callbacks: state unchanged -> no fire
5. Multiple orders same ticker: uses highest price
6. Order removed: ticker cleared, no alerts
7. No resting orders: `check()` no-op
8. Regain top: was jumped, market returns -> fires `(ticker, True)`
9. Integration: `TalosApp` shows warning symbol in Q column
