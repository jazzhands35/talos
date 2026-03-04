# Layer 2: Market Data — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Maintain real-time local orderbook state for subscribed Kalshi markets via WebSocket, enabling higher layers to query best bid/ask and detect staleness.

**Architecture:** Two-module split — `OrderBookManager` (pure state machine, no I/O) manages per-ticker orderbook levels, while `MarketFeed` (async orchestrator) owns WebSocket subscriptions and routes messages. A small modification to `KalshiWSClient` is needed first to pass `sid` and `seq` metadata through to callbacks.

**Tech Stack:** Python 3.12+, Pydantic v2, structlog, existing `KalshiWSClient` + `OrderBookSnapshot`/`OrderBookDelta` models

---

### Task 1: Modify WS client to pass sid and seq to callbacks

The existing `_dispatch` method strips `sid` and `seq` before calling the registered callback. `MarketFeed` needs both: `sid` to map tickers for unsubscribe, `seq` to detect staleness in the orderbook.

**Files:**
- Modify: `src/talos/ws_client.py:152`
- Modify: `tests/test_ws_client.py`

**Step 1: Write the failing test**

Add a new test to `tests/test_ws_client.py` in the `TestMessageDispatch` class:

```python
async def test_passes_sid_and_seq_to_callback(self, client: KalshiWSClient) -> None:
    callback = AsyncMock()
    client.on_message("orderbook_delta", callback)
    client._sid_to_channel[1] = "orderbook_delta"

    raw: dict[str, Any] = {
        "type": "orderbook_snapshot",
        "sid": 1,
        "seq": 3,
        "msg": {
            "market_ticker": "KXBTC-26MAR-T50000",
            "market_id": "uuid-123",
            "yes": [[65, 100]],
            "no": [[35, 50]],
        },
    }
    await client._dispatch(raw)
    callback.assert_called_once()
    _, kwargs = callback.call_args
    assert kwargs["sid"] == 1
    assert kwargs["seq"] == 3
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_ws_client.py::TestMessageDispatch::test_passes_sid_and_seq_to_callback -v`
Expected: FAIL — callback is called without keyword args

**Step 3: Modify the WS client**

In `src/talos/ws_client.py`, change line 152 from:

```python
            await callback(parsed)
```

To:

```python
            await callback(parsed, sid=sid, seq=seq or 0)
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_ws_client.py -v`
Expected: ALL PASS (existing tests use `assert_called_once()` which doesn't check args, so they still pass)

**Step 5: Commit**

```bash
git add src/talos/ws_client.py tests/test_ws_client.py
git commit -m "feat: pass sid and seq to WS callbacks for orderbook tracking"
```

---

### Task 2: Create OrderBookManager with LocalOrderBook and apply_snapshot

The `OrderBookManager` is a pure state machine — no I/O, no async. It maintains per-ticker orderbook state and answers queries. This task creates the file, the `LocalOrderBook` model, and the `apply_snapshot` method.

**Files:**
- Create: `src/talos/orderbook.py`
- Create: `tests/test_orderbook.py`

**Step 1: Write the failing tests**

Create `tests/test_orderbook.py`:

```python
"""Tests for OrderBookManager."""

from __future__ import annotations

import pytest

from talos.models.market import OrderBookLevel
from talos.models.ws import OrderBookSnapshot
from talos.orderbook import LocalOrderBook, OrderBookManager


class TestLocalOrderBookModel:
    def test_defaults(self) -> None:
        book = LocalOrderBook(ticker="MKT-1")
        assert book.ticker == "MKT-1"
        assert book.yes == []
        assert book.no == []
        assert book.last_seq == 0
        assert book.stale is False


class TestApplySnapshot:
    @pytest.fixture()
    def manager(self) -> OrderBookManager:
        return OrderBookManager()

    def test_creates_book_from_snapshot(self, manager: OrderBookManager) -> None:
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[65, 100], [60, 200]],
            no=[[35, 150], [40, 50]],
        )
        manager.apply_snapshot("MKT-1", snapshot)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert book.ticker == "MKT-1"
        assert len(book.yes) == 2
        assert len(book.no) == 2

    def test_sorts_levels_descending_by_price(self, manager: OrderBookManager) -> None:
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[60, 200], [65, 100]],
            no=[[40, 50], [35, 150]],
        )
        manager.apply_snapshot("MKT-1", snapshot)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert book.yes[0].price == 65
        assert book.yes[1].price == 60
        assert book.no[0].price == 40
        assert book.no[1].price == 35

    def test_snapshot_replaces_existing_book(self, manager: OrderBookManager) -> None:
        snap1 = OrderBookSnapshot(
            market_ticker="MKT-1", market_id="uuid-1",
            yes=[[65, 100]], no=[[35, 50]],
        )
        snap2 = OrderBookSnapshot(
            market_ticker="MKT-1", market_id="uuid-1",
            yes=[[70, 300]], no=[[30, 200]],
        )
        manager.apply_snapshot("MKT-1", snap1)
        manager.apply_snapshot("MKT-1", snap2)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert len(book.yes) == 1
        assert book.yes[0].price == 70

    def test_snapshot_resets_stale_flag(self, manager: OrderBookManager) -> None:
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1", market_id="uuid-1",
            yes=[[65, 100]], no=[],
        )
        manager.apply_snapshot("MKT-1", snapshot)
        book = manager.get_book("MKT-1")
        assert book is not None
        book.stale = True  # Simulate a previous seq gap
        manager.apply_snapshot("MKT-1", snapshot)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert book.stale is False

    def test_snapshot_resets_last_seq(self, manager: OrderBookManager) -> None:
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1", market_id="uuid-1",
            yes=[], no=[],
        )
        manager.apply_snapshot("MKT-1", snapshot)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert book.last_seq == 0
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_orderbook.py -v`
Expected: FAIL — `talos.orderbook` does not exist

**Step 3: Write minimal implementation**

Create `src/talos/orderbook.py`:

```python
"""Local orderbook state management for subscribed markets."""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from talos.models.market import OrderBookLevel
from talos.models.ws import OrderBookSnapshot

logger = structlog.get_logger()


class LocalOrderBook(BaseModel):
    """Local state for a single market's orderbook."""

    ticker: str
    yes: list[OrderBookLevel] = []
    no: list[OrderBookLevel] = []
    last_seq: int = 0
    stale: bool = False


class OrderBookManager:
    """Maintains local orderbook state for multiple markets.

    Pure state machine — no I/O, no async. Receives snapshots and deltas,
    maintains sorted level lists, and answers queries.
    """

    def __init__(self) -> None:
        self._books: dict[str, LocalOrderBook] = {}

    def apply_snapshot(self, ticker: str, snapshot: OrderBookSnapshot) -> None:
        """Replace entire book for a ticker. Resets seq and stale flag."""
        yes_levels = sorted(
            [OrderBookLevel(price=p, quantity=q) for p, q in snapshot.yes],
            key=lambda lvl: lvl.price,
            reverse=True,
        )
        no_levels = sorted(
            [OrderBookLevel(price=p, quantity=q) for p, q in snapshot.no],
            key=lambda lvl: lvl.price,
            reverse=True,
        )
        self._books[ticker] = LocalOrderBook(
            ticker=ticker,
            yes=yes_levels,
            no=no_levels,
            last_seq=0,
            stale=False,
        )
        logger.debug(
            "orderbook_snapshot",
            ticker=ticker,
            yes_levels=len(yes_levels),
            no_levels=len(no_levels),
        )

    def get_book(self, ticker: str) -> LocalOrderBook | None:
        """Get current book state, or None if not tracked."""
        return self._books.get(ticker)
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_orderbook.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/talos/orderbook.py tests/test_orderbook.py
git commit -m "feat: add OrderBookManager with LocalOrderBook and apply_snapshot"
```

---

### Task 3: Add apply_delta with seq tracking

Adds incremental orderbook updates. A delta specifies a `price`, `delta` (new quantity at that price), and `side` ("yes" or "no"). If `delta == 0`, the level is removed. Seq tracking detects gaps and marks the book as stale.

**Files:**
- Modify: `src/talos/orderbook.py`
- Modify: `tests/test_orderbook.py`

**Step 1: Write the failing tests**

Add to `tests/test_orderbook.py`:

```python
from talos.models.ws import OrderBookDelta


class TestApplyDelta:
    @pytest.fixture()
    def manager(self) -> OrderBookManager:
        mgr = OrderBookManager()
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1", market_id="uuid-1",
            yes=[[65, 100], [60, 200]], no=[[35, 150]],
        )
        mgr.apply_snapshot("MKT-1", snapshot)
        return mgr

    def _make_delta(
        self, *, price: int, delta: int, side: str, ticker: str = "MKT-1"
    ) -> OrderBookDelta:
        return OrderBookDelta(
            market_ticker=ticker,
            market_id="uuid-1",
            price=price,
            delta=delta,
            side=side,
            ts="2026-03-03T12:00:00Z",
        )

    def test_upsert_existing_level(self, manager: OrderBookManager) -> None:
        d = self._make_delta(price=65, delta=150, side="yes")
        manager.apply_delta("MKT-1", d, seq=1)
        book = manager.get_book("MKT-1")
        assert book is not None
        level = next(l for l in book.yes if l.price == 65)
        assert level.quantity == 150

    def test_insert_new_level(self, manager: OrderBookManager) -> None:
        d = self._make_delta(price=62, delta=50, side="yes")
        manager.apply_delta("MKT-1", d, seq=1)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert len(book.yes) == 3
        # Should be sorted: 65, 62, 60
        assert [l.price for l in book.yes] == [65, 62, 60]

    def test_remove_level_on_zero_delta(self, manager: OrderBookManager) -> None:
        d = self._make_delta(price=60, delta=0, side="yes")
        manager.apply_delta("MKT-1", d, seq=1)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert len(book.yes) == 1
        assert book.yes[0].price == 65

    def test_applies_to_no_side(self, manager: OrderBookManager) -> None:
        d = self._make_delta(price=35, delta=300, side="no")
        manager.apply_delta("MKT-1", d, seq=1)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert book.no[0].quantity == 300

    def test_seq_gap_sets_stale(self, manager: OrderBookManager) -> None:
        d1 = self._make_delta(price=65, delta=110, side="yes")
        manager.apply_delta("MKT-1", d1, seq=1)
        # Skip seq 2 — jump to 3
        d2 = self._make_delta(price=65, delta=120, side="yes")
        manager.apply_delta("MKT-1", d2, seq=3)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert book.stale is True

    def test_sequential_deltas_not_stale(self, manager: OrderBookManager) -> None:
        d1 = self._make_delta(price=65, delta=110, side="yes")
        manager.apply_delta("MKT-1", d1, seq=1)
        d2 = self._make_delta(price=65, delta=120, side="yes")
        manager.apply_delta("MKT-1", d2, seq=2)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert book.stale is False

    def test_unknown_ticker_ignored(self, manager: OrderBookManager) -> None:
        d = self._make_delta(price=50, delta=100, side="yes", ticker="UNKNOWN")
        manager.apply_delta("UNKNOWN", d, seq=1)
        assert manager.get_book("UNKNOWN") is None

    def test_remove_nonexistent_level_is_noop(self, manager: OrderBookManager) -> None:
        d = self._make_delta(price=99, delta=0, side="yes")
        manager.apply_delta("MKT-1", d, seq=1)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert len(book.yes) == 2  # Unchanged
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_orderbook.py::TestApplyDelta -v`
Expected: FAIL — `apply_delta` not defined

**Step 3: Write minimal implementation**

Add to `OrderBookManager` in `src/talos/orderbook.py` (add `from talos.models.ws import OrderBookDelta` to imports):

```python
from talos.models.ws import OrderBookDelta, OrderBookSnapshot
```

Then add the method:

```python
    def apply_delta(self, ticker: str, delta: OrderBookDelta, *, seq: int = 0) -> None:
        """Apply incremental orderbook update. Sets stale on seq gap."""
        book = self._books.get(ticker)
        if book is None:
            logger.warning("orderbook_delta_unknown_ticker", ticker=ticker)
            return

        # Seq gap detection
        if seq > 0 and book.last_seq > 0 and seq != book.last_seq + 1:
            logger.warning(
                "orderbook_seq_gap",
                ticker=ticker,
                expected=book.last_seq + 1,
                got=seq,
            )
            book.stale = True
        if seq > 0:
            book.last_seq = seq

        # Select side
        side_levels = book.yes if delta.side == "yes" else book.no

        # Find existing level at this price
        idx = next(
            (i for i, lvl in enumerate(side_levels) if lvl.price == delta.price),
            None,
        )

        if delta.delta == 0:
            # Remove level
            if idx is not None:
                side_levels.pop(idx)
        elif idx is not None:
            # Update existing level
            side_levels[idx] = OrderBookLevel(price=delta.price, quantity=delta.delta)
        else:
            # Insert new level, maintain descending sort
            side_levels.append(OrderBookLevel(price=delta.price, quantity=delta.delta))
            side_levels.sort(key=lambda lvl: lvl.price, reverse=True)

        logger.debug(
            "orderbook_delta_applied",
            ticker=ticker,
            side=delta.side,
            price=delta.price,
            delta=delta.delta,
        )
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_orderbook.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/talos/orderbook.py tests/test_orderbook.py
git commit -m "feat: add apply_delta with seq gap detection to OrderBookManager"
```

---

### Task 4: Add query methods to OrderBookManager

Adds `best_bid`, `best_ask`, `remove`, and `tickers` property. `best_bid` returns the top YES level (highest yes bid). `best_ask` returns the top NO level (highest no bid — the implied best YES ask is at `100 - level.price`; conversion left to strategy layer).

**Files:**
- Modify: `src/talos/orderbook.py`
- Modify: `tests/test_orderbook.py`

**Step 1: Write the failing tests**

Add to `tests/test_orderbook.py`:

```python
class TestQueryMethods:
    @pytest.fixture()
    def manager(self) -> OrderBookManager:
        mgr = OrderBookManager()
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1", market_id="uuid-1",
            yes=[[65, 100], [60, 200]], no=[[35, 150], [40, 50]],
        )
        mgr.apply_snapshot("MKT-1", snapshot)
        return mgr

    def test_best_bid(self, manager: OrderBookManager) -> None:
        bid = manager.best_bid("MKT-1")
        assert bid is not None
        assert bid.price == 65
        assert bid.quantity == 100

    def test_best_bid_unknown_ticker(self, manager: OrderBookManager) -> None:
        assert manager.best_bid("NOPE") is None

    def test_best_bid_empty_book(self, manager: OrderBookManager) -> None:
        snap = OrderBookSnapshot(
            market_ticker="EMPTY", market_id="uuid-2", yes=[], no=[],
        )
        manager.apply_snapshot("EMPTY", snap)
        assert manager.best_bid("EMPTY") is None

    def test_best_ask(self, manager: OrderBookManager) -> None:
        ask = manager.best_ask("MKT-1")
        assert ask is not None
        assert ask.price == 40  # Top NO level
        assert ask.quantity == 50

    def test_best_ask_unknown_ticker(self, manager: OrderBookManager) -> None:
        assert manager.best_ask("NOPE") is None

    def test_remove(self, manager: OrderBookManager) -> None:
        manager.remove("MKT-1")
        assert manager.get_book("MKT-1") is None

    def test_remove_nonexistent_is_noop(self, manager: OrderBookManager) -> None:
        manager.remove("NOPE")  # Should not raise

    def test_tickers(self, manager: OrderBookManager) -> None:
        assert manager.tickers == {"MKT-1"}
        snap2 = OrderBookSnapshot(
            market_ticker="MKT-2", market_id="uuid-2", yes=[], no=[],
        )
        manager.apply_snapshot("MKT-2", snap2)
        assert manager.tickers == {"MKT-1", "MKT-2"}

    def test_tickers_after_remove(self, manager: OrderBookManager) -> None:
        manager.remove("MKT-1")
        assert manager.tickers == set()
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_orderbook.py::TestQueryMethods -v`
Expected: FAIL — `best_bid` not defined

**Step 3: Write minimal implementation**

Add to `OrderBookManager` in `src/talos/orderbook.py`:

```python
    def best_bid(self, ticker: str) -> OrderBookLevel | None:
        """Highest yes bid. Returns top of YES side."""
        book = self._books.get(ticker)
        if book and book.yes:
            return book.yes[0]
        return None

    def best_ask(self, ticker: str) -> OrderBookLevel | None:
        """Best implied YES ask. Returns top of NO side.

        The implied YES ask price is ``100 - level.price``.
        Conversion is left to the strategy layer.
        """
        book = self._books.get(ticker)
        if book and book.no:
            return book.no[0]
        return None

    def remove(self, ticker: str) -> None:
        """Stop tracking a ticker."""
        self._books.pop(ticker, None)
        logger.debug("orderbook_removed", ticker=ticker)

    @property
    def tickers(self) -> set[str]:
        """All currently tracked tickers."""
        return set(self._books.keys())
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_orderbook.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/talos/orderbook.py tests/test_orderbook.py
git commit -m "feat: add query methods to OrderBookManager"
```

---

### Task 5: Create MarketFeed

The async orchestrator that owns WebSocket subscriptions and routes messages to `OrderBookManager`. It registers a single callback for the `orderbook_delta` channel and routes by `market_ticker` in each message.

**Important context:**
- `KalshiWSClient.subscribe(channel, market_ticker)` takes a single ticker, not a list
- `KalshiWSClient.unsubscribe(sids)` takes a list of integer subscription IDs
- After Task 1, callbacks receive `(parsed_model, sid=int, seq=int)` as keyword args
- The `sid` for a ticker is learned from the first data message (snapshot or delta) for that ticker
- `OrderBookSnapshot` and `OrderBookDelta` both have a `market_ticker` field

**Files:**
- Create: `src/talos/market_feed.py`
- Create: `tests/test_market_feed.py`

**Step 1: Write the failing tests**

Create `tests/test_market_feed.py`:

```python
"""Tests for MarketFeed."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.market_feed import MarketFeed
from talos.models.ws import OrderBookDelta, OrderBookSnapshot
from talos.orderbook import OrderBookManager
from talos.ws_client import KalshiWSClient


@pytest.fixture()
def mock_ws() -> KalshiWSClient:
    ws = MagicMock(spec=KalshiWSClient)
    ws.subscribe = AsyncMock()
    ws.unsubscribe = AsyncMock()
    ws.disconnect = AsyncMock()
    ws.listen = AsyncMock()
    return ws


@pytest.fixture()
def mock_books() -> OrderBookManager:
    mgr = MagicMock(spec=OrderBookManager)
    return mgr


@pytest.fixture()
def feed(mock_ws: KalshiWSClient, mock_books: OrderBookManager) -> MarketFeed:
    return MarketFeed(ws_client=mock_ws, book_manager=mock_books)


class TestSubscribe:
    async def test_subscribe_calls_ws(
        self, feed: MarketFeed, mock_ws: KalshiWSClient
    ) -> None:
        await feed.subscribe("MKT-1")
        mock_ws.subscribe.assert_called_once_with("orderbook_delta", "MKT-1")  # type: ignore[union-attr]

    async def test_subscribe_tracks_ticker(self, feed: MarketFeed) -> None:
        await feed.subscribe("MKT-1")
        assert "MKT-1" in feed.subscriptions


class TestUnsubscribe:
    async def test_unsubscribe_calls_ws_with_sid(
        self, feed: MarketFeed, mock_ws: KalshiWSClient, mock_books: OrderBookManager
    ) -> None:
        await feed.subscribe("MKT-1")
        # Simulate receiving a message that maps ticker to sid
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1", market_id="uuid-1",
            yes=[[65, 100]], no=[[35, 50]],
        )
        await feed._on_message(snapshot, sid=5, seq=1)
        await feed.unsubscribe("MKT-1")
        mock_ws.unsubscribe.assert_called_once_with([5])  # type: ignore[union-attr]

    async def test_unsubscribe_removes_from_book_manager(
        self, feed: MarketFeed, mock_books: OrderBookManager
    ) -> None:
        await feed.subscribe("MKT-1")
        await feed.unsubscribe("MKT-1")
        mock_books.remove.assert_called_once_with("MKT-1")  # type: ignore[union-attr]

    async def test_unsubscribe_removes_from_subscriptions(
        self, feed: MarketFeed
    ) -> None:
        await feed.subscribe("MKT-1")
        await feed.unsubscribe("MKT-1")
        assert "MKT-1" not in feed.subscriptions


class TestMessageRouting:
    async def test_snapshot_routes_to_apply_snapshot(
        self, feed: MarketFeed, mock_books: OrderBookManager
    ) -> None:
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1", market_id="uuid-1",
            yes=[[65, 100]], no=[[35, 50]],
        )
        await feed._on_message(snapshot, sid=1, seq=1)
        mock_books.apply_snapshot.assert_called_once_with("MKT-1", snapshot)  # type: ignore[union-attr]

    async def test_delta_routes_to_apply_delta(
        self, feed: MarketFeed, mock_books: OrderBookManager
    ) -> None:
        delta = OrderBookDelta(
            market_ticker="MKT-1", market_id="uuid-1",
            price=65, delta=150, side="yes", ts="2026-03-03T12:00:00Z",
        )
        await feed._on_message(delta, sid=1, seq=2)
        mock_books.apply_delta.assert_called_once_with("MKT-1", delta, seq=2)  # type: ignore[union-attr]

    async def test_sid_mapping_learned_from_first_message(
        self, feed: MarketFeed
    ) -> None:
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1", market_id="uuid-1",
            yes=[], no=[],
        )
        await feed._on_message(snapshot, sid=7, seq=1)
        assert feed._ticker_to_sid.get("MKT-1") == 7


class TestStartStop:
    async def test_start_calls_listen(
        self, feed: MarketFeed, mock_ws: KalshiWSClient
    ) -> None:
        await feed.start()
        mock_ws.listen.assert_called_once()  # type: ignore[union-attr]

    async def test_stop_unsubscribes_all_and_disconnects(
        self, feed: MarketFeed, mock_ws: KalshiWSClient, mock_books: OrderBookManager
    ) -> None:
        await feed.subscribe("MKT-1")
        await feed.subscribe("MKT-2")
        await feed.stop()
        mock_ws.disconnect.assert_called_once()  # type: ignore[union-attr]
        assert feed.subscriptions == set()


class TestCallbackRegistration:
    def test_registers_callback_on_init(self, mock_ws: KalshiWSClient) -> None:
        mgr = MagicMock(spec=OrderBookManager)
        MarketFeed(ws_client=mock_ws, book_manager=mgr)
        mock_ws.on_message.assert_called_once()  # type: ignore[union-attr]
        call_args = mock_ws.on_message.call_args  # type: ignore[union-attr]
        assert call_args[0][0] == "orderbook_delta"
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_market_feed.py -v`
Expected: FAIL — `talos.market_feed` does not exist

**Step 3: Write minimal implementation**

Create `src/talos/market_feed.py`:

```python
"""Async orchestrator for real-time market data subscriptions."""

from __future__ import annotations

import structlog

from talos.models.ws import OrderBookDelta, OrderBookSnapshot
from talos.orderbook import OrderBookManager
from talos.ws_client import KalshiWSClient

logger = structlog.get_logger()


class MarketFeed:
    """Subscribes to markets via WebSocket, feeds OrderBookManager.

    Routes orderbook snapshots and deltas to the book manager.
    Tracks sid-to-ticker mapping for unsubscribe support.
    """

    def __init__(
        self,
        ws_client: KalshiWSClient,
        book_manager: OrderBookManager,
    ) -> None:
        self._ws = ws_client
        self._books = book_manager
        self._subscribed_tickers: set[str] = set()
        self._ticker_to_sid: dict[str, int] = {}
        self._ws.on_message("orderbook_delta", self._on_message)

    async def _on_message(
        self,
        msg: OrderBookSnapshot | OrderBookDelta,
        *,
        sid: int = 0,
        seq: int = 0,
    ) -> None:
        """Route a WS message to the book manager."""
        ticker = msg.market_ticker

        # Learn sid mapping from first message for this ticker
        if sid and ticker not in self._ticker_to_sid:
            self._ticker_to_sid[ticker] = sid

        if isinstance(msg, OrderBookSnapshot):
            self._books.apply_snapshot(ticker, msg)
            logger.info("market_feed_snapshot", ticker=ticker)
        elif isinstance(msg, OrderBookDelta):
            self._books.apply_delta(ticker, msg, seq=seq)

    async def subscribe(self, ticker: str) -> None:
        """Subscribe to orderbook updates for a ticker."""
        await self._ws.subscribe("orderbook_delta", ticker)
        self._subscribed_tickers.add(ticker)
        logger.info("market_feed_subscribe", ticker=ticker)

    async def unsubscribe(self, ticker: str) -> None:
        """Unsubscribe and remove from book manager."""
        sid = self._ticker_to_sid.pop(ticker, None)
        if sid is not None:
            await self._ws.unsubscribe([sid])
        self._subscribed_tickers.discard(ticker)
        self._books.remove(ticker)
        logger.info("market_feed_unsubscribe", ticker=ticker)

    async def start(self) -> None:
        """Begin listening for WS messages."""
        logger.info("market_feed_start")
        await self._ws.listen()

    async def stop(self) -> None:
        """Unsubscribe all tickers and disconnect."""
        for ticker in list(self._subscribed_tickers):
            await self.unsubscribe(ticker)
        await self._ws.disconnect()
        logger.info("market_feed_stop")

    @property
    def subscriptions(self) -> set[str]:
        """Currently subscribed tickers."""
        return set(self._subscribed_tickers)
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_market_feed.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/talos/market_feed.py tests/test_market_feed.py
git commit -m "feat: add MarketFeed async orchestrator for WS subscriptions"
```

---

### Task 6: Lint and type check

**Files:**
- Potentially any file from Tasks 1–5

**Step 1: Run ruff lint**

Run: `.venv/Scripts/python -m ruff check src/talos/orderbook.py src/talos/market_feed.py tests/test_orderbook.py tests/test_market_feed.py src/talos/ws_client.py tests/test_ws_client.py`

Fix any issues. Common ones:
- Import sorting (auto-fixable with `--fix`)
- Unused imports

**Step 2: Run ruff format**

Run: `.venv/Scripts/python -m ruff format src/talos/orderbook.py src/talos/market_feed.py tests/test_orderbook.py tests/test_market_feed.py`

**Step 3: Run pyright**

Run: `.venv/Scripts/python -m pyright src/talos/orderbook.py src/talos/market_feed.py`

Fix any type errors. Likely issues:
- `MagicMock` vs typed calls in tests (use `# type: ignore[union-attr]` as needed)

**Step 4: Run full test suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: ALL PASS (previous 67 + new tests from Layer 2)

**Step 5: Commit any fixes**

```bash
git add -u
git commit -m "chore: lint and type fixes for Layer 2"
```

---

### Task 7: Update brain vault

**Files:**
- Modify: `brain/architecture.md`
- Modify: `brain/codebase/index.md`

**Step 1: Update architecture**

In `brain/architecture.md`, change Layer 2 from "next" to "COMPLETE":

```markdown
2. **Market Data** (Layer 2) — **COMPLETE**
   - `orderbook.py` — pure state machine: `LocalOrderBook` model, `OrderBookManager` (apply snapshot/delta, seq tracking, staleness)
   - `market_feed.py` — async orchestrator: subscribes to markets via WS, routes snapshots/deltas to book manager
```

**Step 2: Update codebase index**

In `brain/codebase/index.md`, add two new rows to the module map:

```markdown
| `orderbook.py` | Local orderbook state management | `LocalOrderBook`, `OrderBookManager` |
| `market_feed.py` | WS subscription orchestrator | `MarketFeed` |
```

Add to Gotchas:

```markdown
- **WS callback kwargs:** After Layer 2, WS callbacks receive `(parsed, sid=int, seq=int)` as keyword args. The `sid` is used by `MarketFeed` to track ticker-to-subscription mappings for unsubscribe.
- **best_ask returns NO side:** `OrderBookManager.best_ask()` returns the top NO level. The implied YES ask price is `100 - level.price`. Conversion is left to the strategy layer.
```

**Step 3: Commit**

```bash
git add brain/architecture.md brain/codebase/index.md
git commit -m "docs: update brain vault for Layer 2 completion"
```
