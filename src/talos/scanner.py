"""Arbitrage opportunity scanner for NO+NO pairs."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from talos.models.strategy import ArbPair, Opportunity
from talos.orderbook import OrderBookManager

logger = structlog.get_logger()


class ArbitrageScanner:
    """Detects NO+NO arbitrage within game events.

    Pure state machine — no I/O, no async. Reads orderbook state
    from OrderBookManager, evaluates registered pairs, maintains
    a list of current opportunities.
    """

    def __init__(self, book_manager: OrderBookManager) -> None:
        self._books = book_manager
        self._pairs: list[ArbPair] = []
        self._pairs_by_ticker: dict[str, list[ArbPair]] = {}
        self._opportunities: dict[str, Opportunity] = {}

    def add_pair(self, event_ticker: str, ticker_a: str, ticker_b: str) -> None:
        """Register a pair of markets to monitor."""
        if any(p.event_ticker == event_ticker for p in self._pairs):
            return
        pair = ArbPair(event_ticker=event_ticker, ticker_a=ticker_a, ticker_b=ticker_b)
        self._pairs.append(pair)
        self._pairs_by_ticker.setdefault(ticker_a, []).append(pair)
        self._pairs_by_ticker.setdefault(ticker_b, []).append(pair)
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
        logger.info("scanner_pair_removed", event_ticker=event_ticker)

    def scan(self, ticker: str) -> None:
        """Re-evaluate all pairs involving this ticker."""
        pairs = self._pairs_by_ticker.get(ticker, [])
        for pair in pairs:
            self._evaluate_pair(pair)

    def _evaluate_pair(self, pair: ArbPair) -> None:
        """Check one pair for arbitrage opportunity."""
        bid_a = self._books.best_bid(pair.ticker_a)
        bid_b = self._books.best_bid(pair.ticker_b)

        if not bid_a or not bid_b:
            self._opportunities.pop(pair.event_ticker, None)
            return

        book_a = self._books.get_book(pair.ticker_a)
        book_b = self._books.get_book(pair.ticker_b)
        if (book_a and book_a.stale) or (book_b and book_b.stale):
            self._opportunities.pop(pair.event_ticker, None)
            return

        raw_edge = bid_a.price + bid_b.price - 100

        if raw_edge > 0:
            opp = Opportunity(
                event_ticker=pair.event_ticker,
                ticker_a=pair.ticker_a,
                ticker_b=pair.ticker_b,
                no_a=100 - bid_a.price,
                no_b=100 - bid_b.price,
                qty_a=bid_a.quantity,
                qty_b=bid_b.quantity,
                raw_edge=raw_edge,
                tradeable_qty=min(bid_a.quantity, bid_b.quantity),
                timestamp=datetime.now(UTC).isoformat(),
            )
            self._opportunities[pair.event_ticker] = opp
            logger.debug(
                "scanner_opportunity",
                event_ticker=pair.event_ticker,
                edge=raw_edge,
                qty=opp.tradeable_qty,
            )
        else:
            self._opportunities.pop(pair.event_ticker, None)

    @property
    def opportunities(self) -> list[Opportunity]:
        """Current opportunities, sorted by raw_edge descending."""
        return sorted(self._opportunities.values(), key=lambda o: o.raw_edge, reverse=True)

    @property
    def pairs(self) -> list[ArbPair]:
        """Currently registered pairs."""
        return list(self._pairs)
