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
        self._next_id: int = 1
        self._admission_warned: set[str] = set()

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
        talos_id: int = 0,
        fractional_trading_enabled: bool = False,
        tick_bps: int = 100,
    ) -> None:
        """Register a pair of markets to monitor."""
        if any(p.event_ticker == event_ticker for p in self._pairs):
            return
        assigned_id = talos_id if talos_id > 0 else self._next_id
        self._next_id = max(self._next_id, assigned_id + 1)
        pair = ArbPair(
            talos_id=assigned_id,
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
            fractional_trading_enabled=fractional_trading_enabled,
            tick_bps=tick_bps,
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

    def pairs_for_ticker(self, ticker: str) -> list[ArbPair]:
        """Return all registered pairs that include this ticker."""
        return self._pairs_by_ticker.get(ticker, [])

    def scan(self, ticker: str) -> None:
        """Re-evaluate all pairs involving this ticker."""
        pairs = self._pairs_by_ticker.get(ticker, [])
        for pair in pairs:
            self._evaluate_pair(pair)

    def _derive_price(self, ticker: str, side: str) -> tuple[int, int] | None:
        """Derive implied price from the opposite side of the book.

        When the NO side is empty but YES bids exist, the implied NO ask
        is ``100 - best_yes_bid``.  Similarly for the reverse.
        """
        opposite = "yes" if side == "no" else "no"
        level = self._books.best_ask(ticker, side=opposite)
        if level:
            return 100 - level.price, level.quantity
        return None

    def _evaluate_pair(self, pair: ArbPair) -> None:
        """Check one pair for arbitrage opportunity."""
        # Phase 0 admission guard — skip pairs whose shape violates the
        # bps/fp100 migration invariants (fractional trading or sub-cent
        # tick). Local import avoids a circular dep with game_manager.
        from talos.game_manager import ONE_CENT_BPS

        if pair.fractional_trading_enabled or pair.tick_bps < ONE_CENT_BPS:
            if pair.event_ticker not in self._admission_warned:
                self._admission_warned.add(pair.event_ticker)
                reason = (
                    "fractional_trading_enabled"
                    if pair.fractional_trading_enabled
                    else f"sub-cent tick ({pair.tick_bps} bps)"
                )
                logger.warning(
                    "scanner_admission_skip",
                    event_ticker=pair.event_ticker,
                    reason=reason,
                )
            return

        self._sorted_cache = None
        no_a = self._books.best_ask(pair.ticker_a, side=pair.side_a)
        no_b = self._books.best_ask(pair.ticker_b, side=pair.side_b)

        if not no_a or not no_b:
            self._opportunities.pop(pair.event_ticker, None)
            # Derive implied prices from opposite side for display
            existing = self._all_snapshots.get(pair.event_ticker)
            if existing is not None:
                update: dict[str, object] = {"timestamp": datetime.now(UTC).isoformat()}
                pa, qa = (no_a.price, no_a.quantity) if no_a else (None, 0)
                pb, qb = (no_b.price, no_b.quantity) if no_b else (None, 0)
                if pa is None:
                    derived = self._derive_price(pair.ticker_a, pair.side_a)
                    if derived:
                        pa, qa = derived
                if pb is None:
                    derived = self._derive_price(pair.ticker_b, pair.side_b)
                    if derived:
                        pb, qb = derived
                if pa is not None:
                    update["no_a"] = pa
                    update["qty_a"] = qa
                if pb is not None:
                    update["no_b"] = pb
                    update["qty_b"] = qb
                if pa is not None and pb is not None:
                    update["raw_edge"] = 100 - pa - pb
                    update["fee_edge"] = fee_adjusted_edge(pa, pb, rate=pair.fee_rate)
                    update["tradeable_qty"] = min(qa, qb)
                self._all_snapshots[pair.event_ticker] = existing.model_copy(
                    update=update
                )
            return

        book_a = self._books.get_book(pair.ticker_a)
        book_b = self._books.get_book(pair.ticker_b)
        if (book_a and book_a.stale) or (book_b and book_b.stale):
            self._opportunities.pop(pair.event_ticker, None)
            existing = self._all_snapshots.get(pair.event_ticker)
            if existing is not None:
                self._all_snapshots[pair.event_ticker] = existing.model_copy(
                    update={"timestamp": datetime.now(UTC).isoformat()}
                )
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

    def get_talos_id(self, event_ticker: str) -> int:
        """Look up the internal Talos ID for an event ticker."""
        for pair in self._pairs:
            if pair.event_ticker == event_ticker:
                return pair.talos_id
        return 0

    @property
    def pairs(self) -> list[ArbPair]:
        """Currently registered pairs."""
        return list(self._pairs)
