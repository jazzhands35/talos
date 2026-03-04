"""Async orchestrator for real-time market data subscriptions."""

from __future__ import annotations

from collections.abc import Callable

import structlog

from talos.models.ws import OrderBookDelta, OrderBookSnapshot
from talos.orderbook import OrderBookManager
from talos.ws_client import KalshiWSClient

logger = structlog.get_logger()

_ORDERBOOK_CHANNEL = "orderbook_delta"


class MarketFeed:
    """Subscribes to markets via WebSocket, feeds OrderBookManager.

    Routes orderbook snapshots and deltas to the book manager.
    Tracks sid-to-ticker mapping for unsubscribe support.
    """

    def __init__(
        self,
        ws_client: KalshiWSClient,
        book_manager: OrderBookManager,
    ) -> None:
        self._ws = ws_client
        self._books = book_manager
        self._subscribed_tickers: set[str] = set()
        self._ticker_to_sid: dict[str, int] = {}
        self._ws.on_message(_ORDERBOOK_CHANNEL, self._on_message)
        self.on_book_update: Callable[[str], None] | None = None

    async def _on_message(
        self,
        msg: OrderBookSnapshot | OrderBookDelta,
        *,
        sid: int = 0,
        seq: int = 0,
    ) -> None:
        """Route a WS message to the book manager."""
        ticker = msg.market_ticker

        # Learn sid mapping from first message for this ticker
        if sid and ticker not in self._ticker_to_sid:
            self._ticker_to_sid[ticker] = sid

        if isinstance(msg, OrderBookSnapshot):
            self._books.apply_snapshot(ticker, msg)
            logger.info("market_feed_snapshot", ticker=ticker)
        elif isinstance(msg, OrderBookDelta):
            self._books.apply_delta(ticker, msg, seq=seq)

        if self.on_book_update:
            self.on_book_update(ticker)

    async def subscribe(self, ticker: str) -> None:
        """Subscribe to orderbook updates for a ticker."""
        await self._ws.subscribe(_ORDERBOOK_CHANNEL, ticker)
        self._subscribed_tickers.add(ticker)
        logger.info("market_feed_subscribe", ticker=ticker)

    async def unsubscribe(self, ticker: str) -> None:
        """Unsubscribe and remove from book manager."""
        sid = self._ticker_to_sid.pop(ticker, None)
        if sid is not None:
            await self._ws.unsubscribe([sid])
        self._subscribed_tickers.discard(ticker)
        self._books.remove(ticker)
        logger.info("market_feed_unsubscribe", ticker=ticker)

    async def start(self) -> None:
        """Begin listening for WS messages."""
        logger.info("market_feed_start")
        await self._ws.listen()

    async def stop(self) -> None:
        """Unsubscribe all tickers and disconnect."""
        for ticker in list(self._subscribed_tickers):
            await self.unsubscribe(ticker)
        await self._ws.disconnect()
        logger.info("market_feed_stop")

    @property
    def subscriptions(self) -> set[str]:
        """Currently subscribed tickers."""
        return set(self._subscribed_tickers)
