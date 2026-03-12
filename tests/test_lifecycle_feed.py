"""Tests for LifecycleFeed."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from talos.auth import KalshiAuth
from talos.config import KalshiConfig, KalshiEnvironment
from talos.lifecycle_feed import LifecycleFeed
from talos.ws_client import KalshiWSClient


@pytest.fixture()
def config() -> KalshiConfig:
    return KalshiConfig(
        environment=KalshiEnvironment.DEMO,
        key_id="test-key",
        private_key_path=Path("/tmp/fake.pem"),
        rest_base_url="https://demo-api.kalshi.co/trade-api/v2",
        ws_url="wss://demo-api.kalshi.co/",
    )


@pytest.fixture()
def mock_auth() -> KalshiAuth:
    auth = MagicMock(spec=KalshiAuth)
    auth.key_id = "test-key"
    auth.headers.return_value = {
        "KALSHI-ACCESS-KEY": "test-key",
        "KALSHI-ACCESS-TIMESTAMP": "1234567890",
        "KALSHI-ACCESS-SIGNATURE": "fakesig",
    }
    return auth


@pytest.fixture()
def ws_client(config: KalshiConfig, mock_auth: KalshiAuth) -> KalshiWSClient:
    return KalshiWSClient(auth=mock_auth, config=config)


@pytest.fixture()
def feed(ws_client: KalshiWSClient) -> LifecycleFeed:
    return LifecycleFeed(ws_client=ws_client)


class TestLifecycleDispatch:
    async def test_determined_fires_callback(
        self, feed: LifecycleFeed, ws_client: KalshiWSClient
    ) -> None:
        callback = Mock()
        feed.on_determined = callback
        ws_client._sid_to_channel[1] = "market_lifecycle_v2"

        raw: dict[str, Any] = {
            "type": "market_lifecycle_v2",
            "sid": 1,
            "seq": 1,
            "msg": {
                "event_type": "determined",
                "market_ticker": "MKT-1",
                "result": "yes",
                "settlement_value": 100,
            },
        }
        await ws_client._dispatch(raw)
        callback.assert_called_once_with("MKT-1", "yes", 100)

    async def test_settled_fires_callback(
        self, feed: LifecycleFeed, ws_client: KalshiWSClient
    ) -> None:
        callback = Mock()
        feed.on_settled = callback
        ws_client._sid_to_channel[1] = "market_lifecycle_v2"

        raw: dict[str, Any] = {
            "type": "market_lifecycle_v2",
            "sid": 1,
            "seq": 1,
            "msg": {
                "event_type": "settled",
                "market_ticker": "MKT-1",
            },
        }
        await ws_client._dispatch(raw)
        callback.assert_called_once_with("MKT-1")

    async def test_deactivated_fires_paused_callback(
        self, feed: LifecycleFeed, ws_client: KalshiWSClient
    ) -> None:
        callback = Mock()
        feed.on_paused = callback
        ws_client._sid_to_channel[1] = "market_lifecycle_v2"

        raw: dict[str, Any] = {
            "type": "market_lifecycle_v2",
            "sid": 1,
            "seq": 1,
            "msg": {
                "event_type": "deactivated",
                "market_ticker": "MKT-1",
                "is_deactivated": True,
            },
        }
        await ws_client._dispatch(raw)
        callback.assert_called_once_with("MKT-1", True)

    async def test_no_callback_no_crash(
        self, feed: LifecycleFeed, ws_client: KalshiWSClient
    ) -> None:
        """Unregistered event types don't crash."""
        ws_client._sid_to_channel[1] = "market_lifecycle_v2"
        raw: dict[str, Any] = {
            "type": "market_lifecycle_v2",
            "sid": 1,
            "seq": 1,
            "msg": {
                "event_type": "new_market",
                "market_ticker": "MKT-NEW",
            },
        }
        await ws_client._dispatch(raw)  # should not raise


class TestLifecycleSubscribe:
    async def test_subscribe_sends_ws_command(
        self, feed: LifecycleFeed, ws_client: KalshiWSClient
    ) -> None:
        ws_client._ws = AsyncMock()
        await feed.subscribe()
        ws_client._ws.send.assert_called_once()
