# Top-of-Market Tracking Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Alert the user when resting NO bids are no longer at the best price on the book (penny jumped).

**Architecture:** Pure `TopOfMarketTracker` state machine reads orderbook state, compares against resting order prices, fires callback on transitions. Wired via `on_book_update` dispatcher. TUI shows toast notifications and warning symbols in the Q column.

**Tech Stack:** Python 3.12+, Pydantic v2, structlog, Textual, pytest

---

### Task 1: TopOfMarketTracker — core state and detection

**Files:**
- Create: `src/talos/top_of_market.py`
- Create: `tests/test_top_of_market.py`

**Step 1: Write failing tests for basic detection**

```python
"""Tests for TopOfMarketTracker."""

from __future__ import annotations

from talos.orderbook import OrderBookManager
from talos.models.ws import OrderBookSnapshot
from talos.models.order import Order
from talos.models.strategy import ArbPair
from talos.top_of_market import TopOfMarketTracker


def _snapshot(yes: list[list[int]], no: list[list[int]]) -> OrderBookSnapshot:
    return OrderBookSnapshot(market_ticker="", yes=yes, no=no)


def _order(
    ticker: str,
    no_price: int,
    *,
    remaining: int = 5,
    filled: int = 0,
    status: str = "resting",
) -> Order:
    return Order(
        order_id=f"ord-{ticker}-{no_price}",
        ticker=ticker,
        action="buy",
        side="no",
        no_price=no_price,
        initial_count=remaining + filled,
        remaining_count=remaining,
        fill_count=filled,
        status=status,
    )


PAIR = ArbPair(event_ticker="EVT-A", ticker_a="MKT-A", ticker_b="MKT-B")


def _make_tracker() -> tuple[OrderBookManager, TopOfMarketTracker]:
    books = OrderBookManager()
    tracker = TopOfMarketTracker(books)
    return books, tracker


class TestIsAtTop:
    def test_at_top_when_price_matches_best(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 10]]))
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])
        tracker.check("MKT-A")
        assert tracker.is_at_top("MKT-A") is True

    def test_not_at_top_when_jumped(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[48, 5], [47, 10]]))
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])
        tracker.check("MKT-A")
        assert tracker.is_at_top("MKT-A") is False

    def test_none_when_no_resting_orders(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 10]]))
        tracker.check("MKT-A")
        assert tracker.is_at_top("MKT-A") is None

    def test_uses_highest_resting_price(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 10]]))
        orders = [_order("MKT-A", 45), _order("MKT-A", 47)]
        tracker.update_orders(orders, [PAIR])
        tracker.check("MKT-A")
        assert tracker.is_at_top("MKT-A") is True

    def test_partially_filled_still_tracked(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[48, 5], [47, 10]]))
        tracker.update_orders(
            [_order("MKT-A", 47, remaining=2, filled=3)], [PAIR]
        )
        tracker.check("MKT-A")
        assert tracker.is_at_top("MKT-A") is False

    def test_fully_filled_not_tracked(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[48, 5]]))
        tracker.update_orders(
            [_order("MKT-A", 47, remaining=0, filled=5, status="executed")],
            [PAIR],
        )
        tracker.check("MKT-A")
        assert tracker.is_at_top("MKT-A") is None

    def test_resting_price_query(self) -> None:
        books, tracker = _make_tracker()
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])
        assert tracker.resting_price("MKT-A") == 47
        assert tracker.resting_price("MKT-B") is None
```

**Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_top_of_market.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'talos.top_of_market'`

**Step 3: Write minimal implementation**

```python
"""Top-of-market tracking for resting NO bids."""

from __future__ import annotations

from collections.abc import Callable

import structlog

from talos.models.order import ACTIVE_STATUSES, Order
from talos.models.strategy import ArbPair
from talos.orderbook import OrderBookManager

logger = structlog.get_logger()


class TopOfMarketTracker:
    """Detects when resting NO bids are no longer at the best book price.

    Pure state machine — no async, no I/O. Receives order data from polling
    and checks against live orderbook state on every delta.
    """

    def __init__(self, book_manager: OrderBookManager) -> None:
        self._books = book_manager
        self._resting: dict[str, int] = {}  # ticker -> highest resting NO price
        self._at_top: dict[str, bool] = {}  # ticker -> is at top
        self.on_change: Callable[[str, bool], None] | None = None

    def update_orders(self, orders: list[Order], pairs: list[ArbPair]) -> None:
        """Refresh resting order prices from polled order data.

        Filters to resting NO buys on tracked pair tickers. When multiple
        orders exist on the same ticker, keeps the highest NO price.
        """
        tracked: set[str] = set()
        for pair in pairs:
            tracked.add(pair.ticker_a)
            tracked.add(pair.ticker_b)

        new_resting: dict[str, int] = {}
        for order in orders:
            if order.side != "no" or order.action != "buy":
                continue
            if order.status not in ACTIVE_STATUSES:
                continue
            if order.remaining_count <= 0:
                continue
            if order.ticker not in tracked:
                continue
            prev = new_resting.get(order.ticker, 0)
            new_resting[order.ticker] = max(prev, order.no_price)

        # Clear state for tickers that no longer have resting orders
        for ticker in list(self._resting.keys()):
            if ticker not in new_resting:
                self._resting.pop(ticker)
                self._at_top.pop(ticker, None)

        self._resting = new_resting

    def check(self, ticker: str) -> None:
        """Compare resting price against current best book price.

        Called on every orderbook delta. Fires ``on_change`` callback
        only when the at-top state transitions.
        """
        resting_price = self._resting.get(ticker)
        if resting_price is None:
            return

        best = self._books.best_ask(ticker)
        if best is None:
            return

        now_at_top = best.price <= resting_price
        was_at_top = self._at_top.get(ticker)

        self._at_top[ticker] = now_at_top

        if was_at_top is not None and now_at_top != was_at_top:
            logger.info(
                "top_of_market_change",
                ticker=ticker,
                at_top=now_at_top,
                resting=resting_price,
                book_top=best.price,
            )
            if self.on_change:
                self.on_change(ticker, now_at_top)

    def is_at_top(self, ticker: str) -> bool | None:
        """Query current top-of-market state for a ticker.

        Returns ``None`` if no resting orders on this ticker.
        """
        if ticker not in self._resting:
            return None
        return self._at_top.get(ticker)

    def resting_price(self, ticker: str) -> int | None:
        """Query the highest resting NO price for a ticker."""
        return self._resting.get(ticker)
```

**Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_top_of_market.py -v`
Expected: All 7 tests PASS

**Step 5: Commit**

```bash
git add src/talos/top_of_market.py tests/test_top_of_market.py
git commit -m "feat: add TopOfMarketTracker with basic detection"
```

---

### Task 2: Callback transition tests

**Files:**
- Modify: `tests/test_top_of_market.py`

**Step 1: Write failing tests for callback behavior**

Add to `tests/test_top_of_market.py`:

```python
class TestCallbackTransitions:
    def test_callback_fires_on_loss(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 10]]))
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])
        tracker.check("MKT-A")  # initial state: at top

        changes: list[tuple[str, bool]] = []
        tracker.on_change = lambda t, at: changes.append((t, at))

        # Someone penny jumps at 48
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[48, 5], [47, 10]]))
        tracker.check("MKT-A")

        assert changes == [("MKT-A", False)]

    def test_callback_fires_on_regain(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[48, 5], [47, 10]]))
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])
        tracker.check("MKT-A")  # initial: not at top

        changes: list[tuple[str, bool]] = []
        tracker.on_change = lambda t, at: changes.append((t, at))

        # 48 level gets consumed
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 10]]))
        tracker.check("MKT-A")

        assert changes == [("MKT-A", True)]

    def test_no_duplicate_callbacks(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 10]]))
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])
        tracker.check("MKT-A")  # initial: at top

        changes: list[tuple[str, bool]] = []
        tracker.on_change = lambda t, at: changes.append((t, at))

        # Book updates but top doesn't change
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 15]]))
        tracker.check("MKT-A")
        tracker.check("MKT-A")

        assert changes == []

    def test_no_callback_on_first_check(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[48, 5], [47, 10]]))
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])

        changes: list[tuple[str, bool]] = []
        tracker.on_change = lambda t, at: changes.append((t, at))

        tracker.check("MKT-A")  # first check — sets state, no transition

        assert changes == []
        assert tracker.is_at_top("MKT-A") is False

    def test_order_removed_clears_state(self) -> None:
        books, tracker = _make_tracker()
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[47, 10]]))
        tracker.update_orders([_order("MKT-A", 47)], [PAIR])
        tracker.check("MKT-A")

        # Orders cleared
        tracker.update_orders([], [PAIR])
        assert tracker.is_at_top("MKT-A") is None
        assert tracker.resting_price("MKT-A") is None
```

**Step 2: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_top_of_market.py -v`
Expected: All 12 tests PASS (the implementation from Task 1 should handle these)

**Step 3: Commit**

```bash
git add tests/test_top_of_market.py
git commit -m "test: add callback transition tests for TopOfMarketTracker"
```

---

### Task 3: Wire tracker into __main__.py

**Files:**
- Modify: `src/talos/__main__.py:44-62` (inside `main()`)

**Step 1: Add tracker creation and wiring**

In `__main__.py`, after `scanner = ArbitrageScanner(books)` (line 58), add the tracker. Replace the single `feed.on_book_update = scanner.scan` with a dispatcher.

The `main()` function should look like this after the change (from the import block through to `app.run()`):

```python
    from talos.auth import KalshiAuth
    from talos.game_manager import GameManager
    from talos.market_feed import MarketFeed
    from talos.orderbook import OrderBookManager
    from talos.persistence import load_saved_games, save_games
    from talos.rest_client import KalshiRESTClient
    from talos.scanner import ArbitrageScanner
    from talos.top_of_market import TopOfMarketTracker
    from talos.ui.app import TalosApp
    from talos.ws_client import KalshiWSClient

    auth = KalshiAuth(config.key_id, config.private_key_path)
    rest = KalshiRESTClient(auth, config)
    ws = KalshiWSClient(auth, config)
    books = OrderBookManager()
    feed = MarketFeed(ws, books)
    scanner = ArbitrageScanner(books)
    tracker = TopOfMarketTracker(books)
    game_mgr = GameManager(rest, feed, scanner)

    # Wire scanner + tracker to book updates
    def on_book_update(ticker: str) -> None:
        scanner.scan(ticker)
        tracker.check(ticker)

    feed.on_book_update = on_book_update

    # Wire game persistence
    saved_games = load_saved_games()
    game_mgr.on_change = lambda: save_games(
        [p.event_ticker for p in game_mgr.active_games]
    )

    app = TalosApp(
        scanner=scanner,
        game_manager=game_mgr,
        rest_client=rest,
        market_feed=feed,
        tracker=tracker,
        initial_games=saved_games,
    )
    app.run()
```

**Step 2: Commit**

```bash
git add src/talos/__main__.py
git commit -m "feat: wire TopOfMarketTracker into startup and book updates"
```

---

### Task 4: TUI integration — inject tracker, update orders, show alerts

**Files:**
- Modify: `src/talos/ui/app.py`

**Step 1: Add tracker to TalosApp constructor**

In `TalosApp.__init__`, add `tracker` parameter and store it:

```python
    def __init__(
        self,
        *,
        scanner: ArbitrageScanner | None = None,
        game_manager: GameManager | None = None,
        rest_client: KalshiRESTClient | None = None,
        market_feed: MarketFeed | None = None,
        tracker: TopOfMarketTracker | None = None,
        initial_games: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._scanner = scanner
        self._game_manager = game_manager
        self._rest = rest_client
        self._feed = market_feed
        self._tracker = tracker
        self._initial_games = initial_games or []
        self._queue_cache: dict[str, int] = {}
        self._orders_cache: list[Order] = []
        self._cpm = CPMTracker()
```

Add import at top of file:

```python
from talos.top_of_market import TopOfMarketTracker
```

**Step 2: Wire tracker callback in `on_mount`**

Add after existing timer setup in `on_mount`:

```python
    def on_mount(self) -> None:
        """Start polling timers and WebSocket feed."""
        if self._scanner is not None:
            self.set_interval(0.5, self.refresh_opportunities)
        if self._rest is not None:
            self.set_interval(10.0, self.refresh_account)
            self.set_interval(3.0, self.refresh_queue_positions)
            self.set_interval(10.0, self.refresh_trades)
        if self._tracker is not None:
            self._tracker.on_change = self._on_top_of_market_change
        if self._feed is not None:
            self._start_feed()
```

**Step 3: Add the callback handler and toast**

```python
    def _on_top_of_market_change(self, ticker: str, at_top: bool) -> None:
        """Handle top-of-market state transition — show toast."""
        if self._tracker is None:
            return
        resting = self._tracker.resting_price(ticker)
        if at_top:
            self.notify(
                f"Back at top: {ticker} ({resting}c)",
                severity="information",
                timeout=10,
            )
        else:
            books = self._tracker._books
            best = books.best_ask(ticker)
            top_price = best.price if best else "?"
            self.notify(
                f"Jumped: {ticker} (you: {resting}c, top: {top_price}c)",
                severity="warning",
                timeout=15,
            )
```

**Step 4: Feed orders into tracker from `refresh_account`**

In `refresh_account`, after `self._orders_cache = orders` (around line 144), add:

```python
            # Update top-of-market tracker with current orders
            if self._tracker is not None and self._scanner is not None:
                self._tracker.update_orders(orders, self._scanner.pairs)
```

**Step 5: Commit**

```bash
git add src/talos/ui/app.py
git commit -m "feat: wire TopOfMarketTracker into TUI with toast alerts"
```

---

### Task 5: Table visual indicator

**Files:**
- Modify: `src/talos/ui/widgets.py`
- Modify: `src/talos/ui/app.py`

**Step 1: Pass tracker to OpportunitiesTable refresh**

In `OpportunitiesTable.refresh_from_scanner`, add an optional `tracker` parameter. In the Q-A / Q-B column formatting, prefix with a warning symbol when not at top.

Change the method signature in `widgets.py`:

```python
    def refresh_from_scanner(
        self,
        scanner: ArbitrageScanner | None,
        tracker: TopOfMarketTracker | None = None,
    ) -> None:
```

Add import at top of `widgets.py`:

```python
from talos.top_of_market import TopOfMarketTracker
```

In the row-building loop, after computing `q_a` and `q_b`, add top-of-market warning:

```python
                # Top-of-market warning
                if tracker is not None:
                    if tracker.is_at_top(opp.ticker_a) is False:
                        q_a = f"!! {q_a}"
                    if tracker.is_at_top(opp.ticker_b) is False:
                        q_b = f"!! {q_b}"
```

This goes right after `q_a` and `q_b` are computed (after the `if pos is not None:` / `else:` blocks, but a cleaner place is inside the `if pos is not None:` block right after lines 140-141, and also in the `else` block after `q_b = "---"`). Actually, since top-of-market applies regardless of position data, add it after both branches converge, right before `row_data` is assembled:

```python
            # Top-of-market warning (applies regardless of position data)
            if tracker is not None:
                if tracker.is_at_top(opp.ticker_a) is False:
                    q_a = f"!! {q_a}"
                if tracker.is_at_top(opp.ticker_b) is False:
                    q_b = f"!! {q_b}"

            row_data = (
                ...
            )
```

**Step 2: Update the call site in `app.py`**

In `TalosApp.refresh_opportunities`:

```python
    def refresh_opportunities(self) -> None:
        """Update the opportunities table from scanner state."""
        table = self.query_one(OpportunitiesTable)
        table.refresh_from_scanner(self._scanner, self._tracker)
```

**Step 3: Commit**

```bash
git add src/talos/ui/widgets.py src/talos/ui/app.py
git commit -m "feat: show !! warning in Q column when penny jumped"
```

---

### Task 6: Integration test

**Files:**
- Modify: `tests/test_top_of_market.py`

**Step 1: Write integration test for table warning indicator**

Add to `tests/test_top_of_market.py`:

```python
class TestTableIntegration:
    def test_warning_prefix_in_q_column(self) -> None:
        """Q column shows !! prefix when not at top of market."""
        books = OrderBookManager()
        scanner = ArbitrageScanner(books)
        tracker = TopOfMarketTracker(books)

        scanner.add_pair("EVT-A", "MKT-A", "MKT-B")

        # Set up orderbook: MKT-A has been jumped, MKT-B is at top
        books.apply_snapshot("MKT-A", _snapshot(yes=[], no=[[48, 5], [47, 10]]))
        books.apply_snapshot("MKT-B", _snapshot(yes=[], no=[[45, 10]]))
        scanner.scan("MKT-A")
        scanner.scan("MKT-B")

        # Set resting orders
        orders = [_order("MKT-A", 47), _order("MKT-B", 45)]
        tracker.update_orders(orders, scanner.pairs)
        tracker.check("MKT-A")
        tracker.check("MKT-B")

        assert tracker.is_at_top("MKT-A") is False
        assert tracker.is_at_top("MKT-B") is True
```

**Step 2: Run all top-of-market tests**

Run: `.venv/Scripts/python -m pytest tests/test_top_of_market.py -v`
Expected: All tests PASS

**Step 3: Commit**

```bash
git add tests/test_top_of_market.py
git commit -m "test: add integration test for top-of-market table display"
```

---

### Task 7: Lint, type-check, and full test suite

**Files:** None (verification only)

**Step 1: Run ruff lint**

Run: `.venv/Scripts/python -m ruff check src/talos/top_of_market.py tests/test_top_of_market.py`
Expected: No issues. Fix any that arise.

**Step 2: Run ruff format**

Run: `.venv/Scripts/python -m ruff format src/talos/top_of_market.py tests/test_top_of_market.py`

**Step 3: Run pyright**

Run: `.venv/Scripts/python -m pyright src/talos/top_of_market.py`
Expected: No errors. Fix any that arise.

**Step 4: Run full test suite**

Run: `.venv/Scripts/python -m pytest -x`
Expected: All tests PASS

**Step 5: Commit any lint/type fixes**

```bash
git add -u
git commit -m "style: lint and type-check top-of-market tracking"
```

---

### Task 8: Update brain docs

**Files:**
- Modify: `brain/architecture.md`
- Modify: `brain/codebase/index.md`

**Step 1: Add top_of_market.py to codebase index module map**

Add row to the module map table in `brain/codebase/index.md`:

```
| `top_of_market.py` | Top-of-market detection for resting NO bids | `TopOfMarketTracker` |
```

**Step 2: Update architecture.md**

Note that execution layer work has begun, or add a note about top-of-market tracking under the existing Layer 4 entry.

**Step 3: Commit**

```bash
git add brain/architecture.md brain/codebase/index.md
git commit -m "docs: add top-of-market tracker to brain vault"
```
