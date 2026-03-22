"""Arbitrage opportunity scanner for arbitrage pairs."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from talos.fees import fee_adjusted_edge
from talos.models.strategy import ArbPair, Opportunity
from talos.orderbook import OrderBookManager

logger = structlog.get_logger()


class ArbitrageScanner:
    """Detects arbitrage opportunities within game events.

    Pure state machine — no I/O, no async. Reads orderbook state
    from OrderBookManager, evaluates registered pairs, maintains
    a list of current opportunities.
    """

    def __init__(self, book_manager: OrderBookManager) -> None:
        self._books = book_manager
        self._pairs: list[ArbPair] = []
        self._pairs_by_ticker: dict[str, list[ArbPair]] = {}
        self._opportunities: dict[str, Opportunity] = {}
        self._all_snapshots: dict[str, Opportunity] = {}
        self._sorted_cache: list[Opportunity] | None = None

    def add_pair(
        self,
        event_ticker: str,
        ticker_a: str,
        ticker_b: str,
        *,
        fee_type: str = "quadratic_with_maker_fees",
        fee_rate: float = 0.0175,
        close_time: str | None = None,
        expected_expiration_time: str | None = None,
        side_a: str = "no",
        side_b: str = "no",
        kalshi_event_ticker: str = "",
    ) -> None:
        """Register a pair of markets to monitor."""
        if any(p.event_ticker == event_ticker for p in self._pairs):
            return
        pair = ArbPair(
            event_ticker=event_ticker,
            ticker_a=ticker_a,
            ticker_b=ticker_b,
            side_a=side_a,
            side_b=side_b,
            kalshi_event_ticker=kalshi_event_ticker,
            fee_type=fee_type,
            fee_rate=fee_rate,
            close_time=close_time,
            expected_expiration_time=expected_expiration_time,
        )
        self._pairs.append(pair)
        self._pairs_by_ticker.setdefault(ticker_a, []).append(pair)
        self._pairs_by_ticker.setdefault(ticker_b, []).append(pair)
        # Placeholder so the table row exists even before orderbook data arrives
        self._all_snapshots.setdefault(
            event_ticker,
            Opportunity(
                event_ticker=event_ticker,
                ticker_a=ticker_a,
                ticker_b=ticker_b,
                no_a=0,
                no_b=0,
                qty_a=0,
                qty_b=0,
                raw_edge=0,
                tradeable_qty=0,
                timestamp=datetime.now(UTC).isoformat(),
                close_time=close_time,
                fee_rate=fee_rate,
            ),
        )
        logger.info("scanner_pair_added", event_ticker=event_ticker, a=ticker_a, b=ticker_b)

    def remove_pair(self, event_ticker: str) -> None:
        """Remove a pair by event ticker."""
        pair = next((p for p in self._pairs if p.event_ticker == event_ticker), None)
        if pair is None:
            return
        self._pairs.remove(pair)
        for ticker in (pair.ticker_a, pair.ticker_b):
            ticker_pairs = self._pairs_by_ticker.get(ticker, [])
            if pair in ticker_pairs:
                ticker_pairs.remove(pair)
                if not ticker_pairs:
                    del self._pairs_by_ticker[ticker]
        self._opportunities.pop(event_ticker, None)
        self._all_snapshots.pop(event_ticker, None)
        self._sorted_cache = None
        logger.info("scanner_pair_removed", event_ticker=event_ticker)

    def scan(self, ticker: str) -> None:
        """Re-evaluate all pairs involving this ticker."""
        pairs = self._pairs_by_ticker.get(ticker, [])
        for pair in pairs:
            self._evaluate_pair(pair)

    def _evaluate_pair(self, pair: ArbPair) -> None:
        """Check one pair for arbitrage opportunity."""
        self._sorted_cache = None
        no_a = self._books.best_ask(pair.ticker_a, side=pair.side_a)
        no_b = self._books.best_ask(pair.ticker_b, side=pair.side_b)

        if not no_a or not no_b:
            self._opportunities.pop(pair.event_ticker, None)
            return

        book_a = self._books.get_book(pair.ticker_a)
        book_b = self._books.get_book(pair.ticker_b)
        if (book_a and book_a.stale) or (book_b and book_b.stale):
            self._opportunities.pop(pair.event_ticker, None)
            logger.warning(
                "scanner_stale_book_skip",
                event_ticker=pair.event_ticker,
                stale_a=bool(book_a and book_a.stale),
                stale_b=bool(book_b and book_b.stale),
            )
            return

        raw_edge = 100 - no_a.price - no_b.price

        opp = Opportunity(
            event_ticker=pair.event_ticker,
            ticker_a=pair.ticker_a,
            ticker_b=pair.ticker_b,
            no_a=no_a.price,
            no_b=no_b.price,
            qty_a=no_a.quantity,
            qty_b=no_b.quantity,
            raw_edge=raw_edge,
            fee_edge=fee_adjusted_edge(no_a.price, no_b.price, rate=pair.fee_rate),
            tradeable_qty=min(no_a.quantity, no_b.quantity),
            timestamp=datetime.now(UTC).isoformat(),
            close_time=pair.close_time,
            fee_rate=pair.fee_rate,
        )
        self._all_snapshots[pair.event_ticker] = opp

        if raw_edge > 0:
            self._opportunities[pair.event_ticker] = opp
            logger.debug(
                "scanner_opportunity",
                event_ticker=pair.event_ticker,
                edge=raw_edge,
                qty=opp.tradeable_qty,
            )
        else:
            self._opportunities.pop(pair.event_ticker, None)

    def get_opportunity(self, event_ticker: str) -> Opportunity | None:
        """Look up a single opportunity by event ticker."""
        return self._opportunities.get(event_ticker)

    @property
    def opportunities(self) -> list[Opportunity]:
        """Current opportunities, sorted by raw_edge descending."""
        if self._sorted_cache is None:
            self._sorted_cache = sorted(
                self._opportunities.values(), key=lambda o: o.raw_edge, reverse=True
            )
        return self._sorted_cache

    @property
    def all_snapshots(self) -> dict[str, Opportunity]:
        """All monitored pairs with latest prices (including non-positive edge)."""
        return dict(self._all_snapshots)

    @property
    def pairs(self) -> list[ArbPair]:
        """Currently registered pairs."""
        return list(self._pairs)
