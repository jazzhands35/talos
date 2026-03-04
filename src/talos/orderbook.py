"""Local orderbook state management for subscribed markets."""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from talos.models.market import OrderBookLevel
from talos.models.ws import OrderBookDelta, OrderBookSnapshot

logger = structlog.get_logger()


class LocalOrderBook(BaseModel):
    """Local state for a single market's orderbook."""

    ticker: str
    yes: list[OrderBookLevel] = []
    no: list[OrderBookLevel] = []
    last_seq: int = 0
    stale: bool = False


class OrderBookManager:
    """Maintains local orderbook state for multiple markets.

    Pure state machine — no I/O, no async. Receives snapshots and deltas,
    maintains sorted level lists, and answers queries.
    """

    def __init__(self) -> None:
        self._books: dict[str, LocalOrderBook] = {}

    def apply_snapshot(self, ticker: str, snapshot: OrderBookSnapshot) -> None:
        """Replace entire book for a ticker. Resets seq and stale flag."""
        yes_levels = sorted(
            [OrderBookLevel(price=p, quantity=q) for p, q in snapshot.yes],
            key=lambda lvl: lvl.price,
            reverse=True,
        )
        no_levels = sorted(
            [OrderBookLevel(price=p, quantity=q) for p, q in snapshot.no],
            key=lambda lvl: lvl.price,
            reverse=True,
        )
        self._books[ticker] = LocalOrderBook(
            ticker=ticker,
            yes=yes_levels,
            no=no_levels,
            last_seq=0,
            stale=False,
        )
        logger.debug(
            "orderbook_snapshot",
            ticker=ticker,
            yes_levels=len(yes_levels),
            no_levels=len(no_levels),
        )

    def apply_delta(self, ticker: str, delta: OrderBookDelta, *, seq: int = 0) -> None:
        """Apply incremental orderbook update. Sets stale on seq gap."""
        book = self._books.get(ticker)
        if book is None:
            logger.warning("orderbook_delta_unknown_ticker", ticker=ticker)
            return

        # Seq gap detection
        if seq > 0 and book.last_seq > 0 and seq != book.last_seq + 1:
            logger.warning(
                "orderbook_seq_gap",
                ticker=ticker,
                expected=book.last_seq + 1,
                got=seq,
            )
            book.stale = True
        if seq > 0:
            book.last_seq = seq

        # Select side
        side_levels = book.yes if delta.side == "yes" else book.no

        # Find existing level at this price
        idx = next(
            (i for i, lvl in enumerate(side_levels) if lvl.price == delta.price),
            None,
        )

        if delta.delta == 0:
            # Remove level
            if idx is not None:
                side_levels.pop(idx)
        elif idx is not None:
            # Update existing level
            side_levels[idx] = OrderBookLevel(price=delta.price, quantity=delta.delta)
        else:
            # Insert new level, maintain descending sort
            side_levels.append(OrderBookLevel(price=delta.price, quantity=delta.delta))
            side_levels.sort(key=lambda lvl: lvl.price, reverse=True)

        logger.debug(
            "orderbook_delta_applied",
            ticker=ticker,
            side=delta.side,
            price=delta.price,
            delta=delta.delta,
        )

    def best_bid(self, ticker: str) -> OrderBookLevel | None:
        """Highest yes bid. Returns top of YES side."""
        book = self._books.get(ticker)
        if book and book.yes:
            return book.yes[0]
        return None

    def best_ask(self, ticker: str) -> OrderBookLevel | None:
        """Best implied YES ask. Returns top of NO side.

        The implied YES ask price is ``100 - level.price``.
        Conversion is left to the strategy layer.
        """
        book = self._books.get(ticker)
        if book and book.no:
            return book.no[0]
        return None

    def remove(self, ticker: str) -> None:
        """Stop tracking a ticker."""
        self._books.pop(ticker, None)
        logger.debug("orderbook_removed", ticker=ticker)

    @property
    def tickers(self) -> set[str]:
        """All currently tracked tickers."""
        return set(self._books.keys())

    def get_book(self, ticker: str) -> LocalOrderBook | None:
        """Get current book state, or None if not tracked."""
        return self._books.get(ticker)
