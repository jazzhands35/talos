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
        self._ws.on_seq_gap(self._on_seq_gap)
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

        # Always update sid mapping — a resubscribe may assign a new sid
        if sid:
            self._ticker_to_sid[ticker] = sid

        if isinstance(msg, OrderBookSnapshot):
            self._books.apply_snapshot(ticker, msg)
            logger.info("market_feed_snapshot", ticker=ticker)
        elif isinstance(msg, OrderBookDelta):
            self._books.apply_delta(ticker, msg, seq=seq)

        if self.on_book_update:
            self.on_book_update(ticker)

    async def _on_seq_gap(self, sid: int, channel: str) -> None:
        """Recover from a sequence gap by resubscribing.

        Unsubscribes the stale sid and re-subscribes to ALL tickers that shared it.
        Bulk subscriptions share a single sid across many tickers, so we must
        collect every ticker mapped to the failed sid before resubscribing.
        Kalshi sends a fresh snapshot on subscribe, resetting state cleanly.
        """
        # Collect ALL tickers sharing this sid — include tickers that were
        # subscribed but haven't sent data yet (not in _ticker_to_sid).
        learned = [t for t, s in self._ticker_to_sid.items() if s == sid]
        # Also include subscribed tickers with no sid mapping (never sent data)
        unmapped = [
            t for t in self._subscribed_tickers
            if t not in self._ticker_to_sid
        ]
        affected_tickers = list(set(learned + unmapped))
        if not affected_tickers:
            logger.warning("ws_seq_gap_unknown_sid", sid=sid, channel=channel)
            return

        logger.info(
            "ws_seq_gap_recovery",
            sid=sid,
            channel=channel,
            ticker_count=len(affected_tickers),
            learned=len(learned),
            unmapped=len(unmapped),
        )
        # Unsubscribe stale sid BEFORE clearing mappings
        await self._ws.unsubscribe([sid])
        # Remove stale mappings for all affected tickers
        for ticker in affected_tickers:
            self._ticker_to_sid.pop(ticker, None)
        # Resubscribe all affected tickers — use bulk when multiple
        if len(affected_tickers) == 1:
            await self._ws.subscribe(channel, affected_tickers[0])
        else:
            await self._ws.subscribe(channel, market_tickers=affected_tickers)

    async def connect(self) -> None:
        """Connect the underlying WebSocket."""
        await self._ws.connect()

    async def subscribe(self, ticker: str) -> None:
        """Subscribe to orderbook updates for a ticker."""
        await self._ws.subscribe(_ORDERBOOK_CHANNEL, ticker)
        self._subscribed_tickers.add(ticker)
        logger.info("market_feed_subscribe", ticker=ticker)

    async def subscribe_bulk(self, tickers: list[str]) -> None:
        """Subscribe to orderbook updates for multiple tickers in batches.

        Subscribing 292 tickers at once triggers 292 orderbook snapshots
        back-to-back, which freezes the event loop during processing.
        Batching with yields lets the UI stay responsive.
        """
        import asyncio

        new_tickers = [t for t in tickers if t not in self._subscribed_tickers]
        if not new_tickers:
            return
        batch_size = 20
        for i in range(0, len(new_tickers), batch_size):
            batch = new_tickers[i : i + batch_size]
            await self._ws.subscribe(_ORDERBOOK_CHANNEL, market_tickers=batch)
            self._subscribed_tickers.update(batch)
            await asyncio.sleep(0.1)  # yield to event loop between batches
        logger.info("market_feed_subscribe_bulk", count=len(new_tickers))

    async def unsubscribe(self, ticker: str) -> None:
        """Unsubscribe and remove from book manager.

        Bulk-safe: if other tickers share the same subscription (sid),
        removes only this ticker via update_subscription instead of
        killing the entire sid.
        """
        sid = self._ticker_to_sid.pop(ticker, None)
        if sid is not None:
            siblings = [t for t, s in self._ticker_to_sid.items() if s == sid]
            if siblings:
                # Other tickers share this sid — remove just this one
                await self._ws.update_subscription(sid, [ticker], action="delete_markets")
            else:
                # Last ticker on this sid — kill the subscription
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
    def book_manager(self) -> OrderBookManager:
        """The underlying orderbook manager."""
        return self._books

    @property
    def subscriptions(self) -> set[str]:
        """Currently subscribed tickers."""
        return set(self._subscribed_tickers)
