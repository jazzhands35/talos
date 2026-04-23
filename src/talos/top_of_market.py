"""Top-of-market tracking for resting bids (YES and NO)."""

from __future__ import annotations

from collections.abc import Callable

import structlog

from talos.models.order import ACTIVE_STATUSES, Order
from talos.models.strategy import ArbPair
from talos.orderbook import OrderBookManager
from talos.units import bps_to_cents_round

logger = structlog.get_logger()


def _order_remaining_fp100(order: Order) -> int:
    """Read remaining count as fp100 (post-13a-2a: direct passthrough)."""
    return order.remaining_count_fp100


def _order_price_bps(order: Order, side: str) -> int:
    """Read order price in bps for the given side."""
    if side == "no":
        return order.no_price_bps
    return order.yes_price_bps


class TopOfMarketTracker:
    """Detects when resting bids are no longer at the best book price.

    Pure state machine — no async, no I/O. Receives order data from polling
    and checks against live orderbook state on every delta.
    """

    def __init__(self, book_manager: OrderBookManager) -> None:
        self._books = book_manager
        self._resting: dict[tuple[str, str], int] = {}  # (ticker, side) -> highest resting price
        self._at_top: dict[tuple[str, str], bool] = {}  # (ticker, side) -> is at top
        self.on_change: Callable[[str, str, bool], None] | None = None  # (ticker, side, at_top)

    def update_orders(self, orders: list[Order], pairs: list[ArbPair]) -> None:
        """Refresh resting order prices from polled order data.

        Filters to resting buys on tracked pair tickers/sides. When multiple
        orders exist on the same (ticker, side), keeps the highest price.
        """
        # Build set of expected (ticker, side) combinations from pairs
        tracked: dict[str, set[str]] = {}  # ticker -> set of expected sides
        for pair in pairs:
            tracked.setdefault(pair.ticker_a, set()).add(pair.side_a)
            tracked.setdefault(pair.ticker_b, set()).add(pair.side_b)

        new_resting: dict[tuple[str, str], int] = {}
        for order in orders:
            if order.action != "buy":
                continue
            if order.status not in ACTIVE_STATUSES:
                continue
            if _order_remaining_fp100(order) <= 0:
                continue
            expected_sides = tracked.get(order.ticker)
            if expected_sides is None or order.side not in expected_sides:
                continue
            price = bps_to_cents_round(_order_price_bps(order, order.side))
            key = (order.ticker, order.side)
            prev = new_resting.get(key, 0)
            new_resting[key] = max(prev, price)

        # Clear _at_top for keys that no longer have resting orders
        for key in self._resting:
            if key not in new_resting:
                self._at_top.pop(key, None)

        self._resting = new_resting

    def check(self, ticker: str, side: str = "no") -> None:
        """Compare resting price against current best book price.

        Called on every orderbook delta. Fires ``on_change`` callback
        only when the at-top state transitions.
        """
        key = (ticker, side)
        resting_price = self._resting.get(key)
        if resting_price is None:
            return

        best = self._books.best_ask(ticker, side=side)
        if best is None:
            return

        best_price_cents = bps_to_cents_round(best.price_bps)
        now_at_top = best_price_cents <= resting_price
        was_at_top = self._at_top.get(key)

        self._at_top[key] = now_at_top

        # Fire on state transition, or on first observation if already jumped
        if now_at_top != was_at_top and (was_at_top is not None or not now_at_top):
            logger.info(
                "top_of_market_change",
                ticker=ticker,
                side=side,
                at_top=now_at_top,
                resting=resting_price,
                book_top=best_price_cents,
            )
            if self.on_change:
                self.on_change(ticker, side, now_at_top)

    def is_at_top(self, ticker: str, side: str = "no") -> bool | None:
        """Query current top-of-market state for a ticker/side.

        Returns ``None`` if no resting orders on this (ticker, side).
        """
        key = (ticker, side)
        if key not in self._resting:
            return None
        return self._at_top.get(key)

    @property
    def resting_tickers(self) -> list[str]:
        """Return tickers with resting bids."""
        return list({t for t, _ in self._resting})

    @property
    def resting_keys(self) -> list[tuple[str, str]]:
        """Return (ticker, side) keys with resting bids."""
        return list(self._resting.keys())

    def resting_price(self, ticker: str, side: str = "no") -> int | None:
        """Query the highest resting price for a (ticker, side)."""
        return self._resting.get((ticker, side))

    def book_top_price(self, ticker: str, side: str = "no") -> int | None:
        """Query the current best price on the book for a (ticker, side)."""
        best = self._books.best_ask(ticker, side=side)
        if best is None:
            return None
        return bps_to_cents_round(best.price_bps)
