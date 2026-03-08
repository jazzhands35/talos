"""BidAdjuster — async orchestrator for bid adjustment on jumps.

Receives jump events from TopOfMarketTracker, queries PositionLedger
for current state, and proposes adjustments.

See brain/principles.md Principles 15-19 for safety invariants.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

from talos.fees import fee_adjusted_cost
from talos.models.adjustment import ProposedAdjustment
from talos.models.strategy import ArbPair
from talos.orderbook import OrderBookManager
from talos.position_ledger import PositionLedger, Side

logger = structlog.get_logger()


class BidAdjuster:
    """Proposes bid adjustments when resting orders get jumped.

    Pure decision logic (evaluate_jump) is synchronous and testable.
    Async execution (execute) is separated for the orchestrator layer.
    """

    def __init__(
        self,
        book_manager: OrderBookManager,
        pairs: list[ArbPair],
        unit_size: int = 10,
    ) -> None:
        self._books = book_manager
        self._unit_size = unit_size

        # Ticker → (pair, side) lookup
        self._ticker_map: dict[str, tuple[ArbPair, Side]] = {}
        for pair in pairs:
            self._ticker_map[pair.ticker_a] = (pair, Side.A)
            self._ticker_map[pair.ticker_b] = (pair, Side.B)

        # Per-event ledgers
        self._ledgers: dict[str, PositionLedger] = {}
        for pair in pairs:
            self._ledgers[pair.event_ticker] = PositionLedger(
                event_ticker=pair.event_ticker, unit_size=unit_size
            )

        # Pending proposals: event_ticker → {side → proposal}
        self._proposals: dict[str, dict[Side, ProposedAdjustment]] = {}

        # Deferred jumps: event_ticker → set of deferred sides
        self._deferred: dict[str, set[Side]] = {}

        # Callback for emitting proposals to the UI
        self.on_proposal: Callable[[ProposedAdjustment], None] | None = None

    def get_ledger(self, event_ticker: str) -> PositionLedger:
        """Get the position ledger for an event."""
        return self._ledgers[event_ticker]

    def add_event(self, pair: ArbPair) -> None:
        """Register a new event pair."""
        self._ticker_map[pair.ticker_a] = (pair, Side.A)
        self._ticker_map[pair.ticker_b] = (pair, Side.B)
        self._ledgers[pair.event_ticker] = PositionLedger(
            event_ticker=pair.event_ticker, unit_size=self._unit_size
        )

    def remove_event(self, event_ticker: str) -> None:
        """Unregister an event pair."""
        self._ledgers.pop(event_ticker, None)
        self._proposals.pop(event_ticker, None)
        self._deferred.pop(event_ticker, None)
        # Clean ticker map
        to_remove = [
            t for t, (p, _) in self._ticker_map.items()
            if p.event_ticker == event_ticker
        ]
        for t in to_remove:
            del self._ticker_map[t]

    # ── Decision logic (synchronous, testable) ──────────────────────

    def evaluate_jump(
        self, ticker: str, at_top: bool
    ) -> ProposedAdjustment | None:
        """Evaluate a jump event and return a proposal if appropriate.

        Called by TopOfMarketTracker.on_change callback.
        Returns None if no action needed.
        """
        lookup = self._ticker_map.get(ticker)
        if lookup is None:
            return None

        pair, side = lookup

        # Back at top — nothing to do
        if at_top:
            # Clear any deferred for this side
            deferred = self._deferred.get(pair.event_ticker, set())
            deferred.discard(side)
            return None

        ledger = self._ledgers[pair.event_ticker]

        # No resting order on this side — nothing to adjust
        if ledger.resting_order_id(side) is None:
            return None

        # Get new top-of-market price
        best = self._books.best_ask(ticker)
        if best is None:
            return None
        new_price = best.price

        # If new price equals current resting price, no action needed
        if new_price <= ledger.resting_price(side):
            return None

        # Profitability check (Principle 18)
        other_side = side.other
        if ledger.filled_count(other_side) > 0:
            other_effective = fee_adjusted_cost(
                int(round(ledger.avg_filled_price(other_side)))
            )
        elif ledger.resting_count(other_side) > 0:
            # Use top-of-market for other side (worst case / most conservative)
            other_ticker = (
                pair.ticker_a if other_side is Side.A else pair.ticker_b
            )
            other_best = self._books.best_ask(other_ticker)
            other_book_price = other_best.price if other_best else ledger.resting_price(other_side)
            other_effective = fee_adjusted_cost(other_book_price)
        else:
            other_effective = 0.0

        this_effective = fee_adjusted_cost(new_price)
        if other_effective > 0 and this_effective + other_effective >= 100:
            logger.info(
                "jump_not_profitable",
                ticker=ticker,
                new_price=new_price,
                effective_sum=this_effective + other_effective,
            )
            return None

        # Dual-jump tiebreaker (Principle 19)
        other_ticker = pair.ticker_a if other_side is Side.A else pair.ticker_b
        other_jumped = self._is_jumped(other_ticker, ledger, other_side)
        if other_jumped:
            this_remaining = ledger.unit_remaining(side)
            other_remaining = ledger.unit_remaining(other_side)
            if this_remaining == 0:
                this_remaining = ledger.resting_count(side)
            if other_remaining == 0:
                other_remaining = ledger.resting_count(other_side)

            if this_remaining < other_remaining:
                # Other side is more behind — defer this side
                self._deferred.setdefault(pair.event_ticker, set()).add(side)
                logger.info(
                    "jump_deferred",
                    ticker=ticker,
                    side=side.value,
                    reason=f"other side needs {other_remaining} vs this side {this_remaining}",
                )
                return None

        # Build proposal — resting_order_id is guaranteed non-None (checked above)
        cancel_id = ledger.resting_order_id(side)
        assert cancel_id is not None
        cancel_count = ledger.resting_count(side)
        cancel_price = ledger.resting_price(side)
        new_count = cancel_count  # same quantity at new price

        # Safety gate check (simulating the post-cancel state)
        test_ok, test_reason = self._check_post_cancel_safety(
            ledger, side, new_count, new_price
        )
        if not test_ok:
            logger.info("jump_blocked_by_safety", ticker=ticker, reason=test_reason)
            return None

        proposal = ProposedAdjustment(
            event_ticker=pair.event_ticker,
            side=side.value,
            action="follow_jump",
            cancel_order_id=cancel_id,
            cancel_count=cancel_count,
            cancel_price=cancel_price,
            new_count=new_count,
            new_price=new_price,
            reason=(
                f"jumped {cancel_price}c->{new_price}c, "
                f"arb: {this_effective:.1f}+{other_effective:.1f}"
                f"={this_effective + other_effective:.1f} < 100"
            ),
            position_before=(
                f"A: {ledger.format_position(Side.A)} | "
                f"B: {ledger.format_position(Side.B)}"
            ),
            position_after=self._format_position_after(ledger, side, new_count, new_price),
            safety_check=(
                f"filled+new={ledger.filled_count(side)+new_count} <= "
                f"unit({ledger.unit_size}), "
                f"arb={this_effective + other_effective:.1f}c < 100"
            ),
        )

        # Store as pending (supersedes any existing proposal on this side)
        evt_proposals = self._proposals.setdefault(pair.event_ticker, {})
        old = evt_proposals.get(side)
        if old is not None:
            logger.info("proposal_superseded", event_ticker=pair.event_ticker, side=side.value)
        evt_proposals[side] = proposal

        # Clear deferred flag for this side
        deferred = self._deferred.get(pair.event_ticker, set())
        deferred.discard(side)

        if self.on_proposal:
            self.on_proposal(proposal)

        return proposal

    def on_side_complete(
        self, event_ticker: str, completed_side: Side
    ) -> ProposedAdjustment | None:
        """Called when a side's unit completes. Re-evaluates deferred jumps.

        Returns a proposal for the deferred side if still appropriate.
        """
        deferred = self._deferred.get(event_ticker, set())
        other = completed_side.other
        if other not in deferred:
            return None

        deferred.discard(other)

        # Find the ticker for the deferred side
        for ticker, (pair, side) in self._ticker_map.items():
            if pair.event_ticker == event_ticker and side is other:
                # Re-evaluate the jump
                return self.evaluate_jump(ticker, at_top=False)
        return None

    # ── Query methods ───────────────────────────────────────────────

    def has_pending_proposal(self, event_ticker: str, side: Side) -> bool:
        return side in self._proposals.get(event_ticker, {})

    def has_deferred(self, event_ticker: str, side: Side) -> bool:
        return side in self._deferred.get(event_ticker, set())

    def get_proposal(
        self, event_ticker: str, side: Side
    ) -> ProposedAdjustment | None:
        return self._proposals.get(event_ticker, {}).get(side)

    def clear_proposal(self, event_ticker: str, side: Side) -> None:
        """Clear a proposal after execution or rejection."""
        evt = self._proposals.get(event_ticker)
        if evt:
            evt.pop(side, None)

    # ── Internal helpers ────────────────────────────────────────────

    def _is_jumped(
        self, ticker: str, ledger: PositionLedger, side: Side
    ) -> bool:
        """Check if a side has been jumped (book price > resting price)."""
        if ledger.resting_order_id(side) is None:
            return False
        best = self._books.best_ask(ticker)
        if best is None:
            return False
        return best.price > ledger.resting_price(side)

    def _check_post_cancel_safety(
        self,
        ledger: PositionLedger,
        side: Side,
        new_count: int,
        new_price: int,
    ) -> tuple[bool, str]:
        """Check safety as if the existing resting order were already cancelled."""
        s = ledger._sides[side]
        # Simulate post-cancel state
        if s.filled_count + new_count > ledger.unit_size:
            return (
                False,
                f"would exceed unit after cancel: filled={s.filled_count} + "
                f"new={new_count} > {ledger.unit_size}",
            )
        # Check profitability (reuse the gate logic without resting check)
        other = ledger._sides[side.other]
        if other.filled_count > 0:
            other_price = other.filled_total_cost / other.filled_count
        elif other.resting_count > 0:
            other_price = other.resting_price
        else:
            return True, ""

        effective_this = fee_adjusted_cost(new_price)
        effective_other = fee_adjusted_cost(int(round(other_price)))
        if effective_this + effective_other >= 100:
            return (
                False,
                f"arb not profitable: {effective_this:.2f}+{effective_other:.2f} >= 100",
            )
        return True, ""

    def _format_position_after(
        self,
        ledger: PositionLedger,
        side: Side,
        new_count: int,
        new_price: int,
    ) -> str:
        """Format projected position string for proposals."""
        other = side.other
        this_label = side.value
        other_label = other.value
        # After cancel+place: resting changes, filled stays
        s = ledger._sides[side]
        this_parts: list[str] = []
        if s.filled_count > 0:
            avg = ledger.avg_filled_price(side)
            this_parts.append(f"{s.filled_count} filled @ {avg:.1f}c")
        this_parts.append(f"{new_count} resting @ {new_price}c")

        return (
            f"{this_label}: {', '.join(this_parts)} | "
            f"{other_label}: {ledger.format_position(other)}"
        )
