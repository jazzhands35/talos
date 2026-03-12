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

    def test_builds_bulk_subscribe(self, client: KalshiWSClient) -> None:
        """Phase 11: bulk subscribe uses market_tickers (plural)."""
        msg = client._build_subscribe("orderbook_delta", market_tickers=["MKT-1", "MKT-2"])
        assert msg["cmd"] == "subscribe"
        assert msg["params"]["market_tickers"] == ["MKT-1", "MKT-2"]
        assert "market_ticker" not in msg["params"]

    def test_bulk_subscribe_takes_precedence(self, client: KalshiWSClient) -> None:
        """market_tickers takes precedence over market_ticker."""
        msg = client._build_subscribe("orderbook_delta", "SINGLE", market_tickers=["A", "B"])
        assert msg["params"]["market_tickers"] == ["A", "B"]
        assert "market_ticker" not in msg["params"]


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


class TestUpdateSubscription:
    def test_builds_update_subscription_command(self, client: KalshiWSClient) -> None:
        msg = client._build_update_subscription(
            sid=5, market_tickers=["MKT-1", "MKT-2"], action="add_markets"
        )
        assert msg["cmd"] == "update_subscription"
        assert msg["params"]["sids"] == [5]
        assert msg["params"]["market_tickers"] == ["MKT-1", "MKT-2"]
        assert msg["params"]["action"] == "add_markets"
        assert isinstance(msg["id"], int)

    def test_builds_delete_markets_action(self, client: KalshiWSClient) -> None:
        msg = client._build_update_subscription(
            sid=3, market_tickers=["MKT-1"], action="delete_markets"
        )
        assert msg["params"]["action"] == "delete_markets"


class TestListSubscriptions:
    def test_builds_list_subscriptions_command(self, client: KalshiWSClient) -> None:
        msg = client._build_list_subscriptions()
        assert msg["cmd"] == "list_subscriptions"
        assert msg["params"] == {}
        assert isinstance(msg["id"], int)


class TestSeqGapCallback:
    async def test_seq_gap_fires_callback(self, client: KalshiWSClient) -> None:
        """When a sequence gap is detected, the on_seq_gap callback should fire."""
        gap_callback = AsyncMock()
        client.on_seq_gap(gap_callback)
        client._sid_to_channel[1] = "orderbook_delta"
        client._sid_to_seq[1] = 5

        # Skip from seq 5 to seq 8 — gap of 2
        raw: dict[str, Any] = {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 8,
            "msg": {
                "market_ticker": "MKT-1",
                "market_id": "uuid-1",
                "price": 50,
                "delta": 10,
                "side": "yes",
                "ts": "2026-03-12T12:00:00Z",
            },
        }
        await client._dispatch(raw)
        gap_callback.assert_called_once_with(1, "orderbook_delta")

    async def test_no_gap_no_callback(self, client: KalshiWSClient) -> None:
        """Sequential messages should NOT fire the gap callback."""
        gap_callback = AsyncMock()
        client.on_seq_gap(gap_callback)
        client._sid_to_channel[1] = "orderbook_delta"
        client._sid_to_seq[1] = 5

        raw: dict[str, Any] = {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 6,
            "msg": {
                "market_ticker": "MKT-1",
                "market_id": "uuid-1",
                "price": 50,
                "delta": 10,
                "side": "yes",
                "ts": "2026-03-12T12:00:00Z",
            },
        }
        await client._dispatch(raw)
        gap_callback.assert_not_called()


class TestNewMessageTypeDispatch:
    async def test_dispatches_user_order_message(self, client: KalshiWSClient) -> None:
        callback = AsyncMock()
        client.on_message("user_orders", callback)
        client._sid_to_channel[1] = "user_orders"

        raw: dict[str, Any] = {
            "type": "user_order",
            "sid": 1,
            "seq": 1,
            "msg": {
                "order_id": "order-1",
                "ticker": "MKT-1",
                "status": "resting",
            },
        }
        await client._dispatch(raw)
        callback.assert_called_once()

    async def test_dispatches_fill_message(self, client: KalshiWSClient) -> None:
        callback = AsyncMock()
        client.on_message("fill", callback)
        client._sid_to_channel[2] = "fill"

        raw: dict[str, Any] = {
            "type": "fill",
            "sid": 2,
            "seq": 1,
            "msg": {
                "trade_id": "trade-1",
                "order_id": "order-1",
                "market_ticker": "MKT-1",
            },
        }
        await client._dispatch(raw)
        callback.assert_called_once()

    async def test_dispatches_market_position_message(self, client: KalshiWSClient) -> None:
        callback = AsyncMock()
        client.on_message("market_positions", callback)
        client._sid_to_channel[3] = "market_positions"

        raw: dict[str, Any] = {
            "type": "market_position",
            "sid": 3,
            "seq": 1,
            "msg": {
                "market_ticker": "MKT-1",
                "position_fp": "10.00",
            },
        }
        await client._dispatch(raw)
        callback.assert_called_once()

    async def test_dispatches_lifecycle_message(self, client: KalshiWSClient) -> None:
        callback = AsyncMock()
        client.on_message("market_lifecycle_v2", callback)
        client._sid_to_channel[4] = "market_lifecycle_v2"

        raw: dict[str, Any] = {
            "type": "market_lifecycle_v2",
            "sid": 4,
            "seq": 1,
            "msg": {
                "event_type": "determined",
                "market_ticker": "MKT-1",
                "result": "yes",
            },
        }
        await client._dispatch(raw)
        callback.assert_called_once()


class TestDispatchErrorHandling:
    """Verify _dispatch doesn't crash the listen loop on bad messages."""

    async def test_parse_error_does_not_crash(self, client: KalshiWSClient) -> None:
        """A model validation error should be logged, not propagated."""
        client._sid_to_channel[1] = "user_orders"
        raw: dict[str, Any] = {
            "type": "user_order",
            "sid": 1,
            "seq": 1,
            "msg": {},  # Missing required order_id and ticker
        }
        # Should NOT raise — error is caught internally
        await client._dispatch(raw)

    async def test_callback_error_does_not_crash(self, client: KalshiWSClient) -> None:
        """A callback exception should be logged, not propagated."""

        async def bad_callback(msg: Any, *, sid: int = 0, seq: int = 0) -> None:
            raise ValueError("callback exploded")

        client.on_message("user_orders", bad_callback)
        client._sid_to_channel[1] = "user_orders"
        raw: dict[str, Any] = {
            "type": "user_order",
            "sid": 1,
            "seq": 1,
            "msg": {"order_id": "o1", "ticker": "MKT-1"},
        }
        # Should NOT raise — error is caught internally
        await client._dispatch(raw)


class TestCallbackRegistration:
    def test_register_callback(self, client: KalshiWSClient) -> None:
        callback = AsyncMock()
        client.on_message("ticker", callback)
        assert "ticker" in client._callbacks

    def test_register_seq_gap_callback(self, client: KalshiWSClient) -> None:
        callback = AsyncMock()
        client.on_seq_gap(callback)
        assert client._on_seq_gap is callback
