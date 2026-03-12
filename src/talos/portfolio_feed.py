"""Async orchestrator for real-time portfolio data (orders and fills)."""

from __future__ import annotations

from collections.abc import Callable

import structlog

from talos.models.ws import FillMessage, UserOrderMessage
from talos.ws_client import KalshiWSClient

logger = structlog.get_logger()

_USER_ORDERS_CHANNEL = "user_orders"
_FILL_CHANNEL = "fill"


class PortfolioFeed:
    """Subscribes to user_orders and fill WS channels.

    Routes order updates and fill notifications to registered callbacks.
    No state accumulation — just message routing.
    """

    def __init__(self, ws_client: KalshiWSClient) -> None:
        self._ws = ws_client
        self._order_sid: int | None = None
        self._fill_sid: int | None = None
        self._ws.on_message(_USER_ORDERS_CHANNEL, self._on_order_message)
        self._ws.on_message(_FILL_CHANNEL, self._on_fill_message)
        self.on_order_update: Callable[[UserOrderMessage], None] | None = None
        self.on_fill: Callable[[FillMessage], None] | None = None

    async def _on_order_message(
        self,
        msg: UserOrderMessage,
        *,
        sid: int = 0,
        seq: int = 0,
    ) -> None:
        """Route a user_order WS message to the registered callback."""
        if sid and self._order_sid is None:
            self._order_sid = sid
        if self.on_order_update:
            self.on_order_update(msg)

    async def _on_fill_message(
        self,
        msg: FillMessage,
        *,
        sid: int = 0,
        seq: int = 0,
    ) -> None:
        """Route a fill WS message to the registered callback."""
        if sid and self._fill_sid is None:
            self._fill_sid = sid
        if self.on_fill:
            self.on_fill(msg)

    async def subscribe(self, tickers: list[str] | None = None) -> None:
        """Subscribe to user_orders and fill channels.

        Args:
            tickers: Specific market tickers. None for global (all markets).
        """
        if tickers:
            for ticker in tickers:
                await self._ws.subscribe(_USER_ORDERS_CHANNEL, ticker)
                await self._ws.subscribe(_FILL_CHANNEL, ticker)
        else:
            await self._ws.subscribe(_USER_ORDERS_CHANNEL)
            await self._ws.subscribe(_FILL_CHANNEL)
        logger.info("portfolio_feed_subscribe", tickers=tickers)

    async def add_markets(self, tickers: list[str]) -> None:
        """Add tickers to existing subscriptions via update_subscription."""
        if self._order_sid is not None:
            await self._ws.update_subscription(self._order_sid, tickers, action="add_markets")
        if self._fill_sid is not None:
            await self._ws.update_subscription(self._fill_sid, tickers, action="add_markets")
        logger.info("portfolio_feed_add_markets", tickers=tickers)

    async def remove_markets(self, tickers: list[str]) -> None:
        """Remove tickers from existing subscriptions."""
        if self._order_sid is not None:
            await self._ws.update_subscription(self._order_sid, tickers, action="delete_markets")
        if self._fill_sid is not None:
            await self._ws.update_subscription(self._fill_sid, tickers, action="delete_markets")
        logger.info("portfolio_feed_remove_markets", tickers=tickers)
