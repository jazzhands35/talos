"""Tests for Kalshi WebSocket client."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from talos.auth import KalshiAuth
from talos.config import KalshiConfig, KalshiEnvironment
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
def client(config: KalshiConfig, mock_auth: KalshiAuth) -> KalshiWSClient:
    return KalshiWSClient(auth=mock_auth, config=config)


class TestSubscribeMessage:
    def test_builds_subscribe_command(self, client: KalshiWSClient) -> None:
        msg = client._build_subscribe("orderbook_delta", "KXBTC-26MAR-T50000")
        assert msg["cmd"] == "subscribe"
        assert msg["params"]["channels"] == ["orderbook_delta"]
        assert msg["params"]["market_ticker"] == "KXBTC-26MAR-T50000"
        assert isinstance(msg["id"], int)
        assert msg["id"] >= 1

    def test_message_ids_increment(self, client: KalshiWSClient) -> None:
        msg1 = client._build_subscribe("orderbook_delta", "MKT-1")
        msg2 = client._build_subscribe("ticker", "MKT-2")
        assert msg2["id"] == msg1["id"] + 1


class TestUnsubscribeMessage:
    def test_builds_unsubscribe_command(self, client: KalshiWSClient) -> None:
        msg = client._build_unsubscribe([1, 2, 3])
        assert msg["cmd"] == "unsubscribe"
        assert msg["params"]["sids"] == [1, 2, 3]


class TestMessageDispatch:
    async def test_dispatches_to_registered_callback(self, client: KalshiWSClient) -> None:
        callback = AsyncMock()
        client.on_message("orderbook_delta", callback)
        client._sid_to_channel[1] = "orderbook_delta"

        raw: dict[str, Any] = {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {
                "market_ticker": "KXBTC-26MAR-T50000",
                "market_id": "uuid-123",
                "yes": [[65, 100]],
                "no": [[35, 50]],
            },
        }
        await client._dispatch(raw)
        callback.assert_called_once()

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

    async def test_ignores_unknown_sid(self, client: KalshiWSClient) -> None:
        callback = AsyncMock()
        client.on_message("orderbook_delta", callback)

        raw: dict[str, Any] = {"type": "orderbook_delta", "sid": 999, "seq": 1, "msg": {}}
        await client._dispatch(raw)
        callback.assert_not_called()


class TestSeqTracking:
    async def test_detects_seq_gap(self, client: KalshiWSClient) -> None:
        callback = AsyncMock()
        client.on_message("orderbook_delta", callback)
        client._sid_to_channel[1] = "orderbook_delta"
        client._sid_to_seq[1] = 5

        raw: dict[str, Any] = {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 8,
            "msg": {
                "market_ticker": "MKT",
                "market_id": "uuid",
                "price": 50,
                "delta": 10,
                "side": "yes",
                "ts": "2026-03-03T12:00:00Z",
            },
        }

        with patch("talos.ws_client.logger") as mock_logger:
            await client._dispatch(raw)
            mock_logger.warning.assert_called_once()


class TestCallbackRegistration:
    def test_register_callback(self, client: KalshiWSClient) -> None:
        callback = AsyncMock()
        client.on_message("ticker", callback)
        assert "ticker" in client._callbacks
