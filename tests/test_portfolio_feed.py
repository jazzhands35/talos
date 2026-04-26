"""Tests for PortfolioFeed."""

# Tests in this file construct models with legacy wire-shape parameter
# names that the models' _migrate_fp validators remap to canonical
# bps/fp100 fields at runtime. Pyright doesn't see validator remapping as
# part of the constructor signature.
# pyright: reportCallIssue=false

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from talos.models.ws import FillMessage, UserOrderMessage
from talos.portfolio_feed import PortfolioFeed
from talos.ws_client import KalshiWSClient


@pytest.fixture()
def mock_ws() -> KalshiWSClient:
    ws = MagicMock(spec=KalshiWSClient)
    ws.subscribe = AsyncMock()
    ws.unsubscribe = AsyncMock()
    ws.update_subscription = AsyncMock()
    ws.on_message = MagicMock()
    return ws


@pytest.fixture()
def feed(mock_ws: KalshiWSClient) -> PortfolioFeed:
    return PortfolioFeed(ws_client=mock_ws)


class TestInit:
    def test_registers_both_channel_callbacks(self, mock_ws: KalshiWSClient) -> None:
        PortfolioFeed(ws_client=mock_ws)
        calls = mock_ws.on_message.call_args_list  # type: ignore[union-attr]
        channels = [c[0][0] for c in calls]
        assert "user_orders" in channels
        assert "fill" in channels

    def test_sids_start_as_none(self, feed: PortfolioFeed) -> None:
        assert feed._order_sid is None
        assert feed._fill_sid is None


class TestOrderRouting:
    async def test_order_message_fires_callback(self, feed: PortfolioFeed) -> None:
        callback = MagicMock()
        feed.on_order_update = callback
        msg = UserOrderMessage(
            order_id="ord-1",
            ticker="MKT-1",
            side="yes",
            status="resting",
            yes_price=65,
            no_price=35,
        )
        await feed._on_order_message(msg, sid=10, seq=1)
        callback.assert_called_once_with(msg)

    async def test_order_sid_learned_from_first_message(self, feed: PortfolioFeed) -> None:
        msg = UserOrderMessage(
            order_id="ord-1",
            ticker="MKT-1",
            side="yes",
            status="resting",
            yes_price=65,
            no_price=35,
        )
        await feed._on_order_message(msg, sid=10, seq=1)
        assert feed._order_sid == 10

    async def test_order_sid_not_overwritten_by_later_messages(self, feed: PortfolioFeed) -> None:
        msg = UserOrderMessage(
            order_id="ord-1",
            ticker="MKT-1",
            side="yes",
            status="resting",
            yes_price=65,
            no_price=35,
        )
        await feed._on_order_message(msg, sid=10, seq=1)
        await feed._on_order_message(msg, sid=20, seq=2)
        assert feed._order_sid == 10

    async def test_no_callback_no_error(self, feed: PortfolioFeed) -> None:
        msg = UserOrderMessage(
            order_id="ord-1",
            ticker="MKT-1",
            side="yes",
            status="resting",
            yes_price=65,
            no_price=35,
        )
        await feed._on_order_message(msg, sid=10, seq=1)


class TestFillRouting:
    async def test_fill_message_fires_callback(self, feed: PortfolioFeed) -> None:
        callback = MagicMock()
        feed.on_fill = callback
        msg = FillMessage(
            trade_id="fill-1",
            order_id="ord-1",
            market_ticker="MKT-1",
            side="yes",
            yes_price=65,
            count=10,
        )
        await feed._on_fill_message(msg, sid=20, seq=1)
        callback.assert_called_once_with(msg)

    async def test_fill_sid_learned_from_first_message(self, feed: PortfolioFeed) -> None:
        msg = FillMessage(
            trade_id="fill-1",
            order_id="ord-1",
            market_ticker="MKT-1",
            side="yes",
            yes_price=65,
            count=10,
        )
        await feed._on_fill_message(msg, sid=20, seq=1)
        assert feed._fill_sid == 20

    async def test_no_fill_callback_no_error(self, feed: PortfolioFeed) -> None:
        msg = FillMessage(
            trade_id="fill-1",
            order_id="ord-1",
            market_ticker="MKT-1",
            side="yes",
            yes_price=65,
            count=10,
        )
        await feed._on_fill_message(msg, sid=20, seq=1)


class TestSubscribe:
    async def test_global_subscribe_no_tickers(
        self, feed: PortfolioFeed, mock_ws: KalshiWSClient
    ) -> None:
        await feed.subscribe()
        calls = mock_ws.subscribe.call_args_list  # type: ignore[union-attr]
        assert len(calls) == 2
        assert calls[0] == call("user_orders")
        assert calls[1] == call("fill")

    async def test_subscribe_with_tickers(
        self, feed: PortfolioFeed, mock_ws: KalshiWSClient
    ) -> None:
        await feed.subscribe(tickers=["MKT-1", "MKT-2"])
        calls = mock_ws.subscribe.call_args_list  # type: ignore[union-attr]
        assert len(calls) == 4
        assert calls[0] == call("user_orders", "MKT-1")
        assert calls[1] == call("fill", "MKT-1")
        assert calls[2] == call("user_orders", "MKT-2")
        assert calls[3] == call("fill", "MKT-2")


class TestAddRemoveMarkets:
    async def test_add_markets_sends_update_subscription(
        self, feed: PortfolioFeed, mock_ws: KalshiWSClient
    ) -> None:
        feed._order_sid = 10
        feed._fill_sid = 20
        await feed.add_markets(["MKT-3"])
        calls = mock_ws.update_subscription.call_args_list  # type: ignore[union-attr]
        assert len(calls) == 2
        assert calls[0] == call(10, ["MKT-3"], action="add_markets")
        assert calls[1] == call(20, ["MKT-3"], action="add_markets")

    async def test_remove_markets_sends_update_subscription(
        self, feed: PortfolioFeed, mock_ws: KalshiWSClient
    ) -> None:
        feed._order_sid = 10
        feed._fill_sid = 20
        await feed.remove_markets(["MKT-3"])
        calls = mock_ws.update_subscription.call_args_list  # type: ignore[union-attr]
        assert len(calls) == 2
        assert calls[0] == call(10, ["MKT-3"], action="delete_markets")
        assert calls[1] == call(20, ["MKT-3"], action="delete_markets")

    async def test_add_markets_skips_if_no_sid(
        self, feed: PortfolioFeed, mock_ws: KalshiWSClient
    ) -> None:
        await feed.add_markets(["MKT-3"])
        mock_ws.update_subscription.assert_not_called()  # type: ignore[union-attr]

    async def test_remove_markets_skips_if_no_sid(
        self, feed: PortfolioFeed, mock_ws: KalshiWSClient
    ) -> None:
        await feed.remove_markets(["MKT-3"])
        mock_ws.update_subscription.assert_not_called()  # type: ignore[union-attr]
