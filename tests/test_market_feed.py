"""Tests for MarketFeed."""

# Tests in this file construct models with legacy wire-shape parameter
# names that the models' _migrate_fp validators remap to canonical
# bps/fp100 fields at runtime. Pyright doesn't see validator remapping as
# part of the constructor signature.
# pyright: reportCallIssue=false

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.market_feed import _ORDERBOOK_CHANNEL, MarketFeed
from talos.models.ws import OrderBookDelta, OrderBookSnapshot
from talos.orderbook import OrderBookManager
from talos.ws_client import KalshiWSClient


@pytest.fixture()
def mock_ws() -> KalshiWSClient:
    ws = MagicMock(spec=KalshiWSClient)
    ws.subscribe = AsyncMock()
    ws.unsubscribe = AsyncMock()
    ws.update_subscription = AsyncMock()
    ws.disconnect = AsyncMock()
    ws.listen = AsyncMock()
    ws.on_seq_gap = MagicMock()
    return ws


@pytest.fixture()
def mock_books() -> OrderBookManager:
    mgr = MagicMock(spec=OrderBookManager)
    return mgr


@pytest.fixture()
def feed(mock_ws: KalshiWSClient, mock_books: OrderBookManager) -> MarketFeed:
    return MarketFeed(ws_client=mock_ws, book_manager=mock_books)


class TestSubscribe:
    async def test_subscribe_calls_ws(self, feed: MarketFeed, mock_ws: KalshiWSClient) -> None:
        await feed.subscribe("MKT-1")
        mock_ws.subscribe.assert_called_once_with("orderbook_delta", "MKT-1")  # type: ignore[union-attr]

    async def test_subscribe_tracks_ticker(self, feed: MarketFeed) -> None:
        await feed.subscribe("MKT-1")
        assert "MKT-1" in feed.subscriptions


class TestUnsubscribe:
    async def test_unsubscribe_last_ticker_kills_sid(
        self, feed: MarketFeed, mock_ws: KalshiWSClient, mock_books: OrderBookManager
    ) -> None:
        """Unsubscribing the last ticker on a sid kills the whole subscription."""
        await feed.subscribe("MKT-1")
        # Simulate receiving a message that maps ticker to sid
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[65, 100]],
            no=[[35, 50]],
        )
        await feed._on_message(snapshot, sid=5, seq=1)
        await feed.unsubscribe("MKT-1")
        mock_ws.unsubscribe.assert_called_once_with([5])  # type: ignore[union-attr]
        mock_ws.update_subscription.assert_not_called()  # type: ignore[union-attr]

    async def test_unsubscribe_with_siblings_uses_update(
        self, feed: MarketFeed, mock_ws: KalshiWSClient, mock_books: OrderBookManager
    ) -> None:
        """Unsubscribing one ticker from a bulk sub uses update_subscription, not unsubscribe."""
        # Simulate bulk subscription: 2 tickers sharing sid=5
        feed._ticker_to_sid["MKT-1"] = 5
        feed._ticker_to_sid["MKT-2"] = 5
        feed._subscribed_tickers.update(["MKT-1", "MKT-2"])

        await feed.unsubscribe("MKT-1")
        # Should NOT kill the whole sid
        mock_ws.unsubscribe.assert_not_called()  # type: ignore[union-attr]
        # Should use update_subscription to remove just MKT-1
        mock_ws.update_subscription.assert_called_once_with(  # type: ignore[union-attr]
            5, ["MKT-1"], action="delete_markets"
        )
        # MKT-2 should still be mapped
        assert feed._ticker_to_sid.get("MKT-2") == 5

    async def test_unsubscribe_removes_from_book_manager(
        self, feed: MarketFeed, mock_books: OrderBookManager
    ) -> None:
        await feed.subscribe("MKT-1")
        await feed.unsubscribe("MKT-1")
        mock_books.remove.assert_called_once_with("MKT-1")  # type: ignore[union-attr]

    async def test_unsubscribe_removes_from_subscriptions(self, feed: MarketFeed) -> None:
        await feed.subscribe("MKT-1")
        await feed.unsubscribe("MKT-1")
        assert "MKT-1" not in feed.subscriptions


class TestMessageRouting:
    async def test_snapshot_routes_to_apply_snapshot(
        self, feed: MarketFeed, mock_books: OrderBookManager
    ) -> None:
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[65, 100]],
            no=[[35, 50]],
        )
        await feed._on_message(snapshot, sid=1, seq=1)
        mock_books.apply_snapshot.assert_called_once_with("MKT-1", snapshot)  # type: ignore[union-attr]

    async def test_delta_routes_to_apply_delta(
        self, feed: MarketFeed, mock_books: OrderBookManager
    ) -> None:
        delta = OrderBookDelta(
            market_ticker="MKT-1",
            market_id="uuid-1",
            price=65,
            delta=150,
            side="yes",
            ts="2026-03-03T12:00:00Z",
        )
        await feed._on_message(delta, sid=1, seq=2)
        mock_books.apply_delta.assert_called_once_with("MKT-1", delta, seq=2)  # type: ignore[union-attr]

    async def test_sid_mapping_learned_from_first_message(self, feed: MarketFeed) -> None:
        snapshot = OrderBookSnapshot(
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[],
            no=[],
        )
        await feed._on_message(snapshot, sid=7, seq=1)
        assert feed._ticker_to_sid.get("MKT-1") == 7


class TestStartStop:
    async def test_start_calls_listen(self, feed: MarketFeed, mock_ws: KalshiWSClient) -> None:
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
        assert call_args[0][0] == _ORDERBOOK_CHANNEL


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
        await feed._on_message(snapshot, sid=1, seq=1)


class TestSeqGapRecovery:
    def test_registers_seq_gap_callback_on_init(self, mock_ws: KalshiWSClient) -> None:
        mgr = MagicMock(spec=OrderBookManager)
        MarketFeed(ws_client=mock_ws, book_manager=mgr)
        mock_ws.on_seq_gap.assert_called_once()  # type: ignore[union-attr]

    async def test_seq_gap_resubscribes(self, feed: MarketFeed, mock_ws: KalshiWSClient) -> None:
        """On seq gap, should unsubscribe old sid and resubscribe to same channel+ticker."""
        # Simulate a known ticker-to-sid mapping
        feed._ticker_to_sid["MKT-1"] = 5

        await feed._on_seq_gap(5, "orderbook_delta")

        mock_ws.unsubscribe.assert_called_once_with([5])  # type: ignore[union-attr]
        mock_ws.subscribe.assert_called_once_with("orderbook_delta", "MKT-1")  # type: ignore[union-attr]
        # Old sid mapping should be cleared
        assert "MKT-1" not in feed._ticker_to_sid

    async def test_seq_gap_bulk_resubscribes_all_tickers(
        self, feed: MarketFeed, mock_ws: KalshiWSClient
    ) -> None:
        """On seq gap for a shared sid, should resubscribe ALL tickers, not just the first."""
        # Simulate bulk subscription: 3 tickers sharing a single sid
        feed._ticker_to_sid["MKT-1"] = 10
        feed._ticker_to_sid["MKT-2"] = 10
        feed._ticker_to_sid["MKT-3"] = 10
        # A different sid should be untouched
        feed._ticker_to_sid["MKT-OTHER"] = 99

        await feed._on_seq_gap(10, "orderbook_delta")

        mock_ws.unsubscribe.assert_called_once_with([10])  # type: ignore[union-attr]
        # Should use bulk subscribe with all 3 tickers
        call_args = mock_ws.subscribe.call_args  # type: ignore[union-attr]
        assert call_args[0][0] == "orderbook_delta"
        assert sorted(call_args[1]["market_tickers"]) == ["MKT-1", "MKT-2", "MKT-3"]
        # All 3 stale mappings should be cleared
        assert "MKT-1" not in feed._ticker_to_sid
        assert "MKT-2" not in feed._ticker_to_sid
        assert "MKT-3" not in feed._ticker_to_sid
        # Unrelated sid should be untouched
        assert feed._ticker_to_sid["MKT-OTHER"] == 99

    async def test_seq_gap_unknown_sid_no_crash(
        self, feed: MarketFeed, mock_ws: KalshiWSClient
    ) -> None:
        """If sid is unknown, should log warning but not crash."""
        await feed._on_seq_gap(999, "orderbook_delta")
        mock_ws.unsubscribe.assert_not_called()  # type: ignore[union-attr]
        mock_ws.subscribe.assert_not_called()  # type: ignore[union-attr]

    async def test_seq_gap_scoped_to_batch(
        self, feed: MarketFeed, mock_ws: KalshiWSClient
    ) -> None:
        """Unmapped tickers from unrelated batches must NOT be included in recovery.

        Regression: all globally unmapped tickers were swept into recovery for
        any failed sid, causing duplicate subscribes and extra snapshots.
        """
        # Simulate two bulk subscriptions in different batches
        feed._bulk_batches = [
            {"MKT-1", "MKT-2", "MKT-3"},   # batch 1 → sid 10
            {"MKT-X", "MKT-Y", "MKT-Z"},   # batch 2 → sid 20 (unrelated)
        ]
        feed._subscribed_tickers = {"MKT-1", "MKT-2", "MKT-3", "MKT-X", "MKT-Y", "MKT-Z"}
        # Only MKT-1 has learned sid 10; MKT-2/MKT-3 are still unmapped
        feed._ticker_to_sid["MKT-1"] = 10
        # MKT-X learned sid 20; MKT-Y/MKT-Z are unmapped (from batch 2)
        feed._ticker_to_sid["MKT-X"] = 20

        await feed._on_seq_gap(10, "orderbook_delta")

        # Should include MKT-1 (learned) + MKT-2, MKT-3 (unmapped from same batch)
        # Should NOT include MKT-Y, MKT-Z (unmapped but from batch 2)
        call_args = mock_ws.subscribe.call_args  # type: ignore[union-attr]
        resubscribed = set(call_args[1]["market_tickers"])
        assert resubscribed == {"MKT-1", "MKT-2", "MKT-3"}
        # Unrelated batch/sid must be untouched
        assert feed._ticker_to_sid["MKT-X"] == 20

    async def test_seq_gap_fallback_all_unmapped_when_no_learned(
        self, feed: MarketFeed, mock_ws: KalshiWSClient
    ) -> None:
        """When NO tickers have learned the failed sid, fall back to all unmapped.

        This handles the case where the entire subscription gapped before any
        snapshot arrived — we don't know which batch it belongs to.
        """
        feed._subscribed_tickers = {"MKT-A", "MKT-B"}
        # Both are unmapped (subscribed but no data received yet)

        await feed._on_seq_gap(42, "orderbook_delta")

        # Fallback: no learned tickers for sid 42, so include all unmapped
        mock_ws.unsubscribe.assert_called_once_with([42])  # type: ignore[union-attr]
        call_args = mock_ws.subscribe.call_args  # type: ignore[union-attr]
        resubscribed = set(call_args[1]["market_tickers"])
        assert resubscribed == {"MKT-A", "MKT-B"}
