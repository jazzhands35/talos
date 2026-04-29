"""Arbitrage opportunity scanner for arbitrage pairs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import structlog

from talos.fees import fee_adjusted_edge_bps
from talos.models.market import OrderBookLevel
from talos.models.strategy import ArbPair, Opportunity
from talos.orderbook import OrderBookManager
from talos.units import (
    ONE_CENT_BPS,
    ONE_CONTRACT_FP100,
    ONE_DOLLAR_BPS,
    bps_to_cents_round,
    complement_bps,
)

logger = structlog.get_logger()


def _level_price_bps(level: OrderBookLevel) -> int:
    """Exact bps price for a level (post-13a-2b: direct passthrough)."""
    return level.price_bps


def _level_quantity_fp100(level: OrderBookLevel) -> int:
    """Exact fp100 quantity for a level (post-13a-2b: direct passthrough)."""
    return level.quantity_fp100


class ArbitrageScanner:
    """Detects arbitrage opportunities within game events.

    Pure state machine — no I/O, no async. Reads orderbook state
    from OrderBookManager, evaluates registered pairs, maintains
    a list of current opportunities.
    """

    def __init__(
        self,
        book_manager: OrderBookManager,
        id_assigner: Callable[[], int] | None = None,
    ) -> None:
        self._books = book_manager
        self._pairs: list[ArbPair] = []
        self._pairs_by_ticker: dict[str, list[ArbPair]] = {}
        self._opportunities: dict[str, Opportunity] = {}
        self._all_snapshots: dict[str, Opportunity] = {}
        self._sorted_cache: list[Opportunity] | None = None
        self._next_id: int = 1
        self._id_assigner = id_assigner

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
    ) -> int:
        """Register a pair of markets to monitor. Returns the assigned talos_id."""
        existing = next((p for p in self._pairs if p.event_ticker == event_ticker), None)
        if existing is not None:
            return existing.talos_id
        if talos_id > 0:
            assigned_id = talos_id
        elif self._id_assigner is not None:
            assigned_id = self._id_assigner()
            if assigned_id <= 0:
                raise ValueError(
                    f"id_assigner returned non-positive talos_id ({assigned_id}); "
                    f"expected a value > 0"
                )
        else:
            assigned_id = self._next_id
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
                raw_edge_bps=0,
                tradeable_qty=0,
                timestamp=datetime.now(UTC).isoformat(),
                close_time=close_time,
                fee_rate=fee_rate,
            ),
        )
        logger.info("scanner_pair_added", event_ticker=event_ticker, a=ticker_a, b=ticker_b)
        return assigned_id

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
        is ``100 - best_yes_bid``.  Similarly for the reverse. Returns
        (price_cents, quantity_contracts) derived from the bps/fp100 fields.
        """
        opposite = "yes" if side == "no" else "no"
        level = self._books.best_ask(ticker, side=opposite)
        if level:
            return (
                100 - level.price_bps // ONE_CENT_BPS,
                level.quantity_fp100 // ONE_CONTRACT_FP100,
            )
        return None

    def _derive_price_bps(self, ticker: str, side: str) -> tuple[int, int] | None:
        """Bps/fp100 sibling of :meth:`_derive_price`.

        Implied NO-ask in bps is ``ONE_DOLLAR_BPS - best_yes_bid_bps``.
        """
        opposite = "yes" if side == "no" else "no"
        level = self._books.best_ask(ticker, side=opposite)
        if level:
            return (
                complement_bps(_level_price_bps(level)),
                _level_quantity_fp100(level),
            )
        return None

    def _evaluate_pair(self, pair: ArbPair) -> None:
        """Check one pair for arbitrage opportunity.

        Post-migration (Task 12): fractional_trading_enabled and sub-cent-tick
        pairs flow through unchanged. The Phase 0 admission guard that used
        to short-circuit such pairs has been removed now that the bps/fp100
        edge computation (``_level_price_bps`` / ``fee_adjusted_edge_bps``)
        preserves exact sub-cent precision end-to-end.
        """
        self._sorted_cache = None
        no_a = self._books.best_ask(pair.ticker_a, side=pair.side_a)
        no_b = self._books.best_ask(pair.ticker_b, side=pair.side_b)

        if not no_a or not no_b:
            self._opportunities.pop(pair.event_ticker, None)
            # Derive implied prices from opposite side for display
            existing = self._all_snapshots.get(pair.event_ticker)
            if existing is not None:
                update: dict[str, object] = {"timestamp": datetime.now(UTC).isoformat()}
                pa, qa = (
                    (
                        no_a.price_bps // ONE_CENT_BPS,
                        no_a.quantity_fp100 // ONE_CONTRACT_FP100,
                    )
                    if no_a
                    else (None, 0)
                )
                pb, qb = (
                    (
                        no_b.price_bps // ONE_CENT_BPS,
                        no_b.quantity_fp100 // ONE_CONTRACT_FP100,
                    )
                    if no_b
                    else (None, 0)
                )
                # Parallel bps / fp100 extraction — see _level_price_bps.
                pa_bps: int | None = _level_price_bps(no_a) if no_a else None
                qa_fp100: int = _level_quantity_fp100(no_a) if no_a else 0
                pb_bps: int | None = _level_price_bps(no_b) if no_b else None
                qb_fp100: int = _level_quantity_fp100(no_b) if no_b else 0
                if pa is None:
                    derived = self._derive_price(pair.ticker_a, pair.side_a)
                    if derived:
                        pa, qa = derived
                    derived_bps = self._derive_price_bps(pair.ticker_a, pair.side_a)
                    if derived_bps:
                        pa_bps, qa_fp100 = derived_bps
                if pb is None:
                    derived = self._derive_price(pair.ticker_b, pair.side_b)
                    if derived:
                        pb, qb = derived
                    derived_bps = self._derive_price_bps(pair.ticker_b, pair.side_b)
                    if derived_bps:
                        pb_bps, qb_fp100 = derived_bps
                if pa is not None:
                    update["no_a"] = pa
                    update["qty_a"] = qa
                if pb is not None:
                    update["no_b"] = pb
                    update["qty_b"] = qb
                if pa_bps is not None:
                    update["no_a_bps"] = pa_bps
                    update["qty_a_fp100"] = qa_fp100
                if pb_bps is not None:
                    update["no_b_bps"] = pb_bps
                    update["qty_b_fp100"] = qb_fp100
                if pa is not None and pb is not None:
                    update["raw_edge"] = 100 - pa - pb
                    # fee_edge historically returns float cents; derive from bps.
                    update["fee_edge"] = (
                        fee_adjusted_edge_bps(
                            pa * ONE_CENT_BPS, pb * ONE_CENT_BPS, rate=pair.fee_rate
                        )
                        / ONE_CENT_BPS
                    )
                    update["tradeable_qty"] = min(qa, qb)
                if pa_bps is not None and pb_bps is not None:
                    update["raw_edge_bps"] = ONE_DOLLAR_BPS - pa_bps - pb_bps
                    update["fee_edge_bps"] = fee_adjusted_edge_bps(
                        pa_bps, pb_bps, rate=pair.fee_rate
                    )
                    update["tradeable_qty_fp100"] = min(qa_fp100, qb_fp100)
                self._all_snapshots[pair.event_ticker] = existing.model_copy(update=update)
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

        # Exact bps edge — authoritative. Legacy cents `raw_edge` is a
        # cents-rounded view of the exact edge (lossy on sub-cent markets).
        pa_bps = _level_price_bps(no_a)
        pb_bps = _level_price_bps(no_b)
        qa_fp100 = _level_quantity_fp100(no_a)
        qb_fp100 = _level_quantity_fp100(no_b)
        raw_edge_bps = ONE_DOLLAR_BPS - pa_bps - pb_bps
        raw_edge = bps_to_cents_round(raw_edge_bps)
        fee_edge_bps = fee_adjusted_edge_bps(pa_bps, pb_bps, rate=pair.fee_rate)
        # Preserve the historical float-cents semantics of ``fee_edge``:
        # pass the cent-rounded prices through the legacy formula so
        # whole-cent tests continue to see the exact same fractional value.
        no_a_cents = pa_bps // ONE_CENT_BPS
        no_b_cents = pb_bps // ONE_CENT_BPS
        no_a_qty = qa_fp100 // ONE_CONTRACT_FP100
        no_b_qty = qb_fp100 // ONE_CONTRACT_FP100
        # fee_edge historically returns float cents; derive from bps.
        fee_edge = (
            fee_adjusted_edge_bps(
                no_a_cents * ONE_CENT_BPS,
                no_b_cents * ONE_CENT_BPS,
                rate=pair.fee_rate,
            )
            / ONE_CENT_BPS
        )
        tradeable_qty = min(no_a_qty, no_b_qty)
        tradeable_qty_fp100 = min(qa_fp100, qb_fp100)

        opp = Opportunity(
            event_ticker=pair.event_ticker,
            ticker_a=pair.ticker_a,
            ticker_b=pair.ticker_b,
            no_a=no_a_cents,
            no_b=no_b_cents,
            qty_a=no_a_qty,
            qty_b=no_b_qty,
            raw_edge=raw_edge,
            fee_edge=fee_edge,
            tradeable_qty=tradeable_qty,
            timestamp=datetime.now(UTC).isoformat(),
            close_time=pair.close_time,
            fee_rate=pair.fee_rate,
            no_a_bps=pa_bps,
            no_b_bps=pb_bps,
            qty_a_fp100=qa_fp100,
            qty_b_fp100=qb_fp100,
            raw_edge_bps=raw_edge_bps,
            fee_edge_bps=fee_edge_bps,
            tradeable_qty_fp100=tradeable_qty_fp100,
        )
        self._all_snapshots[pair.event_ticker] = opp

        # Admission check: gate on the exact bps edge, not the lossy
        # cents view. On sub-cent markets (e.g. DJT at 3.8¢/96.1¢)
        # ``raw_edge`` rounds to 0 while ``raw_edge_bps`` preserves the
        # true edge (10 bps = 0.10¢). Without this fix, sub-cent
        # opportunities are silently dropped — the entire point of the
        # Task 7a migration.
        if raw_edge_bps > 0:
            self._opportunities[pair.event_ticker] = opp
            logger.debug(
                "scanner_opportunity",
                event_ticker=pair.event_ticker,
                edge=raw_edge,
                edge_bps=raw_edge_bps,
                qty=opp.tradeable_qty,
            )
        else:
            self._opportunities.pop(pair.event_ticker, None)

    def get_opportunity(self, event_ticker: str) -> Opportunity | None:
        """Look up a single opportunity by event ticker."""
        return self._opportunities.get(event_ticker)

    @property
    def opportunities(self) -> list[Opportunity]:
        """Current opportunities, sorted by ``raw_edge_bps`` descending.

        Sort key migrated from ``raw_edge`` (cents, lossy) to
        ``raw_edge_bps`` (exact) so sub-cent opportunities order correctly
        against each other. For whole-cent markets the ordering is
        identical (each cent of edge corresponds to 100 bps).
        """
        if self._sorted_cache is None:
            self._sorted_cache = sorted(
                self._opportunities.values(),
                key=lambda o: o.raw_edge_bps,
                reverse=True,
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
