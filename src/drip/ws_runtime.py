"""WS orchestration layer — owns connection, subscriptions, and reconnect.

Responsibilities:
- Own one authenticated KalshiWSClient
- Subscribe to fill, user_orders, orderbook_delta for Drip's two tickers
- Forward normalized events to DripApp callbacks
- Auto-reconnect with exponential backoff
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import structlog

from talos.auth import KalshiAuth
from talos.config import KalshiConfig
from talos.models.ws import (
    FillMessage,
    OrderBookDelta,
    OrderBookSnapshot,
    UserOrderMessage,
)
from talos.ws_client import KalshiWSClient

logger = structlog.get_logger()

_RECONNECT_BASE = 2.0  # seconds
_RECONNECT_MAX = 30.0  # seconds


class DripWSRuntime:
    """Manages the WebSocket lifecycle for a single Drip run.

    Owns one KalshiWSClient.  Subscribes to fill, user_orders, and
    orderbook_delta for the strategy's two tickers.  Forwards parsed
    events to app-provided callbacks.  Reconnects with exponential backoff
    on disconnection.
    """

    def __init__(
        self,
        auth: KalshiAuth,
        config: KalshiConfig,
        tickers: list[str],
        *,
        on_fill: Callable[[FillMessage], Coroutine[Any, Any, None]],
        on_user_order: Callable[[UserOrderMessage], Coroutine[Any, Any, None]],
        on_orderbook_snapshot: Callable[[OrderBookSnapshot], Coroutine[Any, Any, None]],
        on_orderbook_delta: Callable[[OrderBookDelta], Coroutine[Any, Any, None]],
        on_connect: Callable[[], Coroutine[Any, Any, None]],
        on_disconnect: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        self._auth = auth
        self._kalshi_config = config
        self._tickers = tickers
        self._on_fill = on_fill
        self._on_user_order = on_user_order
        self._on_orderbook_snapshot = on_orderbook_snapshot
        self._on_orderbook_delta = on_orderbook_delta
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._ws: KalshiWSClient | None = None
        self._running = False
        self._attempts = 0

    async def start(self) -> None:
        """Connect and listen.  Reconnects automatically on failure."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except Exception:
                if not self._running:
                    break
                await self._on_disconnect()
                delay = min(_RECONNECT_BASE * (2**self._attempts), _RECONNECT_MAX)
                logger.warning(
                    "drip_ws_reconnecting",
                    delay=delay,
                    attempt=self._attempts + 1,
                )
                await asyncio.sleep(delay)
                self._attempts += 1

    async def stop(self) -> None:
        """Gracefully disconnect."""
        self._running = False
        if self._ws:
            await self._ws.disconnect()
            self._ws = None

    async def _connect_and_listen(self) -> None:
        """Single connection lifecycle: connect -> hydrate -> subscribe -> listen."""
        ws = KalshiWSClient(self._auth, self._kalshi_config)

        # Thin wrappers that strip ws_client's **kwargs and forward the model
        async def _handle_fill(msg: FillMessage, **_: Any) -> None:
            await self._on_fill(msg)

        async def _handle_user_order(msg: UserOrderMessage, **_: Any) -> None:
            await self._on_user_order(msg)

        async def _handle_orderbook(msg: OrderBookSnapshot | OrderBookDelta, **_: Any) -> None:
            if isinstance(msg, OrderBookSnapshot):
                await self._on_orderbook_snapshot(msg)
            else:
                await self._on_orderbook_delta(msg)

        ws.on_message("fill", _handle_fill)
        ws.on_message("user_orders", _handle_user_order)
        ws.on_message("orderbook_delta", _handle_orderbook)

        async def _on_gap(sid: int, channel: str) -> None:
            logger.warning("drip_ws_seq_gap", sid=sid, channel=channel)

        ws.on_seq_gap(_on_gap)

        # Connect
        await ws.connect()
        self._ws = ws
        self._attempts = 0

        # Let app hydrate from REST *before* subscribing to give a sane
        # baseline.  Small gaps between REST snapshot and WS start are
        # possible; the periodic reconcile is the real repair mechanism.
        await self._on_connect()

        # Subscribe to channels
        await ws.subscribe("fill", market_tickers=self._tickers)
        await ws.subscribe("user_orders", market_tickers=self._tickers)
        await ws.subscribe("orderbook_delta", market_tickers=self._tickers)
        logger.info("drip_ws_subscribed", tickers=self._tickers)

        # Block on listen until disconnect or error
        await ws.listen()
