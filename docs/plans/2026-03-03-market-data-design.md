# Layer 2: Market Data — Design Document

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:writing-plans after this design is approved.

**Goal:** Maintain real-time local orderbook state for subscribed Kalshi markets, enabling higher layers to query best bid/ask and detect staleness.

**Architecture:** Two-module split — a pure state machine (`OrderBookManager`) with no I/O, and an async orchestrator (`MarketFeed`) that owns the WebSocket subscription lifecycle and routes messages to the state machine.

**Tech Stack:** Python 3.12+, Pydantic v2, structlog, websockets (via existing `KalshiWSClient`)

---

## Design Decisions

- **Cross-event pairs** — the arbitrage strategy targets markets from different events in the same series (NO+NO), not same-event markets
- **Manual subscription** — higher layers tell `MarketFeed` which tickers to watch; no auto-discovery at this layer
- **No reconnection logic** — `MarketFeed` propagates WS errors upward; reconnection is a higher-layer concern
- **Server sends fresh snapshot on resubscribe** — so reconnection is simple: just resubscribe and the book rebuilds from scratch
- **Pure state + async orchestrator split** — `OrderBookManager` is trivially testable (no mocks needed for core logic), `MarketFeed` tests mock the WS layer

## Module Structure

### `src/talos/orderbook.py` — Pure State Machine

No I/O, no async. Maintains local orderbook per market ticker, applies snapshots and deltas, answers queries.

```python
class LocalOrderBook(BaseModel):
    ticker: str
    yes: list[OrderBookLevel]    # sorted by price descending (best first)
    no: list[OrderBookLevel]     # sorted by price descending (best first)
    last_seq: int                # last applied sequence number
    snapshot_time: str           # ISO 8601 of last snapshot
    stale: bool = False          # True if seq gap detected

class OrderBookManager:
    def apply_snapshot(self, ticker: str, snapshot: OrderBookSnapshot) -> None:
        """Replace entire book for a ticker. Resets seq tracking."""

    def apply_delta(self, ticker: str, delta: OrderBookDelta) -> None:
        """Apply incremental update. Warns on seq gaps."""

    def get_book(self, ticker: str) -> LocalOrderBook | None:
        """Get current book state, or None if not tracked."""

    def best_bid(self, ticker: str) -> OrderBookLevel | None:
        """Highest yes bid price."""

    def best_ask(self, ticker: str) -> OrderBookLevel | None:
        """Lowest yes ask price."""

    def remove(self, ticker: str) -> None:
        """Stop tracking a ticker."""

    @property
    def tickers(self) -> set[str]:
        """All currently tracked tickers."""
```

**Key behaviors:**
- `apply_snapshot` replaces the full book, resets `last_seq`, clears `stale`
- `apply_delta` checks `seq == last_seq + 1` — if gap, sets `stale = True` and logs warning
- Levels sorted by price descending so `best_bid` / `best_ask` are O(1) index lookups
- Delta application: price with qty=0 means remove level, otherwise upsert

### `src/talos/market_feed.py` — Async Orchestrator

Owns WebSocket subscription lifecycle, routes messages to `OrderBookManager`.

```python
class MarketFeed:
    def __init__(
        self,
        ws_client: KalshiWSClient,
        book_manager: OrderBookManager,
    ) -> None: ...

    async def subscribe(self, ticker: str) -> None:
        """Subscribe to orderbook updates for a ticker."""

    async def unsubscribe(self, ticker: str) -> None:
        """Unsubscribe and remove from book manager."""

    async def start(self) -> None:
        """Begin listening. Routes snapshots/deltas to book_manager."""

    async def stop(self) -> None:
        """Unsubscribe all, stop listening."""

    @property
    def subscriptions(self) -> set[str]:
        """Currently subscribed tickers."""
```

**Key behaviors:**
- `subscribe` calls `ws_client.subscribe("orderbook_delta", [ticker])` and registers a callback
- Callback parses `OrderBookSnapshot` or `OrderBookDelta` and calls `book_manager.apply_*`
- `start` kicks off WS listen loop (delegates to `ws_client.listen()`)
- `stop` unsubscribes all tickers, then disconnects
- Structured logging on every subscribe/unsubscribe/snapshot/delta

## Data Flow

```
MarketFeed.subscribe("KXBTC-26MAR-T50000")
  → ws_client.subscribe("orderbook_delta", ["KXBTC-26MAR-T50000"])
  → Kalshi sends OrderBookSnapshot
  → MarketFeed callback → book_manager.apply_snapshot()
  → Kalshi sends OrderBookDelta (seq 2, 3, 4...)
  → MarketFeed callback → book_manager.apply_delta()

Higher layer queries:
  book_manager.get_book("KXBTC-26MAR-T50000")
  book_manager.best_bid("KXBTC-26MAR-T50000")
```

## Error Handling

- `OrderBookManager`: no exceptions — logs warnings on seq gaps, sets `stale = True`
- `MarketFeed`: catches WS errors, logs with structlog, propagates upward
- Invalid/malformed messages: logged and skipped (no crash)
- Delta for unknown ticker (no snapshot yet): logged and ignored

## Testing Strategy

### `tests/test_orderbook.py`
- Apply snapshot → verify book state
- Apply sequential deltas → verify level updates
- Seq gap detection → verify `stale` flag
- Remove level (qty=0) → verify level gone
- `best_bid` / `best_ask` accuracy
- Unknown ticker delta → ignored gracefully
- `remove()` → ticker gone from `tickers`

### `tests/test_market_feed.py`
- Mock `KalshiWSClient` + `OrderBookManager`
- Subscribe → verify WS subscribe called + callback registered
- Unsubscribe → verify WS unsubscribe + `book_manager.remove`
- Snapshot callback → verify `apply_snapshot` called
- Delta callback → verify `apply_delta` called
- Stop → verify all tickers unsubscribed

## Principles Applied

- **Subtract Before You Add** — two focused modules, no extras
- **Prove It Works** — pure state machine is trivially testable without mocks
- **Trust But Log** — WS messages are trusted but every operation is logged
- **Safety Above All** — staleness detection prevents acting on stale data
- **Correctness Over Speed** — sorted levels and seq checking over raw performance
