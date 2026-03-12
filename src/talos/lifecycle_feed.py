"""Async handler for market_lifecycle_v2 WebSocket channel."""

from __future__ import annotations

from collections.abc import Callable

import structlog

from talos.models.ws import MarketLifecycleMessage
from talos.ws_client import KalshiWSClient

logger = structlog.get_logger()

_LIFECYCLE_CHANNEL = "market_lifecycle_v2"


class LifecycleFeed:
    """Routes market lifecycle events (determined, settled, paused, created).

    Global subscription — no market filter. All lifecycle events from all
    markets arrive on a single sid.
    """

    def __init__(self, ws_client: KalshiWSClient) -> None:
        self._ws = ws_client
        self._sid: int | None = None
        self._ws.on_message(_LIFECYCLE_CHANNEL, self._on_message)

        # Optional callbacks by event type
        self.on_determined: Callable[[str, str, int], None] | None = None
        self.on_settled: Callable[[str], None] | None = None
        self.on_paused: Callable[[str, bool], None] | None = None
        self.on_created: Callable[[str], None] | None = None

    async def _on_message(
        self,
        msg: MarketLifecycleMessage,
        *,
        sid: int = 0,
        seq: int = 0,
    ) -> None:
        """Dispatch lifecycle event to the appropriate callback."""
        if sid and self._sid is None:
            self._sid = sid

        event_type = msg.event_type
        ticker = msg.market_ticker

        if event_type == "determined" and self.on_determined:
            self.on_determined(ticker, msg.result, msg.settlement_value)
        elif event_type == "settled" and self.on_settled:
            self.on_settled(ticker)
        elif event_type == "deactivated" and self.on_paused:
            self.on_paused(ticker, msg.is_deactivated)
        elif event_type == "new_market" and self.on_created:
            self.on_created(ticker)
        else:
            logger.debug(
                "lifecycle_event_unhandled",
                event_type=event_type,
                ticker=ticker,
            )

    async def subscribe(self) -> None:
        """Subscribe to lifecycle channel (global, no market filter)."""
        await self._ws.subscribe(_LIFECYCLE_CHANNEL)
        logger.info("lifecycle_feed_subscribed")
