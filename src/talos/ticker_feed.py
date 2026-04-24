"""Async orchestrator for real-time ticker data (BBA, volume, OI)."""

from __future__ import annotations

from collections.abc import Callable

import structlog

from talos.models.ws import TickerMessage
from talos.ws_client import KalshiWSClient

logger = structlog.get_logger()

_TICKER_CHANNEL = "ticker"


class TickerFeed:
    """Subscribes to the ticker WS channel for real-time market data.

    Caches the latest TickerMessage per market for polling-style reads.
    Also supports an optional callback for event-driven consumers.
    """

    def __init__(self, ws_client: KalshiWSClient) -> None:
        self._ws = ws_client
        self._sid: int | None = None
        self._latest: dict[str, TickerMessage] = {}
        self._ws.on_message(_TICKER_CHANNEL, self._on_message)
        self.on_ticker: Callable[[TickerMessage], None] | None = None

    async def _on_message(
        self,
        msg: TickerMessage,
        *,
        sid: int = 0,
        seq: int = 0,
    ) -> None:
        """Cache the latest ticker data and fire callback."""
        if sid and self._sid is None:
            self._sid = sid
        self._latest[msg.market_ticker] = msg
        if self.on_ticker:
            self.on_ticker(msg)

    def get_ticker(self, ticker: str) -> TickerMessage | None:
        """Return the latest ticker data for a market, or None."""
        return self._latest.get(ticker)

    async def subscribe(self, tickers: list[str]) -> None:
        """Subscribe to ticker updates for specific markets.

        Uses skip_ticker_ack and send_initial_snapshot for efficiency.
        """
        unique = list(dict.fromkeys(tickers))  # dedupe, preserve order
        for ticker in unique:
            await self._ws.subscribe(
                _TICKER_CHANNEL,
                ticker,
                skip_ticker_ack=True,
                send_initial_snapshot=True,
            )
        logger.info("ticker_feed_subscribe", count=len(unique))

    async def add_markets(self, tickers: list[str]) -> None:
        """Add tickers to existing subscription via update_subscription."""
        if self._sid is not None:
            await self._ws.update_subscription(self._sid, tickers, action="add_markets")
        logger.info("ticker_feed_add_markets", tickers=tickers)

    async def remove_markets(self, tickers: list[str]) -> None:
        """Remove tickers from existing subscription."""
        if self._sid is not None:
            await self._ws.update_subscription(self._sid, tickers, action="delete_markets")
        # Clean up cached data for removed tickers
        for ticker in tickers:
            self._latest.pop(ticker, None)
        logger.info("ticker_feed_remove_markets", tickers=tickers)
