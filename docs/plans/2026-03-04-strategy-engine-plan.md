# Strategy Engine Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a pure-state arbitrage scanner that detects NO+NO opportunities within Kalshi game events, plus an async game manager for URL-based setup.

**Architecture:** Three new modules — strategy models (`models/strategy.py`), arbitrage scanner (`scanner.py`), and game manager (`game_manager.py`). One modification to `market_feed.py` to add an `on_book_update` callback. Scanner is a pure state machine (like `OrderBookManager`). GameManager is an async orchestrator (like `MarketFeed`).

**Tech Stack:** Python 3.12+, Pydantic v2, structlog, pytest + pytest-asyncio

**Design doc:** `docs/plans/2026-03-04-strategy-engine-design.md`

---

## Context for implementers

### How the arbitrage works

Each Kalshi game event (e.g., Stanford vs Miami) has two contracts — one per team. Buying NO on both guarantees a $1 payout (exactly one team wins). Profit exists when the combined NO cost is less than $1.

```
NO ask for team A = 100 - best_yes_bid_A  (cents)
NO ask for team B = 100 - best_yes_bid_B  (cents)
raw_edge = best_yes_bid_A + best_yes_bid_B - 100
```

Edge > 0 means profit. The scanner uses `OrderBookManager.best_bid()` (top of YES side) for each contract.

### Key files to understand

- `src/talos/orderbook.py` — `OrderBookManager` with `best_bid(ticker)`, `get_book(ticker)`, `LocalOrderBook.stale`
- `src/talos/market_feed.py` — `MarketFeed` routes WS messages to `OrderBookManager`
- `src/talos/rest_client.py:105-110` — `get_event(event_ticker, with_nested_markets=True)` returns `Event` with `markets: list[Market]`
- `src/talos/models/market.py` — `Market`, `Event`, `OrderBookLevel`
- `tests/test_orderbook.py` — reference for pure state machine test patterns (no mocks needed)
- `tests/test_market_feed.py` — reference for async orchestrator test patterns (MagicMock + AsyncMock)

### Running tests

```bash
.venv/Scripts/python -m pytest tests/test_scanner.py -v         # scanner tests
.venv/Scripts/python -m pytest tests/test_game_manager.py -v    # game manager tests
.venv/Scripts/python -m pytest tests/test_market_feed.py -v     # feed callback tests
.venv/Scripts/python -m pytest -v                                # all tests
```

---

## Task 1: Strategy models (ArbPair + Opportunity)

**Files:**
- Create: `src/talos/models/strategy.py`
- Modify: `src/talos/models/__init__.py`

**Step 1: Create the models file**

Create `src/talos/models/strategy.py`:

```python
"""Pydantic models for arbitrage strategy."""

from __future__ import annotations

from pydantic import BaseModel


class ArbPair(BaseModel):
    """Two mutually exclusive markets within a game event."""

    event_ticker: str
    ticker_a: str
    ticker_b: str


class Opportunity(BaseModel):
    """A detected NO+NO arbitrage opportunity."""

    event_ticker: str
    ticker_a: str
    ticker_b: str
    no_a: int
    no_b: int
    qty_a: int
    qty_b: int
    raw_edge: int
    tradeable_qty: int
    timestamp: str
```

**Step 2: Add exports to `models/__init__.py`**

Add to `src/talos/models/__init__.py`:
- Import `ArbPair` and `Opportunity` from `talos.models.strategy`
- Add both to `__all__`

**Step 3: Run all tests to confirm nothing broke**

Run: `.venv/Scripts/python -m pytest -v`
Expected: All existing tests PASS (103 tests)

**Step 4: Commit**

```bash
git add src/talos/models/strategy.py src/talos/models/__init__.py
git commit -m "feat: add ArbPair and Opportunity strategy models"
```

---

## Task 2: ArbitrageScanner — core scan logic

This is the pure state machine. No I/O, no async. Same testing pattern as `OrderBookManager` in `tests/test_orderbook.py`.

**Files:**
- Create: `src/talos/scanner.py`
- Create: `tests/test_scanner.py`

**Step 1: Write the failing tests**

Create `tests/test_scanner.py`:

```python
"""Tests for ArbitrageScanner."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from talos.models.ws import OrderBookSnapshot
from talos.orderbook import OrderBookManager
from talos.scanner import ArbitrageScanner


def _setup_books(manager: OrderBookManager, bid_a: int, qty_a: int, bid_b: int, qty_b: int) -> None:
    """Set up two books with given YES bid prices and quantities."""
    manager.apply_snapshot(
        "GAME-STAN",
        OrderBookSnapshot(market_ticker="GAME-STAN", market_id="u1", yes=[[bid_a, qty_a]], no=[]),
    )
    manager.apply_snapshot(
        "GAME-MIA",
        OrderBookSnapshot(market_ticker="GAME-MIA", market_id="u2", yes=[[bid_b, qty_b]], no=[]),
    )


class TestPairManagement:
    @pytest.fixture()
    def scanner(self) -> ArbitrageScanner:
        return ArbitrageScanner(OrderBookManager())

    def test_add_pair(self, scanner: ArbitrageScanner) -> None:
        scanner.add_pair("EVT-1", "TICK-A", "TICK-B")
        assert len(scanner.pairs) == 1
        assert scanner.pairs[0].event_ticker == "EVT-1"

    def test_remove_pair(self, scanner: ArbitrageScanner) -> None:
        scanner.add_pair("EVT-1", "TICK-A", "TICK-B")
        scanner.remove_pair("EVT-1")
        assert len(scanner.pairs) == 0

    def test_remove_nonexistent_pair_is_noop(self, scanner: ArbitrageScanner) -> None:
        scanner.remove_pair("EVT-NOPE")
        assert len(scanner.pairs) == 0


class TestScanFindsOpportunity:
    @pytest.fixture()
    def manager(self) -> OrderBookManager:
        return OrderBookManager()

    @pytest.fixture()
    def scanner(self, manager: OrderBookManager) -> ArbitrageScanner:
        s = ArbitrageScanner(manager)
        s.add_pair("GAME-1", "GAME-STAN", "GAME-MIA")
        return s

    def test_detects_positive_edge(self, scanner: ArbitrageScanner, manager: OrderBookManager) -> None:
        # YES bids: 62 + 45 = 107 > 100 → edge = 7
        _setup_books(manager, bid_a=62, qty_a=100, bid_b=45, qty_b=200)
        scanner.scan("GAME-STAN")
        opps = scanner.opportunities
        assert len(opps) == 1
        assert opps[0].raw_edge == 7
        assert opps[0].no_a == 38  # 100 - 62
        assert opps[0].no_b == 55  # 100 - 45

    def test_tradeable_qty_is_min(self, scanner: ArbitrageScanner, manager: OrderBookManager) -> None:
        _setup_books(manager, bid_a=62, qty_a=100, bid_b=45, qty_b=200)
        scanner.scan("GAME-STAN")
        assert scanner.opportunities[0].tradeable_qty == 100

    def test_scan_from_either_leg(self, scanner: ArbitrageScanner, manager: OrderBookManager) -> None:
        _setup_books(manager, bid_a=62, qty_a=100, bid_b=45, qty_b=200)
        scanner.scan("GAME-MIA")
        assert len(scanner.opportunities) == 1


class TestScanNoOpportunity:
    @pytest.fixture()
    def manager(self) -> OrderBookManager:
        return OrderBookManager()

    @pytest.fixture()
    def scanner(self, manager: OrderBookManager) -> ArbitrageScanner:
        s = ArbitrageScanner(manager)
        s.add_pair("GAME-1", "GAME-STAN", "GAME-MIA")
        return s

    def test_no_edge_when_sum_under_100(self, scanner: ArbitrageScanner, manager: OrderBookManager) -> None:
        # YES bids: 40 + 50 = 90 < 100 → no edge
        _setup_books(manager, bid_a=40, qty_a=100, bid_b=50, qty_b=100)
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0

    def test_no_edge_when_sum_exactly_100(self, scanner: ArbitrageScanner, manager: OrderBookManager) -> None:
        _setup_books(manager, bid_a=50, qty_a=100, bid_b=50, qty_b=100)
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0

    def test_removes_vanished_opportunity(self, scanner: ArbitrageScanner, manager: OrderBookManager) -> None:
        # First: edge exists
        _setup_books(manager, bid_a=62, qty_a=100, bid_b=45, qty_b=200)
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 1
        # Then: edge vanishes
        _setup_books(manager, bid_a=40, qty_a=100, bid_b=50, qty_b=200)
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0


class TestScanEdgeCases:
    @pytest.fixture()
    def manager(self) -> OrderBookManager:
        return OrderBookManager()

    @pytest.fixture()
    def scanner(self, manager: OrderBookManager) -> ArbitrageScanner:
        s = ArbitrageScanner(manager)
        s.add_pair("GAME-1", "GAME-STAN", "GAME-MIA")
        return s

    def test_missing_book_for_one_leg(self, scanner: ArbitrageScanner, manager: OrderBookManager) -> None:
        # Only set up one book
        manager.apply_snapshot(
            "GAME-STAN",
            OrderBookSnapshot(market_ticker="GAME-STAN", market_id="u1", yes=[[62, 100]], no=[]),
        )
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0

    def test_empty_book_no_bids(self, scanner: ArbitrageScanner, manager: OrderBookManager) -> None:
        manager.apply_snapshot(
            "GAME-STAN",
            OrderBookSnapshot(market_ticker="GAME-STAN", market_id="u1", yes=[], no=[]),
        )
        manager.apply_snapshot(
            "GAME-MIA",
            OrderBookSnapshot(market_ticker="GAME-MIA", market_id="u2", yes=[[45, 100]], no=[]),
        )
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0

    def test_stale_book_skipped(self, scanner: ArbitrageScanner, manager: OrderBookManager) -> None:
        _setup_books(manager, bid_a=62, qty_a=100, bid_b=45, qty_b=200)
        book = manager.get_book("GAME-STAN")
        assert book is not None
        book.stale = True
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 0

    def test_scan_unrelated_ticker_is_noop(self, scanner: ArbitrageScanner, manager: OrderBookManager) -> None:
        _setup_books(manager, bid_a=62, qty_a=100, bid_b=45, qty_b=200)
        scanner.scan("UNRELATED")
        assert len(scanner.opportunities) == 0

    def test_updates_existing_opportunity(self, scanner: ArbitrageScanner, manager: OrderBookManager) -> None:
        _setup_books(manager, bid_a=62, qty_a=100, bid_b=45, qty_b=200)
        scanner.scan("GAME-STAN")
        assert scanner.opportunities[0].raw_edge == 7
        # Update bid
        _setup_books(manager, bid_a=65, qty_a=150, bid_b=45, qty_b=200)
        scanner.scan("GAME-STAN")
        assert len(scanner.opportunities) == 1
        assert scanner.opportunities[0].raw_edge == 10
        assert scanner.opportunities[0].tradeable_qty == 150


class TestOpportunitySorting:
    def test_sorted_by_edge_descending(self) -> None:
        manager = OrderBookManager()
        scanner = ArbitrageScanner(manager)
        scanner.add_pair("GAME-1", "GAME-A1", "GAME-B1")
        scanner.add_pair("GAME-2", "GAME-A2", "GAME-B2")

        # Game 1: edge = 5
        manager.apply_snapshot("GAME-A1", OrderBookSnapshot(market_ticker="GAME-A1", market_id="u1", yes=[[55, 100]], no=[]))
        manager.apply_snapshot("GAME-B1", OrderBookSnapshot(market_ticker="GAME-B1", market_id="u2", yes=[[50, 100]], no=[]))
        # Game 2: edge = 10
        manager.apply_snapshot("GAME-A2", OrderBookSnapshot(market_ticker="GAME-A2", market_id="u3", yes=[[60, 100]], no=[]))
        manager.apply_snapshot("GAME-B2", OrderBookSnapshot(market_ticker="GAME-B2", market_id="u4", yes=[[50, 100]], no=[]))

        scanner.scan("GAME-A1")
        scanner.scan("GAME-A2")

        opps = scanner.opportunities
        assert len(opps) == 2
        assert opps[0].raw_edge == 10
        assert opps[1].raw_edge == 5
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_scanner.py -v`
Expected: FAIL (ImportError — `talos.scanner` doesn't exist yet)

**Step 3: Write the ArbitrageScanner implementation**

Create `src/talos/scanner.py`:

```python
"""Arbitrage opportunity scanner for NO+NO pairs."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from talos.models.strategy import ArbPair, Opportunity
from talos.orderbook import OrderBookManager

logger = structlog.get_logger()


class ArbitrageScanner:
    """Detects NO+NO arbitrage within game events.

    Pure state machine — no I/O, no async. Reads orderbook state
    from OrderBookManager, evaluates registered pairs, maintains
    a list of current opportunities.
    """

    def __init__(self, book_manager: OrderBookManager) -> None:
        self._books = book_manager
        self._pairs: list[ArbPair] = []
        self._pairs_by_ticker: dict[str, list[ArbPair]] = {}
        self._opportunities: dict[str, Opportunity] = {}  # keyed by event_ticker

    def add_pair(self, event_ticker: str, ticker_a: str, ticker_b: str) -> None:
        """Register a pair of markets to monitor."""
        pair = ArbPair(event_ticker=event_ticker, ticker_a=ticker_a, ticker_b=ticker_b)
        self._pairs.append(pair)
        self._pairs_by_ticker.setdefault(ticker_a, []).append(pair)
        self._pairs_by_ticker.setdefault(ticker_b, []).append(pair)
        logger.info("scanner_pair_added", event=event_ticker, a=ticker_a, b=ticker_b)

    def remove_pair(self, event_ticker: str) -> None:
        """Remove a pair by event ticker."""
        pair = next((p for p in self._pairs if p.event_ticker == event_ticker), None)
        if pair is None:
            return
        self._pairs.remove(pair)
        for ticker in (pair.ticker_a, pair.ticker_b):
            ticker_pairs = self._pairs_by_ticker.get(ticker, [])
            if pair in ticker_pairs:
                ticker_pairs.remove(pair)
                if not ticker_pairs:
                    del self._pairs_by_ticker[ticker]
        self._opportunities.pop(event_ticker, None)
        logger.info("scanner_pair_removed", event=event_ticker)

    def scan(self, ticker: str) -> None:
        """Re-evaluate all pairs involving this ticker."""
        pairs = self._pairs_by_ticker.get(ticker, [])
        for pair in pairs:
            self._evaluate_pair(pair)

    def _evaluate_pair(self, pair: ArbPair) -> None:
        """Check one pair for arbitrage opportunity."""
        bid_a = self._books.best_bid(pair.ticker_a)
        bid_b = self._books.best_bid(pair.ticker_b)

        # Need both legs with valid bids
        if not bid_a or not bid_b:
            self._opportunities.pop(pair.event_ticker, None)
            return

        # Skip stale books
        book_a = self._books.get_book(pair.ticker_a)
        book_b = self._books.get_book(pair.ticker_b)
        if (book_a and book_a.stale) or (book_b and book_b.stale):
            self._opportunities.pop(pair.event_ticker, None)
            return

        raw_edge = bid_a.price + bid_b.price - 100

        if raw_edge > 0:
            opp = Opportunity(
                event_ticker=pair.event_ticker,
                ticker_a=pair.ticker_a,
                ticker_b=pair.ticker_b,
                no_a=100 - bid_a.price,
                no_b=100 - bid_b.price,
                qty_a=bid_a.quantity,
                qty_b=bid_b.quantity,
                raw_edge=raw_edge,
                tradeable_qty=min(bid_a.quantity, bid_b.quantity),
                timestamp=datetime.now(UTC).isoformat(),
            )
            self._opportunities[pair.event_ticker] = opp
            logger.debug(
                "scanner_opportunity",
                event=pair.event_ticker,
                edge=raw_edge,
                qty=opp.tradeable_qty,
            )
        else:
            self._opportunities.pop(pair.event_ticker, None)

    @property
    def opportunities(self) -> list[Opportunity]:
        """Current opportunities, sorted by raw_edge descending."""
        return sorted(self._opportunities.values(), key=lambda o: o.raw_edge, reverse=True)

    @property
    def pairs(self) -> list[ArbPair]:
        """Currently registered pairs."""
        return list(self._pairs)
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_scanner.py -v`
Expected: All PASS

**Step 5: Run full test suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: All PASS (103 existing + new scanner tests)

**Step 6: Commit**

```bash
git add src/talos/scanner.py tests/test_scanner.py
git commit -m "feat: add ArbitrageScanner with NO+NO edge detection"
```

---

## Task 3: MarketFeed on_book_update callback

Add a generic callback to `MarketFeed` that fires after each book update, so the scanner can register `scan()`.

**Files:**
- Modify: `src/talos/market_feed.py:23-52`
- Modify: `tests/test_market_feed.py`

**Step 1: Write the failing tests**

Add to `tests/test_market_feed.py` — a new test class at the bottom:

```python
class TestOnBookUpdate:
    async def test_callback_fires_after_snapshot(
        self, feed: MarketFeed, mock_ws: KalshiWSClient, mock_books: OrderBookManager
    ) -> None:
        callback = MagicMock()
        feed.on_book_update = callback
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[65, 100]],
            no=[[35, 50]],
        )
        await feed._on_message(snapshot, sid=1, seq=1)
        callback.assert_called_once_with("MKT-1")

    async def test_callback_fires_after_delta(
        self, feed: MarketFeed, mock_ws: KalshiWSClient, mock_books: OrderBookManager
    ) -> None:
        callback = MagicMock()
        feed.on_book_update = callback
        delta = OrderBookDelta(
            market_ticker="MKT-1",
            market_id="uuid-1",
            price=65,
            delta=150,
            side="yes",
            ts="2026-03-03T12:00:00Z",
        )
        await feed._on_message(delta, sid=1, seq=2)
        callback.assert_called_once_with("MKT-1")

    async def test_no_callback_no_error(
        self, feed: MarketFeed, mock_books: OrderBookManager
    ) -> None:
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[65, 100]],
            no=[[35, 50]],
        )
        # No callback set — should not raise
        await feed._on_message(snapshot, sid=1, seq=1)
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_market_feed.py::TestOnBookUpdate -v`
Expected: FAIL (AttributeError — `on_book_update` doesn't exist)

**Step 3: Modify MarketFeed**

In `src/talos/market_feed.py`:

Add to `__init__` (after line 31):
```python
self.on_book_update: Callable[[str], None] | None = None
```

Add import at top:
```python
from collections.abc import Callable
```

Modify `_on_message` — add at the end of the method (after the `elif` block, before method ends), at line 52:
```python
        if self.on_book_update:
            self.on_book_update(ticker)
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_market_feed.py -v`
Expected: All PASS (existing 11 + 3 new)

**Step 5: Run full suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add src/talos/market_feed.py tests/test_market_feed.py
git commit -m "feat: add on_book_update callback to MarketFeed"
```

---

## Task 4: URL parser + GameManager

The async orchestrator that sets up games from pasted Kalshi URLs. Similar test pattern to `tests/test_market_feed.py` (MagicMock + AsyncMock).

**Files:**
- Create: `src/talos/game_manager.py`
- Create: `tests/test_game_manager.py`

**Step 1: Write the failing tests**

Create `tests/test_game_manager.py`:

```python
"""Tests for GameManager and URL parsing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.game_manager import GameManager, parse_kalshi_url
from talos.market_feed import MarketFeed
from talos.models.market import Event, Market
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner


class TestParseKalshiUrl:
    def test_parses_full_url(self) -> None:
        url = "https://kalshi.com/markets/kxncaawbgame/college-basketball-womens-game/kxncaawbgame-26mar04stanmia"
        assert parse_kalshi_url(url) == "kxncaawbgame-26mar04stanmia"

    def test_parses_url_with_trailing_slash(self) -> None:
        url = "https://kalshi.com/markets/kxncaawbgame/college-basketball-womens-game/kxncaawbgame-26mar04stanmia/"
        assert parse_kalshi_url(url) == "kxncaawbgame-26mar04stanmia"

    def test_parses_bare_ticker(self) -> None:
        assert parse_kalshi_url("kxncaawbgame-26mar04stanmia") == "kxncaawbgame-26mar04stanmia"

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_kalshi_url("")

    def test_rejects_non_kalshi_url(self) -> None:
        with pytest.raises(ValueError, match="Kalshi"):
            parse_kalshi_url("https://example.com/markets/foo")


class TestGameManager:
    @pytest.fixture()
    def mock_rest(self) -> KalshiRESTClient:
        rest = MagicMock(spec=KalshiRESTClient)
        rest.get_event = AsyncMock()
        return rest

    @pytest.fixture()
    def mock_feed(self) -> MarketFeed:
        feed = MagicMock(spec=MarketFeed)
        feed.subscribe = AsyncMock()
        feed.unsubscribe = AsyncMock()
        return feed

    @pytest.fixture()
    def mock_scanner(self) -> ArbitrageScanner:
        scanner = MagicMock(spec=ArbitrageScanner)
        return scanner

    @pytest.fixture()
    def manager(
        self,
        mock_rest: KalshiRESTClient,
        mock_feed: MarketFeed,
        mock_scanner: ArbitrageScanner,
    ) -> GameManager:
        return GameManager(rest=mock_rest, feed=mock_feed, scanner=mock_scanner)

    def _make_event(self, event_ticker: str, tickers: list[str]) -> Event:
        markets = [
            Market(ticker=t, event_ticker=event_ticker, title=f"Team {i}", status="open")
            for i, t in enumerate(tickers)
        ]
        return Event(
            event_ticker=event_ticker,
            series_ticker="SER-1",
            title="Game",
            category="sports",
            status="open",
            markets=markets,
        )

    async def test_add_game_fetches_event(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
        mock_scanner: ArbitrageScanner,
        mock_feed: MarketFeed,
    ) -> None:
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        await manager.add_game("EVT-1")
        mock_rest.get_event.assert_called_once_with("EVT-1", with_nested_markets=True)  # type: ignore[union-attr]

    async def test_add_game_registers_pair(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
        mock_scanner: ArbitrageScanner,
    ) -> None:
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        await manager.add_game("EVT-1")
        mock_scanner.add_pair.assert_called_once_with("EVT-1", "TICK-A", "TICK-B")  # type: ignore[union-attr]

    async def test_add_game_subscribes_both_tickers(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
        mock_feed: MarketFeed,
    ) -> None:
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        await manager.add_game("EVT-1")
        assert mock_feed.subscribe.call_count == 2  # type: ignore[union-attr]
        mock_feed.subscribe.assert_any_call("TICK-A")  # type: ignore[union-attr]
        mock_feed.subscribe.assert_any_call("TICK-B")  # type: ignore[union-attr]

    async def test_add_game_rejects_non_binary_event(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
    ) -> None:
        event = self._make_event("EVT-1", ["A", "B", "C"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        with pytest.raises(ValueError, match="exactly 2"):
            await manager.add_game("EVT-1")

    async def test_add_game_returns_pair(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
    ) -> None:
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        pair = await manager.add_game("EVT-1")
        assert pair.event_ticker == "EVT-1"
        assert pair.ticker_a == "TICK-A"
        assert pair.ticker_b == "TICK-B"

    async def test_add_game_from_url(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
    ) -> None:
        event = self._make_event("kxncaawbgame-26mar04stanmia", ["STAN", "MIA"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        url = "https://kalshi.com/markets/kxncaawbgame/college-basketball-womens-game/kxncaawbgame-26mar04stanmia"
        pair = await manager.add_game(url)
        assert pair.event_ticker == "kxncaawbgame-26mar04stanmia"

    async def test_add_games_multiple(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
    ) -> None:
        mock_rest.get_event.side_effect = [  # type: ignore[union-attr]
            self._make_event("EVT-1", ["A1", "B1"]),
            self._make_event("EVT-2", ["A2", "B2"]),
        ]
        pairs = await manager.add_games(["EVT-1", "EVT-2"])
        assert len(pairs) == 2

    async def test_remove_game(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
        mock_feed: MarketFeed,
        mock_scanner: ArbitrageScanner,
    ) -> None:
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        await manager.add_game("EVT-1")
        await manager.remove_game("EVT-1")
        mock_scanner.remove_pair.assert_called_once_with("EVT-1")  # type: ignore[union-attr]
        assert mock_feed.unsubscribe.call_count == 2  # type: ignore[union-attr]

    async def test_active_games(
        self,
        manager: GameManager,
        mock_rest: KalshiRESTClient,
    ) -> None:
        event = self._make_event("EVT-1", ["TICK-A", "TICK-B"])
        mock_rest.get_event.return_value = event  # type: ignore[union-attr]
        await manager.add_game("EVT-1")
        assert len(manager.active_games) == 1
        assert manager.active_games[0].event_ticker == "EVT-1"
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_game_manager.py -v`
Expected: FAIL (ImportError — `talos.game_manager` doesn't exist)

**Step 3: Write the GameManager implementation**

Create `src/talos/game_manager.py`:

```python
"""Game lifecycle manager — sets up monitoring from Kalshi URLs."""

from __future__ import annotations

from urllib.parse import urlparse

import structlog

from talos.market_feed import MarketFeed
from talos.models.strategy import ArbPair
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner

logger = structlog.get_logger()


def parse_kalshi_url(url_or_ticker: str) -> str:
    """Extract event ticker from a Kalshi URL or return bare ticker.

    Accepted formats:
      - https://kalshi.com/markets/series/slug/EVENT-TICKER
      - EVENT-TICKER (bare)
    """
    if not url_or_ticker.strip():
        raise ValueError("URL or ticker is empty")

    parsed = urlparse(url_or_ticker)
    if parsed.scheme and parsed.netloc:
        if "kalshi.com" not in parsed.netloc:
            raise ValueError(f"Not a Kalshi URL: {parsed.netloc}")
        path = parsed.path.rstrip("/")
        return path.rsplit("/", 1)[-1]

    return url_or_ticker.strip()


class GameManager:
    """Orchestrates game setup, teardown, and ties layers together.

    Async — owns REST calls and feed subscriptions.
    """

    def __init__(
        self,
        rest: KalshiRESTClient,
        feed: MarketFeed,
        scanner: ArbitrageScanner,
    ) -> None:
        self._rest = rest
        self._feed = feed
        self._scanner = scanner
        self._games: dict[str, ArbPair] = {}

    async def add_game(self, url_or_ticker: str) -> ArbPair:
        """Set up monitoring for a game from a URL or event ticker."""
        ticker = parse_kalshi_url(url_or_ticker)
        event = await self._rest.get_event(ticker, with_nested_markets=True)

        if len(event.markets) != 2:
            raise ValueError(
                f"Event {ticker} has {len(event.markets)} markets, expected exactly 2"
            )

        ticker_a = event.markets[0].ticker
        ticker_b = event.markets[1].ticker

        pair = ArbPair(event_ticker=event.event_ticker, ticker_a=ticker_a, ticker_b=ticker_b)
        self._scanner.add_pair(event.event_ticker, ticker_a, ticker_b)
        await self._feed.subscribe(ticker_a)
        await self._feed.subscribe(ticker_b)
        self._games[event.event_ticker] = pair

        logger.info(
            "game_added",
            event=event.event_ticker,
            a=ticker_a,
            b=ticker_b,
            title=event.title,
        )
        return pair

    async def add_games(self, urls: list[str]) -> list[ArbPair]:
        """Set up monitoring for multiple games."""
        pairs = []
        for url in urls:
            pair = await self.add_game(url)
            pairs.append(pair)
        return pairs

    async def remove_game(self, event_ticker: str) -> None:
        """Remove a game from monitoring."""
        pair = self._games.pop(event_ticker, None)
        if pair is None:
            return
        self._scanner.remove_pair(event_ticker)
        await self._feed.unsubscribe(pair.ticker_a)
        await self._feed.unsubscribe(pair.ticker_b)
        logger.info("game_removed", event=event_ticker)

    @property
    def active_games(self) -> list[ArbPair]:
        """Currently monitored games."""
        return list(self._games.values())
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_game_manager.py -v`
Expected: All PASS

**Step 5: Run full suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add src/talos/game_manager.py tests/test_game_manager.py
git commit -m "feat: add GameManager with URL parsing for game setup"
```

---

## Task 5: Lint + type check

**Step 1: Run ruff lint**

Run: `.venv/Scripts/python -m ruff check src/ tests/`

Fix any errors found (unused imports, line length, etc.).

**Step 2: Run ruff format**

Run: `.venv/Scripts/python -m ruff format src/ tests/`

**Step 3: Run pyright**

Run: `.venv/Scripts/python -m pyright`
Expected: 0 errors

**Step 4: Run full test suite one final time**

Run: `.venv/Scripts/python -m pytest -v`
Expected: All PASS

**Step 5: Commit (if any formatting changes)**

```bash
git add -u
git commit -m "style: lint and format strategy engine code"
```

---

## Task 6: Brain update

Update brain files to reflect Layer 3 completion.

**Files:**
- Modify: `brain/architecture.md` — Mark Layer 3 COMPLETE with module descriptions
- Modify: `brain/codebase/index.md` — Add `scanner.py` and `game_manager.py` to module map, add any new gotchas

**Step 1: Update architecture.md**

Change Layer 3 line from:
```
3. **Strategy Engine** — identifies arbitrage opportunities
```
to:
```
3. **Strategy Engine** (Layer 3) — **COMPLETE**
   - `models/strategy.py` — `ArbPair` and `Opportunity` models
   - `scanner.py` — pure state machine: `ArbitrageScanner` (pair management, edge detection, opportunity tracking)
   - `game_manager.py` — async orchestrator: URL parsing, REST event fetch, feed subscription wiring
```

**Step 2: Update codebase/index.md**

Add to module map:
| `models/strategy.py` | Strategy data models | `ArbPair`, `Opportunity` |
| `scanner.py` | NO+NO arbitrage detection | `ArbitrageScanner` |
| `game_manager.py` | Game lifecycle from URLs | `GameManager`, `parse_kalshi_url` |

Add gotcha:
- **NO pricing uses YES bids:** To get the NO ask price (cheapest you can buy NO), use `100 - best_bid(ticker).price`. The `best_bid()` method returns the top of the YES side. `raw_edge = best_bid_a + best_bid_b - 100`.
- **Game events have exactly 2 markets:** Each game event on Kalshi has one contract per team. `GameManager.add_game()` validates this and raises `ValueError` if not.

**Step 3: Commit**

```bash
git add brain/
git commit -m "docs: update brain with Layer 3 (Strategy Engine) completion"
```

---

## Summary

| Task | Creates/Modifies | Tests |
|------|-----------------|-------|
| 1. Strategy models | `models/strategy.py`, `models/__init__.py` | — (verified by downstream tests) |
| 2. ArbitrageScanner | `scanner.py`, `test_scanner.py` | ~13 tests (pure state, no mocks) |
| 3. MarketFeed callback | `market_feed.py`, `test_market_feed.py` | 3 tests (async, mock callback) |
| 4. GameManager | `game_manager.py`, `test_game_manager.py` | ~12 tests (async, mock REST/feed/scanner) |
| 5. Lint + types | All new files | — |
| 6. Brain update | `brain/` | — |
