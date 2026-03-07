"""Top-of-market tracking for resting NO bids."""

from __future__ import annotations

from collections.abc import Callable

import structlog

from talos.models.order import ACTIVE_STATUSES, Order
from talos.models.strategy import ArbPair
from talos.orderbook import OrderBookManager

logger = structlog.get_logger()


class TopOfMarketTracker:
    """Detects when resting NO bids are no longer at the best book price.

    Pure state machine — no async, no I/O. Receives order data from polling
    and checks against live orderbook state on every delta.
    """

    def __init__(self, book_manager: OrderBookManager) -> None:
        self._books = book_manager
        self._resting: dict[str, int] = {}  # ticker -> highest resting NO price
        self._at_top: dict[str, bool] = {}  # ticker -> is at top
        self.on_change: Callable[[str, bool], None] | None = None

    def update_orders(self, orders: list[Order], pairs: list[ArbPair]) -> None:
        """Refresh resting order prices from polled order data.

        Filters to resting NO buys on tracked pair tickers. When multiple
        orders exist on the same ticker, keeps the highest NO price.
        """
        tracked: set[str] = set()
        for pair in pairs:
            tracked.add(pair.ticker_a)
            tracked.add(pair.ticker_b)

        new_resting: dict[str, int] = {}
        for order in orders:
            if order.side != "no" or order.action != "buy":
                continue
            if order.status not in ACTIVE_STATUSES:
                continue
            if order.remaining_count <= 0:
                continue
            if order.ticker not in tracked:
                continue
            prev = new_resting.get(order.ticker, 0)
            new_resting[order.ticker] = max(prev, order.no_price)

        # Clear state for tickers that no longer have resting orders
        for ticker in list(self._resting.keys()):
            if ticker not in new_resting:
                self._resting.pop(ticker)
                self._at_top.pop(ticker, None)

        self._resting = new_resting

    def check(self, ticker: str) -> None:
        """Compare resting price against current best book price.

        Called on every orderbook delta. Fires ``on_change`` callback
        only when the at-top state transitions.
        """
        resting_price = self._resting.get(ticker)
        if resting_price is None:
            return

        best = self._books.best_ask(ticker)
        if best is None:
            return

        now_at_top = best.price <= resting_price
        was_at_top = self._at_top.get(ticker)

        self._at_top[ticker] = now_at_top

        if was_at_top is not None and now_at_top != was_at_top:
            logger.info(
                "top_of_market_change",
                ticker=ticker,
                at_top=now_at_top,
                resting=resting_price,
                book_top=best.price,
            )
            if self.on_change:
                self.on_change(ticker, now_at_top)

    def is_at_top(self, ticker: str) -> bool | None:
        """Query current top-of-market state for a ticker.

        Returns ``None`` if no resting orders on this ticker.
        """
        if ticker not in self._resting:
            return None
        return self._at_top.get(ticker)

    def resting_price(self, ticker: str) -> int | None:
        """Query the highest resting NO price for a ticker."""
        return self._resting.get(ticker)
