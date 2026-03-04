"""Tests for MarketFeed."""

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
    async def test_subscribe_calls_ws(self, feed: MarketFeed, mock_ws: KalshiWSClient) -> None:
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
            market_ticker="MKT-1",
            market_id="uuid-1",
            yes=[[65, 100]],
            no=[[35, 50]],
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
