"""Tests for OrderBookManager."""

from __future__ import annotations

import time
from typing import Literal

import pytest

from talos.models.ws import OrderBookDelta, OrderBookSnapshot
from talos.orderbook import _STALE_THRESHOLD, LocalOrderBook, OrderBookManager


class TestLocalOrderBookModel:
    def test_defaults(self) -> None:
        book = LocalOrderBook(ticker="MKT-1")
        assert book.ticker == "MKT-1"
        assert book.yes == []
        assert book.no == []
        assert book.last_update == 0.0
        assert book.stale is False  # no update yet → not stale

    def test_stale_when_old(self) -> None:
        book = LocalOrderBook(ticker="MKT-1", last_update=time.time() - _STALE_THRESHOLD - 1)
        assert book.stale is True

    def test_not_stale_when_recent(self) -> None:
        book = LocalOrderBook(ticker="MKT-1", last_update=time.time())
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
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[65, 100]],
            no=[[35, 50]],
        )
        snap2 = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[70, 300]],
            no=[[30, 200]],
        )
        manager.apply_snapshot("MKT-1", snap1)
        manager.apply_snapshot("MKT-1", snap2)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert len(book.yes) == 1
        assert book.yes[0].price == 70

    def test_snapshot_sets_last_update(self, manager: OrderBookManager) -> None:
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[65, 100]],
            no=[],
        )
        before = time.time()
        manager.apply_snapshot("MKT-1", snapshot)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert book.last_update >= before
        assert book.stale is False


class TestApplyDelta:
    @pytest.fixture()
    def manager(self) -> OrderBookManager:
        mgr = OrderBookManager()
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[65, 100], [60, 200]],
            no=[[35, 150]],
        )
        mgr.apply_snapshot("MKT-1", snapshot)
        return mgr

    def _make_delta(
        self, *, price: int, delta: int, side: Literal["yes", "no"], ticker: str = "MKT-1"
    ) -> OrderBookDelta:
        return OrderBookDelta(
            market_ticker=ticker,
            market_id="uuid-1",
            price=price,
            delta=delta,
            side=side,
            ts="2026-03-03T12:00:00Z",
        )

    def test_accumulates_into_existing_level(self, manager: OrderBookManager) -> None:
        # YES@65 starts at qty=100, delta +50 → 150
        d = self._make_delta(price=65, delta=50, side="yes")
        manager.apply_delta("MKT-1", d, seq=1)
        book = manager.get_book("MKT-1")
        assert book is not None
        level = next(lvl for lvl in book.yes if lvl.price == 65)
        assert level.quantity == 150

    def test_insert_new_level(self, manager: OrderBookManager) -> None:
        d = self._make_delta(price=62, delta=50, side="yes")
        manager.apply_delta("MKT-1", d, seq=1)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert len(book.yes) == 3
        assert [lvl.price for lvl in book.yes] == [65, 62, 60]

    def test_removes_level_when_qty_hits_zero(self, manager: OrderBookManager) -> None:
        # YES@60 starts at qty=200, delta -200 → removed
        d = self._make_delta(price=60, delta=-200, side="yes")
        manager.apply_delta("MKT-1", d, seq=1)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert len(book.yes) == 1
        assert book.yes[0].price == 65

    def test_removes_level_when_qty_goes_negative(self, manager: OrderBookManager) -> None:
        # YES@60 starts at qty=200, delta -300 → removed (not stored as -100)
        d = self._make_delta(price=60, delta=-300, side="yes")
        manager.apply_delta("MKT-1", d, seq=1)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert len(book.yes) == 1

    def test_applies_to_no_side(self, manager: OrderBookManager) -> None:
        # NO@35 starts at qty=150, delta +100 → 250
        d = self._make_delta(price=35, delta=100, side="no")
        manager.apply_delta("MKT-1", d, seq=1)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert book.no[0].quantity == 250

    def test_delta_updates_last_update(self, manager: OrderBookManager) -> None:
        before = time.time()
        d = self._make_delta(price=65, delta=110, side="yes")
        manager.apply_delta("MKT-1", d, seq=1)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert book.last_update >= before

    def test_unknown_ticker_buffered(self, manager: OrderBookManager) -> None:
        """Deltas for unknown tickers are buffered, not dropped."""
        d = self._make_delta(price=50, delta=100, side="yes", ticker="UNKNOWN")
        manager.apply_delta("UNKNOWN", d, seq=1)
        # Not yet in books (no snapshot), but buffered internally
        assert manager.get_book("UNKNOWN") is None
        assert "UNKNOWN" in manager._pending_deltas
        assert len(manager._pending_deltas["UNKNOWN"]) == 1

    def test_negative_delta_nonexistent_level_is_noop(self, manager: OrderBookManager) -> None:
        d = self._make_delta(price=99, delta=-50, side="yes")
        manager.apply_delta("MKT-1", d, seq=1)
        book = manager.get_book("MKT-1")
        assert book is not None
        assert len(book.yes) == 2


class TestQueryMethods:
    @pytest.fixture()
    def manager(self) -> OrderBookManager:
        mgr = OrderBookManager()
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[65, 100], [60, 200]],
            no=[[35, 150], [40, 50]],
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
            market_ticker="EMPTY",
            market_id="uuid-2",
            yes=[],
            no=[],
        )
        manager.apply_snapshot("EMPTY", snap)
        assert manager.best_bid("EMPTY") is None

    def test_best_ask(self, manager: OrderBookManager) -> None:
        ask = manager.best_ask("MKT-1")
        assert ask is not None
        assert ask.price == 40
        assert ask.quantity == 50

    def test_best_ask_unknown_ticker(self, manager: OrderBookManager) -> None:
        assert manager.best_ask("NOPE") is None

    def test_best_ask_empty_book(self, manager: OrderBookManager) -> None:
        snap = OrderBookSnapshot(
            market_ticker="EMPTY",
            market_id="uuid-2",
            yes=[],
            no=[],
        )
        manager.apply_snapshot("EMPTY", snap)
        assert manager.best_ask("EMPTY") is None

    def test_remove(self, manager: OrderBookManager) -> None:
        manager.remove("MKT-1")
        assert manager.get_book("MKT-1") is None

    def test_remove_nonexistent_is_noop(self, manager: OrderBookManager) -> None:
        manager.remove("NOPE")

    def test_tickers(self, manager: OrderBookManager) -> None:
        assert manager.tickers == {"MKT-1"}
        snap2 = OrderBookSnapshot(
            market_ticker="MKT-2",
            market_id="uuid-2",
            yes=[],
            no=[],
        )
        manager.apply_snapshot("MKT-2", snap2)
        assert manager.tickers == {"MKT-1", "MKT-2"}

    def test_tickers_after_remove(self, manager: OrderBookManager) -> None:
        manager.remove("MKT-1")
        assert manager.tickers == set()


class TestDeltaBuffering:
    """Tests for buffering deltas that arrive before the snapshot."""

    @staticmethod
    def _make_delta(
        *, price: int, delta: int, side: Literal["yes", "no"], ticker: str = "MKT-1"
    ) -> OrderBookDelta:
        return OrderBookDelta(
            market_ticker=ticker,
            market_id="uuid-1",
            price=price,
            delta=delta,
            side=side,
            ts="2026-03-03T12:00:00Z",
        )

    def test_buffered_deltas_applied_on_snapshot(self) -> None:
        """Deltas arriving before snapshot are replayed when snapshot arrives."""
        mgr = OrderBookManager()
        # Delta arrives first — no snapshot yet
        d = self._make_delta(price=65, delta=50, side="yes")
        mgr.apply_delta("MKT-1", d, seq=1)
        assert mgr.get_book("MKT-1") is None

        # Snapshot arrives — should replay the buffered delta
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[65, 100]],
            no=[],
        )
        mgr.apply_snapshot("MKT-1", snapshot)
        book = mgr.get_book("MKT-1")
        assert book is not None
        # 100 from snapshot + 50 from buffered delta
        assert book.yes[0].price == 65
        assert book.yes[0].quantity == 150

    def test_multiple_buffered_deltas_applied_in_order(self) -> None:
        """Multiple buffered deltas are replayed in arrival order."""
        mgr = OrderBookManager()
        d1 = self._make_delta(price=65, delta=50, side="yes")
        d2 = self._make_delta(price=60, delta=30, side="yes")
        d3 = self._make_delta(price=65, delta=-20, side="yes")
        mgr.apply_delta("MKT-1", d1, seq=1)
        mgr.apply_delta("MKT-1", d2, seq=2)
        mgr.apply_delta("MKT-1", d3, seq=3)

        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[65, 100]],
            no=[],
        )
        mgr.apply_snapshot("MKT-1", snapshot)
        book = mgr.get_book("MKT-1")
        assert book is not None
        # 65: 100 + 50 - 20 = 130
        level_65 = next(lvl for lvl in book.yes if lvl.price == 65)
        assert level_65.quantity == 130
        # 60: new level from delta = 30
        level_60 = next(lvl for lvl in book.yes if lvl.price == 60)
        assert level_60.quantity == 30

    def test_buffer_cleared_after_snapshot(self) -> None:
        """Pending buffer is cleared once snapshot is applied."""
        mgr = OrderBookManager()
        d = self._make_delta(price=65, delta=50, side="yes")
        mgr.apply_delta("MKT-1", d, seq=1)
        assert "MKT-1" in mgr._pending_deltas

        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[65, 100]],
            no=[],
        )
        mgr.apply_snapshot("MKT-1", snapshot)
        assert "MKT-1" not in mgr._pending_deltas

    def test_remove_clears_pending_buffer(self) -> None:
        """Removing a ticker also clears its pending delta buffer."""
        mgr = OrderBookManager()
        d = self._make_delta(price=65, delta=50, side="yes")
        mgr.apply_delta("MKT-1", d, seq=1)
        assert "MKT-1" in mgr._pending_deltas
        mgr.remove("MKT-1")
        assert "MKT-1" not in mgr._pending_deltas

    def test_buffered_deltas_for_multiple_tickers(self) -> None:
        """Buffering works independently per ticker."""
        mgr = OrderBookManager()
        d1 = self._make_delta(price=65, delta=50, side="yes", ticker="MKT-1")
        d2 = self._make_delta(price=40, delta=80, side="no", ticker="MKT-2")
        mgr.apply_delta("MKT-1", d1, seq=1)
        mgr.apply_delta("MKT-2", d2, seq=1)

        # Only snapshot MKT-1
        snap1 = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[65, 100]],
            no=[],
        )
        mgr.apply_snapshot("MKT-1", snap1)
        book1 = mgr.get_book("MKT-1")
        assert book1 is not None
        assert book1.yes[0].quantity == 150
        # MKT-2 still buffered
        assert mgr.get_book("MKT-2") is None
        assert "MKT-2" in mgr._pending_deltas

    def test_no_buffered_deltas_is_noop(self) -> None:
        """Snapshot with no pending deltas works normally (no crash)."""
        mgr = OrderBookManager()
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[65, 100]],
            no=[],
        )
        mgr.apply_snapshot("MKT-1", snapshot)
        book = mgr.get_book("MKT-1")
        assert book is not None
        assert book.yes[0].quantity == 100


class TestStaleTickers:
    def test_fresh_book_not_stale(self) -> None:
        mgr = OrderBookManager()
        mgr.apply_snapshot(
            "MKT-1",
            OrderBookSnapshot(market_ticker="MKT-1", market_id="m1", yes=[], no=[[50, 10]]),
        )
        assert mgr.stale_tickers() == []

    def test_old_book_is_stale(self) -> None:
        mgr = OrderBookManager()
        mgr.apply_snapshot(
            "MKT-1",
            OrderBookSnapshot(market_ticker="MKT-1", market_id="m1", yes=[], no=[[50, 10]]),
        )
        # Simulate time passing by backdating last_update
        book = mgr.get_book("MKT-1")
        assert book is not None
        book.last_update = time.time() - _STALE_THRESHOLD - 1
        assert mgr.stale_tickers() == ["MKT-1"]

    def test_snapshot_refreshes_staleness(self) -> None:
        mgr = OrderBookManager()
        mgr.apply_snapshot(
            "MKT-1",
            OrderBookSnapshot(market_ticker="MKT-1", market_id="m1", yes=[], no=[[50, 10]]),
        )
        # Backdate to make stale
        book = mgr.get_book("MKT-1")
        assert book is not None
        book.last_update = time.time() - _STALE_THRESHOLD - 1
        assert mgr.stale_tickers() == ["MKT-1"]

        # Fresh snapshot clears stale
        mgr.apply_snapshot(
            "MKT-1",
            OrderBookSnapshot(market_ticker="MKT-1", market_id="m1", yes=[], no=[[50, 20]]),
        )
        assert mgr.stale_tickers() == []

    def test_delta_refreshes_staleness(self) -> None:
        mgr = OrderBookManager()
        mgr.apply_snapshot(
            "MKT-1",
            OrderBookSnapshot(market_ticker="MKT-1", market_id="m1", yes=[], no=[[50, 10]]),
        )
        book = mgr.get_book("MKT-1")
        assert book is not None
        book.last_update = time.time() - _STALE_THRESHOLD - 1
        assert mgr.stale_tickers() == ["MKT-1"]

        # Delta refreshes timestamp
        mgr.apply_delta(
            "MKT-1",
            OrderBookDelta(
                market_ticker="MKT-1", market_id="m1", price=50, delta=5, side="no", ts="0"
            ),
            seq=1,
        )
        assert mgr.stale_tickers() == []
