"""Tests for PositionFeed."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.models.ws import MarketPositionMessage
from talos.position_feed import PositionFeed
from talos.ws_client import KalshiWSClient


@pytest.fixture()
def mock_ws() -> KalshiWSClient:
    ws = MagicMock(spec=KalshiWSClient)
    ws.subscribe = AsyncMock()
    ws.on_message = MagicMock()
    return ws


@pytest.fixture()
def feed(mock_ws: KalshiWSClient) -> PositionFeed:
    return PositionFeed(ws_client=mock_ws)


class TestInit:
    def test_registers_market_positions_channel(self, mock_ws: KalshiWSClient) -> None:
        PositionFeed(ws_client=mock_ws)
        mock_ws.on_message.assert_called_once()  # type: ignore[union-attr]
        channel = mock_ws.on_message.call_args[0][0]  # type: ignore[union-attr]
        assert channel == "market_positions"

    def test_sid_starts_as_none(self, feed: PositionFeed) -> None:
        assert feed._sid is None

    def test_latest_starts_empty(self, feed: PositionFeed) -> None:
        assert feed._latest == {}


class TestMessageRouting:
    async def test_caches_latest_position(self, feed: PositionFeed) -> None:
        msg = MarketPositionMessage(market_ticker="MKT-1", position=-10, fees_paid=5)
        await feed._on_message(msg, sid=10, seq=1)
        assert feed.get_position("MKT-1") is msg

    async def test_overwrites_with_newer_data(self, feed: PositionFeed) -> None:
        msg1 = MarketPositionMessage(market_ticker="MKT-1", position=-10)
        msg2 = MarketPositionMessage(market_ticker="MKT-1", position=-20)
        await feed._on_message(msg1, sid=10, seq=1)
        await feed._on_message(msg2, sid=10, seq=2)
        assert feed.get_position("MKT-1") is msg2

    async def test_get_position_returns_none_for_unknown(self, feed: PositionFeed) -> None:
        assert feed.get_position("UNKNOWN") is None

    async def test_fires_callback(self, feed: PositionFeed) -> None:
        callback = MagicMock()
        feed.on_position = callback
        msg = MarketPositionMessage(market_ticker="MKT-1", position=-10)
        await feed._on_message(msg, sid=10, seq=1)
        callback.assert_called_once_with(msg)

    async def test_no_callback_no_error(self, feed: PositionFeed) -> None:
        msg = MarketPositionMessage(market_ticker="MKT-1", position=-10)
        await feed._on_message(msg, sid=10, seq=1)

    async def test_sid_learned_from_first_message(self, feed: PositionFeed) -> None:
        msg = MarketPositionMessage(market_ticker="MKT-1")
        await feed._on_message(msg, sid=10, seq=1)
        assert feed._sid == 10

    async def test_sid_not_overwritten_by_later_message(self, feed: PositionFeed) -> None:
        msg1 = MarketPositionMessage(market_ticker="MKT-1")
        msg2 = MarketPositionMessage(market_ticker="MKT-2")
        await feed._on_message(msg1, sid=10, seq=1)
        await feed._on_message(msg2, sid=20, seq=2)
        assert feed._sid == 10


class TestSubscribe:
    async def test_subscribe_global(self, feed: PositionFeed, mock_ws: KalshiWSClient) -> None:
        await feed.subscribe()
        mock_ws.subscribe.assert_called_once_with("market_positions")  # type: ignore[union-attr]
