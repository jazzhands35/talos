"""Local orderbook state management for subscribed markets."""

from __future__ import annotations

import bisect
import time

import structlog
from pydantic import BaseModel

from talos.models.market import OrderBookLevel
from talos.models.ws import OrderBookDelta, OrderBookSnapshot

logger = structlog.get_logger()

# Books with no updates for this long are considered stale.
# The 30s recovery cycle resubscribes stale books, triggering a fresh snapshot.
_STALE_THRESHOLD = 120.0


def _parse_levels_sorted(
    raw_bps_fp100: list[list[int]],
) -> list[OrderBookLevel]:
    """Parse [[price_bps, quantity_fp100], ...] into a sorted OrderBookLevel list.

    The WS validator (``OrderBookSnapshot._migrate_fp``) normalizes both
    integer-cents wire and ``_dollars_fp`` string wire into this shape, so
    this function receives exact bps/fp100 pairs directly.
    """
    levels = [
        OrderBookLevel(price_bps=pb, quantity_fp100=qf)
        for (pb, qf) in raw_bps_fp100
    ]
    return sorted(levels, key=lambda lvl: lvl.price_bps, reverse=True)


class LocalOrderBook(BaseModel):
    """Local state for a single market's orderbook."""

    ticker: str
    yes: list[OrderBookLevel] = []
    no: list[OrderBookLevel] = []
    last_update: float = 0.0
    created_at: float = 0.0

    @property
    def stale(self) -> bool:
        """Book is stale if no updates received within the threshold.

        A book that was created but never received any data (last_update == 0)
        is stale if it was created more than threshold seconds ago — this
        catches subscriptions that succeeded but never delivered data.
        """
        now = time.time()
        if self.last_update <= 0.0:
            # Never received data — stale if created long enough ago
            return self.created_at > 0.0 and now - self.created_at > _STALE_THRESHOLD
        return now - self.last_update > _STALE_THRESHOLD


class OrderBookManager:
    """Maintains local orderbook state for multiple markets.

    Pure state machine — no I/O, no async. Receives snapshots and deltas,
    maintains sorted level lists, and answers queries.
    """

    def __init__(self) -> None:
        self._books: dict[str, LocalOrderBook] = {}
        self._pending_deltas: dict[str, list[tuple[OrderBookDelta, int]]] = {}

    def apply_snapshot(self, ticker: str, snapshot: OrderBookSnapshot) -> None:
        """Replace entire book for a ticker. Resets update timestamp."""
        yes_levels = _parse_levels_sorted(snapshot.yes_bps_fp100)
        no_levels = _parse_levels_sorted(snapshot.no_bps_fp100)
        now = time.time()
        self._books[ticker] = LocalOrderBook(
            ticker=ticker,
            yes=yes_levels,
            no=no_levels,
            last_update=now,
            created_at=now,
        )
        logger.debug(
            "orderbook_snapshot",
            ticker=ticker,
            yes_levels=len(yes_levels),
            no_levels=len(no_levels),
        )

        # Replay any deltas that arrived before this snapshot
        buffered = self._pending_deltas.pop(ticker, [])
        if buffered:
            logger.info(
                "orderbook_replay_buffered_deltas",
                ticker=ticker,
                count=len(buffered),
            )
            for delta, seq in buffered:
                self.apply_delta(ticker, delta, seq=seq)

    def apply_delta(self, ticker: str, delta: OrderBookDelta, *, seq: int = 0) -> None:
        """Apply incremental orderbook update."""
        book = self._books.get(ticker)
        if book is None:
            # Buffer delta until snapshot arrives
            self._pending_deltas.setdefault(ticker, []).append((delta, seq))
            logger.debug(
                "orderbook_delta_buffered",
                ticker=ticker,
                price_bps=delta.price_bps,
                side=delta.side,
                seq=seq,
                buffer_size=len(self._pending_deltas[ticker]),
            )
            return

        book.last_update = time.time()

        # Select side
        side_levels = book.yes if delta.side == "yes" else book.no

        # Find existing level at this price
        idx = next(
            (i for i, lvl in enumerate(side_levels) if lvl.price_bps == delta.price_bps),
            None,
        )

        if idx is not None:
            side_levels[idx].quantity_fp100 += delta.delta_fp100
            if side_levels[idx].quantity_fp100 <= 0:
                side_levels.pop(idx)
        elif delta.delta_fp100 > 0:
            # Insert new level, maintain descending sort via bisect.
            new_level = OrderBookLevel(
                price_bps=delta.price_bps,
                quantity_fp100=delta.delta_fp100,
            )
            bisect.insort(side_levels, new_level, key=lambda lvl: -lvl.price_bps)

        logger.debug(
            "orderbook_delta_applied",
            ticker=ticker,
            side=delta.side,
            price_bps=delta.price_bps,
            delta_fp100=delta.delta_fp100,
        )

    def best_bid(self, ticker: str) -> OrderBookLevel | None:
        """Highest yes bid. Returns top of YES side."""
        book = self._books.get(ticker)
        if book and book.yes:
            return book.yes[0]
        return None

    def best_ask(self, ticker: str, side: str = "no") -> OrderBookLevel | None:
        """Best price on the given side of the book.

        Default ``side='no'`` returns top of NO side (existing behavior).
        The implied YES ask price is ``100 - level.price``.
        Conversion is left to the strategy layer.

        ``side='yes'`` returns top of YES side (for YES/NO arb).
        """
        book = self._books.get(ticker)
        if not book:
            return None
        levels = book.no if side == "no" else book.yes
        return levels[0] if levels else None

    def remove(self, ticker: str) -> None:
        """Stop tracking a ticker."""
        self._books.pop(ticker, None)
        self._pending_deltas.pop(ticker, None)
        logger.debug("orderbook_removed", ticker=ticker)

    @property
    def tickers(self) -> set[str]:
        """All currently tracked tickers."""
        return set(self._books.keys())

    def stale_tickers(self) -> list[str]:
        """Return tickers whose books haven't been updated recently."""
        return [t for t, book in self._books.items() if book.stale]

    def missing_tickers(self, subscribed: set[str]) -> list[str]:
        """Return subscribed tickers that have no book (never received data)."""
        return [t for t in subscribed if t not in self._books]

    def most_recent_update(self) -> float:
        """Epoch timestamp of the most recently updated book, or 0.0 if no books."""
        if not self._books:
            return 0.0
        return max(book.last_update for book in self._books.values())

    def get_book(self, ticker: str) -> LocalOrderBook | None:
        """Get current book state, or None if not tracked."""
        return self._books.get(ticker)
