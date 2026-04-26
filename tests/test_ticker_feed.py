"""Tests for TickerFeed."""

# Tests in this file construct models with legacy wire-shape parameter
# names that the models' _migrate_fp validators remap to canonical
# bps/fp100 fields at runtime. Pyright doesn't see validator remapping as
# part of the constructor signature.
# pyright: reportCallIssue=false

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from talos.models.ws import TickerMessage
from talos.ticker_feed import TickerFeed
from talos.ws_client import KalshiWSClient


@pytest.fixture()
def mock_ws() -> KalshiWSClient:
    ws = MagicMock(spec=KalshiWSClient)
    ws.subscribe = AsyncMock()
    ws.update_subscription = AsyncMock()
    ws.on_message = MagicMock()
    return ws


@pytest.fixture()
def feed(mock_ws: KalshiWSClient) -> TickerFeed:
    return TickerFeed(ws_client=mock_ws)


class TestInit:
    def test_registers_ticker_channel(self, mock_ws: KalshiWSClient) -> None:
        TickerFeed(ws_client=mock_ws)
        mock_ws.on_message.assert_called_once()  # type: ignore[union-attr]
        channel = mock_ws.on_message.call_args[0][0]  # type: ignore[union-attr]
        assert channel == "ticker"

    def test_sid_starts_as_none(self, feed: TickerFeed) -> None:
        assert feed._sid is None

    def test_latest_starts_empty(self, feed: TickerFeed) -> None:
        assert feed._latest == {}


class TestMessageRouting:
    async def test_caches_latest_ticker_data(self, feed: TickerFeed) -> None:
        msg = TickerMessage(market_ticker="MKT-1", yes_bid=65, yes_ask=66)
        await feed._on_message(msg, sid=10, seq=1)
        assert feed.get_ticker("MKT-1") is msg

    async def test_overwrites_with_newer_data(self, feed: TickerFeed) -> None:
        msg1 = TickerMessage(market_ticker="MKT-1", yes_bid=65)
        msg2 = TickerMessage(market_ticker="MKT-1", yes_bid=70)
        await feed._on_message(msg1, sid=10, seq=1)
        await feed._on_message(msg2, sid=10, seq=2)
        assert feed.get_ticker("MKT-1") is msg2

    async def test_get_ticker_returns_none_for_unknown(self, feed: TickerFeed) -> None:
        assert feed.get_ticker("UNKNOWN") is None

    async def test_fires_callback(self, feed: TickerFeed) -> None:
        callback = MagicMock()
        feed.on_ticker = callback
        msg = TickerMessage(market_ticker="MKT-1", yes_bid=65)
        await feed._on_message(msg, sid=10, seq=1)
        callback.assert_called_once_with(msg)

    async def test_no_callback_no_error(self, feed: TickerFeed) -> None:
        msg = TickerMessage(market_ticker="MKT-1", yes_bid=65)
        await feed._on_message(msg, sid=10, seq=1)

    async def test_sid_learned_from_first_message(self, feed: TickerFeed) -> None:
        msg = TickerMessage(market_ticker="MKT-1")
        await feed._on_message(msg, sid=10, seq=1)
        assert feed._sid == 10


class TestSubscribe:
    async def test_subscribe_sends_per_ticker(
        self, feed: TickerFeed, mock_ws: KalshiWSClient
    ) -> None:
        await feed.subscribe(["MKT-1", "MKT-2"])
        calls = mock_ws.subscribe.call_args_list  # type: ignore[union-attr]
        assert len(calls) == 2
        assert calls[0] == call("ticker", "MKT-1", skip_ticker_ack=True, send_initial_snapshot=True)
        assert calls[1] == call("ticker", "MKT-2", skip_ticker_ack=True, send_initial_snapshot=True)


class TestAddRemoveMarkets:
    async def test_add_markets_with_sid(self, feed: TickerFeed, mock_ws: KalshiWSClient) -> None:
        feed._sid = 10
        await feed.add_markets(["MKT-3"])
        mock_ws.update_subscription.assert_called_once_with(  # type: ignore[union-attr]
            10, ["MKT-3"], action="add_markets"
        )

    async def test_add_markets_no_sid_skips(
        self, feed: TickerFeed, mock_ws: KalshiWSClient
    ) -> None:
        await feed.add_markets(["MKT-3"])
        mock_ws.update_subscription.assert_not_called()  # type: ignore[union-attr]

    async def test_remove_markets_cleans_cache(
        self, feed: TickerFeed, mock_ws: KalshiWSClient
    ) -> None:
        feed._sid = 10
        feed._latest["MKT-3"] = TickerMessage(market_ticker="MKT-3")
        await feed.remove_markets(["MKT-3"])
        assert "MKT-3" not in feed._latest
        mock_ws.update_subscription.assert_called_once()  # type: ignore[union-attr]
