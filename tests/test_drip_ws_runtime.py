"""Tests for DripWSRuntime — WS orchestration layer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from drip.ws_runtime import _RECONNECT_BASE, _RECONNECT_MAX, DripWSRuntime


def _make_runtime(
    *,
    on_fill: AsyncMock | None = None,
    on_user_order: AsyncMock | None = None,
    on_orderbook_snapshot: AsyncMock | None = None,
    on_orderbook_delta: AsyncMock | None = None,
    on_connect: AsyncMock | None = None,
    on_disconnect: AsyncMock | None = None,
) -> DripWSRuntime:
    """Build a DripWSRuntime with mocked auth/config and callbacks."""
    auth = MagicMock()
    config = MagicMock()
    return DripWSRuntime(
        auth=auth,
        config=config,
        tickers=["KXTEST-A", "KXTEST-B"],
        on_fill=on_fill or AsyncMock(),
        on_user_order=on_user_order or AsyncMock(),
        on_orderbook_snapshot=on_orderbook_snapshot or AsyncMock(),
        on_orderbook_delta=on_orderbook_delta or AsyncMock(),
        on_connect=on_connect or AsyncMock(),
        on_disconnect=on_disconnect or AsyncMock(),
    )


def _make_mock_ws() -> MagicMock:
    """Build a mock KalshiWSClient with correct sync/async method signatures.

    on_message() and on_seq_gap() are sync (callback registration).
    connect(), subscribe(), listen(), disconnect() are async (I/O).
    Using MagicMock base avoids RuntimeWarning from unawaited coroutines.
    """
    ws = MagicMock()
    ws.connect = AsyncMock()
    ws.subscribe = AsyncMock()
    ws.listen = AsyncMock(return_value=None)
    ws.disconnect = AsyncMock()
    return ws


class TestDripWSRuntimeInit:
    def test_starts_not_running(self) -> None:
        rt = _make_runtime()
        assert rt._running is False
        assert rt._ws is None

    def test_stores_tickers(self) -> None:
        rt = _make_runtime()
        assert rt._tickers == ["KXTEST-A", "KXTEST-B"]


class TestDripWSRuntimeStop:
    @pytest.mark.asyncio()
    async def test_stop_sets_not_running(self) -> None:
        rt = _make_runtime()
        rt._running = True
        await rt.stop()
        assert rt._running is False

    @pytest.mark.asyncio()
    async def test_stop_disconnects_ws(self) -> None:
        rt = _make_runtime()
        rt._running = True
        mock_ws = _make_mock_ws()
        rt._ws = mock_ws
        await rt.stop()
        mock_ws.disconnect.assert_called_once()
        assert rt._ws is None


class TestReconnectBackoff:
    def test_backoff_constants(self) -> None:
        assert _RECONNECT_BASE == 2.0
        assert _RECONNECT_MAX == 30.0

    def test_exponential_backoff_formula(self) -> None:
        """Verify the backoff sequence: 2, 4, 8, 16, 30, 30..."""
        delays = []
        for attempt in range(6):
            delay = min(_RECONNECT_BASE * (2**attempt), _RECONNECT_MAX)
            delays.append(delay)
        assert delays == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


class TestConnectAndListen:
    @pytest.mark.asyncio()
    async def test_connect_calls_on_connect_before_subscribe(self) -> None:
        """Verify hydrate-before-subscribe ordering."""
        call_order: list[str] = []

        async def track_connect() -> None:
            call_order.append("on_connect")

        on_connect = AsyncMock(side_effect=track_connect)
        rt = _make_runtime(on_connect=on_connect)

        with patch("drip.ws_runtime.KalshiWSClient") as mock_ws_cls:
            mock_ws = _make_mock_ws()
            mock_ws_cls.return_value = mock_ws

            async def track_subscribe(*_args: object, **_kwargs: object) -> None:
                call_order.append("subscribe")

            mock_ws.subscribe = AsyncMock(side_effect=track_subscribe)

            await rt._connect_and_listen()

        # on_connect (hydration) must happen before any subscribe
        connect_idx = call_order.index("on_connect")
        first_subscribe_idx = call_order.index("subscribe")
        assert connect_idx < first_subscribe_idx

    @pytest.mark.asyncio()
    async def test_subscribes_to_three_channels(self) -> None:
        rt = _make_runtime()

        with patch("drip.ws_runtime.KalshiWSClient") as mock_ws_cls:
            mock_ws = _make_mock_ws()
            mock_ws_cls.return_value = mock_ws

            await rt._connect_and_listen()

        assert mock_ws.subscribe.call_count == 3
        channels = [call.args[0] for call in mock_ws.subscribe.call_args_list]
        assert "fill" in channels
        assert "user_orders" in channels
        assert "orderbook_delta" in channels

    @pytest.mark.asyncio()
    async def test_resets_attempts_on_success(self) -> None:
        rt = _make_runtime()
        rt._attempts = 5

        with patch("drip.ws_runtime.KalshiWSClient") as mock_ws_cls:
            mock_ws = _make_mock_ws()
            mock_ws_cls.return_value = mock_ws

            await rt._connect_and_listen()

        assert rt._attempts == 0

    @pytest.mark.asyncio()
    async def test_registers_callbacks_on_ws(self) -> None:
        """Verify on_message and on_seq_gap are called (sync registration)."""
        rt = _make_runtime()

        with patch("drip.ws_runtime.KalshiWSClient") as mock_ws_cls:
            mock_ws = _make_mock_ws()
            mock_ws_cls.return_value = mock_ws

            await rt._connect_and_listen()

        # on_message called for fill, user_orders, orderbook_delta
        assert mock_ws.on_message.call_count == 3
        channels = [call.args[0] for call in mock_ws.on_message.call_args_list]
        assert channels == ["fill", "user_orders", "orderbook_delta"]

        # on_seq_gap called once
        mock_ws.on_seq_gap.assert_called_once()
