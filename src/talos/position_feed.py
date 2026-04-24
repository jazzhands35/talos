"""Async orchestrator for real-time position data from the market_positions WS channel.

Caches the latest MarketPositionMessage per market ticker for polling-style
reads. The engine uses this cache to cross-check position counts against the
PositionLedger each refresh cycle (pure observability — log only, never act).
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

from talos.models.ws import MarketPositionMessage
from talos.ws_client import KalshiWSClient

logger = structlog.get_logger()

_CHANNEL = "market_positions"


class PositionFeed:
    """Subscribes to the market_positions WS channel for real-time position data.

    Caches the latest MarketPositionMessage per market for polling-style reads.
    Also supports an optional callback for event-driven consumers.
    """

    def __init__(self, ws_client: KalshiWSClient) -> None:
        self._ws = ws_client
        self._sid: int | None = None
        self._latest: dict[str, MarketPositionMessage] = {}
        self._ws.on_message(_CHANNEL, self._on_message)
        self.on_position: Callable[[MarketPositionMessage], None] | None = None

    async def _on_message(
        self,
        msg: MarketPositionMessage,
        *,
        sid: int = 0,
        seq: int = 0,
    ) -> None:
        """Cache the latest position data and fire callback."""
        if sid and self._sid is None:
            self._sid = sid
        self._latest[msg.market_ticker] = msg
        if self.on_position:
            self.on_position(msg)

    def get_position(self, ticker: str) -> MarketPositionMessage | None:
        """Return the latest position data for a market, or None."""
        return self._latest.get(ticker)

    async def subscribe(self) -> None:
        """Subscribe to position updates globally (all markets)."""
        await self._ws.subscribe(_CHANNEL)
        logger.info("position_feed_subscribed")
