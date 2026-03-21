"""BidAdjuster — async orchestrator for bid adjustment on jumps.

Receives jump events from TopOfMarketTracker, queries PositionLedger
for current state, and proposes adjustments.

See brain/principles.md Principles 15-19 for safety invariants.
"""

from __future__ import annotations

import structlog

from talos.errors import KalshiAPIError
from talos.fees import MAKER_FEE_RATE, fee_adjusted_cost
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

    @property
    def ledgers(self) -> dict[str, PositionLedger]:
        """Read-only access to all per-event ledgers."""
        return self._ledgers

    def get_ledger(self, event_ticker: str) -> PositionLedger:
        """Get the position ledger for an event."""
        return self._ledgers[event_ticker]

    def set_unit_size(self, unit_size: int) -> None:
        """Update unit size for future and existing ledgers."""
        self._unit_size = unit_size
        for ledger in self._ledgers.values():
            ledger.unit_size = unit_size

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
        to_remove = [t for t, (p, _) in self._ticker_map.items() if p.event_ticker == event_ticker]
        for t in to_remove:
            del self._ticker_map[t]

    # ── Decision logic (synchronous, testable) ──────────────────────

    def evaluate_jump(
        self,
        ticker: str,
        at_top: bool,
        exit_only: bool = False,
    ) -> ProposedAdjustment | None:
        """Evaluate a jump event and return a proposal if appropriate.

        Called by TopOfMarketTracker.on_change callback.
        Returns None if no action needed.

        When exit_only=True, only allows adjustments on the behind side
        (to catch up to delta neutral). The ahead side is blocked.
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

        # Exit-only gate: block adjustments on the ahead side
        if exit_only:
            filled_a = ledger.filled_count(Side.A)
            filled_b = ledger.filled_count(Side.B)
            if filled_a == filled_b:
                # Balanced — block all adjustments
                return None
            ahead = Side.A if filled_a > filled_b else Side.B
            if side is ahead:
                # This is the ahead side — don't adjust
                return None

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

        def _hold(reason: str) -> ProposedAdjustment:
            return ProposedAdjustment(
                event_ticker=pair.event_ticker,
                side=side.value,
                action="hold",
                reason=reason,
                position_before=(
                    f"A: {ledger.format_position(Side.A)} | B: {ledger.format_position(Side.B)}"
                ),
            )

        # Profitability check (Principle 18)
        rate = pair.fee_rate
        other_side = side.other
        if ledger.filled_count(other_side) > 0:
            other_effective = fee_adjusted_cost(
                int(round(ledger.avg_filled_price(other_side))), rate=rate
            )
        elif ledger.resting_count(other_side) > 0:
            # Use top-of-market for other side (worst case / most conservative)
            other_ticker = pair.ticker_a if other_side is Side.A else pair.ticker_b
            other_best = self._books.best_ask(other_ticker)
            other_book_price = other_best.price if other_best else ledger.resting_price(other_side)
            other_effective = fee_adjusted_cost(other_book_price, rate=rate)
        else:
            other_effective = 0.0

        this_effective = fee_adjusted_cost(new_price, rate=rate)
        if other_effective > 0 and this_effective + other_effective >= 100:
            # No fills on either side → withdraw both orders entirely.
            # With fills → hold and wait for market to return (P16).
            if ledger.filled_count(Side.A) == 0 and ledger.filled_count(Side.B) == 0:
                logger.info(
                    "jump_withdraw",
                    ticker=ticker,
                    new_price=new_price,
                    effective_sum=this_effective + other_effective,
                )
                return ProposedAdjustment(
                    event_ticker=pair.event_ticker,
                    side=side.value,
                    action="withdraw",
                    reason=(
                        f"no fills — withdraw both sides, "
                        f"following to {new_price}c not profitable "
                        f"({this_effective:.1f}+{other_effective:.1f}"
                        f"={this_effective + other_effective:.1f} >= 100)"
                    ),
                    position_before=(
                        f"A: {ledger.format_position(Side.A)} | B: {ledger.format_position(Side.B)}"
                    ),
                )
            logger.info(
                "jump_not_profitable",
                ticker=ticker,
                new_price=new_price,
                effective_sum=this_effective + other_effective,
            )
            return _hold(
                f"stay at {ledger.resting_price(side)}c — "
                f"following to {new_price}c not profitable "
                f"({this_effective:.1f}+{other_effective:.1f}"
                f"={this_effective + other_effective:.1f} >= 100)"
            )

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

            if this_remaining <= other_remaining:
                # Other side is more behind (or equal) — defer this side
                # Equal case: deterministic tiebreak by deferring this side
                self._deferred.setdefault(pair.event_ticker, set()).add(side)
                logger.info(
                    "jump_deferred",
                    ticker=ticker,
                    side=side.value,
                    reason=f"other side needs {other_remaining} vs this side {this_remaining}",
                )
                return _hold(
                    f"deferred — other side needs {other_remaining} fills "
                    f"vs this side {this_remaining}"
                )
            else:
                # This side is more behind — cancel other side's existing proposal
                evt_proposals = self._proposals.get(pair.event_ticker, {})
                if other_side in evt_proposals:
                    logger.info(
                        "proposal_superseded_by_tiebreaker",
                        event_ticker=pair.event_ticker,
                        superseded_side=other_side.value,
                        winning_side=side.value,
                    )
                    del evt_proposals[other_side]
                self._deferred.setdefault(pair.event_ticker, set()).add(other_side)

        # Build proposal — resting_order_id is guaranteed non-None (checked above)
        cancel_id = ledger.resting_order_id(side)
        assert cancel_id is not None
        cancel_count = ledger.resting_count(side)
        cancel_price = ledger.resting_price(side)
        new_count = cancel_count  # same quantity at new price

        # Safety gate check (simulating the post-cancel state)
        test_ok, test_reason = self._check_post_cancel_safety(ledger, side, new_count, new_price)
        if not test_ok:
            logger.info("jump_blocked_by_safety", ticker=ticker, reason=test_reason)
            return _hold(f"stay — safety gate: {test_reason}")

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
                f"cost: {this_effective:.1f} + {other_effective:.1f}"
                f" = {this_effective + other_effective:.1f}c"
                f" (profit {100 - this_effective - other_effective:.1f}c/pair)"
            ),
            position_before=(
                f"A: {ledger.format_position(Side.A)} | B: {ledger.format_position(Side.B)}"
            ),
            position_after=self._format_position_after(ledger, side, new_count, new_price),
            safety_check=(
                f"filled_in_unit+new="
                f"{ledger.filled_count(side) % ledger.unit_size + new_count}"
                f" <= unit({ledger.unit_size}), "
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

    def resolve_event(self, ticker: str) -> str | None:
        """Resolve a market ticker to its event ticker, or None if unknown."""
        lookup = self._ticker_map.get(ticker)
        return lookup[0].event_ticker if lookup is not None else None

    # ── Query methods ───────────────────────────────────────────────

    def has_pending_proposal(self, event_ticker: str, side: Side) -> bool:
        return side in self._proposals.get(event_ticker, {})

    def has_deferred(self, event_ticker: str, side: Side) -> bool:
        return side in self._deferred.get(event_ticker, set())

    def get_proposal(self, event_ticker: str, side: Side) -> ProposedAdjustment | None:
        return self._proposals.get(event_ticker, {}).get(side)

    def clear_proposal(self, event_ticker: str, side: Side) -> None:
        """Clear a proposal after execution or rejection."""
        evt = self._proposals.get(event_ticker)
        if evt:
            evt.pop(side, None)

    # ── Async execution ─────────────────────────────────────────────

    async def execute(self, proposal: ProposedAdjustment, rest_client: object) -> None:
        """Execute a proposed adjustment via amend (Principle 17).

        Single atomic API call — changes price on existing order.
        On failure: halt immediately, flag operator. Do NOT fall back
        to cancel-then-place.

        Args:
            proposal: the approved ProposedAdjustment
            rest_client: KalshiRESTClient instance (typed as object for testability)
        """
        side = Side(proposal.side)
        ledger = self._ledgers[proposal.event_ticker]

        # Staleness check: verify the proposal's order still matches ledger state.
        # Silently dismiss if stale — this commonly happens when rebalance
        # cancelled the order between proposal creation and execution.
        current_resting = ledger.resting_order_id(side)
        if current_resting != proposal.cancel_order_id:
            logger.info(
                "adjustment_stale_dismissed",
                event_ticker=proposal.event_ticker,
                side=side.value,
                expected=proposal.cancel_order_id,
                actual=current_resting,
            )
            self.clear_proposal(proposal.event_ticker, side)
            return

        # Find the ticker for this side
        ticker = self._side_ticker(proposal.event_ticker, side)

        # Fetch the ORDER's own state — amend needs order-specific fill_count,
        # not the ledger aggregate which includes archived orders (P7/P21).
        # See patterns.md "Order-specific APIs need order-specific data".
        try:
            fresh_order = await rest_client.get_order(  # type: ignore[attr-defined]
                proposal.cancel_order_id,
            )
        except KalshiAPIError as e:
            if e.status_code == 404:
                # Order no longer exists — cancelled or settled between
                # proposal creation and execution. Silently dismiss.
                logger.info(
                    "adjustment_order_gone",
                    event_ticker=proposal.event_ticker,
                    side=side.value,
                    order_id=proposal.cancel_order_id,
                )
                self.clear_proposal(proposal.event_ticker, side)
                return
            raise
        total_count = fresh_order.fill_count + fresh_order.remaining_count

        # Skip if the order is already at the target price (avoids AMEND_ORDER_NO_OP)
        if fresh_order.no_price == proposal.new_price:
            logger.info(
                "adjustment_already_at_target",
                event_ticker=proposal.event_ticker,
                side=side.value,
                price=proposal.new_price,
            )
            self.clear_proposal(proposal.event_ticker, side)
            return

        # Re-check P18 profitability with current ledger state.
        # Between proposal and execution, the other side may have filled
        # at a different price than expected at proposal time.
        pair_lookup = self._ticker_map.get(ticker)
        if pair_lookup is not None:
            pair, _ = pair_lookup
            ok, reason = ledger.is_placement_safe(
                side, fresh_order.remaining_count, proposal.new_price,
                rate=pair.fee_rate, catchup=True,
            )
            if not ok:
                logger.warning(
                    "adjustment_blocked_p18_recheck",
                    event_ticker=proposal.event_ticker,
                    side=side.value,
                    new_price=proposal.new_price,
                    reason=reason,
                )
                self.clear_proposal(proposal.event_ticker, side)
                return

        logger.info(
            "adjustment_amend",
            event_ticker=proposal.event_ticker,
            side=side.value,
            order_id=proposal.cancel_order_id,
            old_price=proposal.cancel_price,
            new_price=proposal.new_price,
            total_count=total_count,
            order_fills=fresh_order.fill_count,
            order_remaining=fresh_order.remaining_count,
        )

        # Single atomic amend call
        old_order, amended_order = await rest_client.amend_order(  # type: ignore[attr-defined]
            proposal.cancel_order_id,
            ticker=ticker,
            side="no",
            action="buy",
            no_price=proposal.new_price,
            count=total_count,
        )

        # Update fills from amend response (handles fills that arrived during approval)
        fill_delta = old_order.fill_count - ledger.filled_count(side)
        if fill_delta > 0:
            # Prorate maker fees for the new fills
            fee_delta = old_order.maker_fees - ledger.filled_fees(side)
            ledger.record_fill(
                side,
                count=fill_delta,
                price=old_order.no_price,
                fees=max(0, fee_delta),
            )

        # Update ledger from amend response
        ledger.record_resting(
            side,
            order_id=amended_order.order_id,
            count=amended_order.remaining_count,
            price=amended_order.no_price,
        )

        # Clear the proposal
        self.clear_proposal(proposal.event_ticker, side)

        logger.info(
            "adjustment_complete",
            event_ticker=proposal.event_ticker,
            side=side.value,
            order_id=amended_order.order_id,
            new_price=proposal.new_price,
        )

    def _side_ticker(self, event_ticker: str, side: Side) -> str:
        """Look up the market ticker for a given event + side."""
        for ticker, (pair, s) in self._ticker_map.items():
            if pair.event_ticker == event_ticker and s is side:
                return ticker
        raise ValueError(f"No ticker found for {event_ticker} side {side.value}")

    # ── Internal helpers ────────────────────────────────────────────

    def _fee_rate_for(self, event_ticker: str) -> float:
        """Look up the fee rate for a pair by event ticker."""
        for pair, _ in self._ticker_map.values():
            if pair.event_ticker == event_ticker:
                return pair.fee_rate
        return MAKER_FEE_RATE

    def _is_jumped(self, ticker: str, ledger: PositionLedger, side: Side) -> bool:
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
        # Simulate post-cancel state (use fills in current unit, not total)
        filled_in_unit = ledger.filled_count(side) % ledger.unit_size
        if filled_in_unit + new_count > ledger.unit_size:
            return (
                False,
                f"would exceed unit after cancel: filled_in_unit={filled_in_unit} + "
                f"new={new_count} > {ledger.unit_size}",
            )
        # Check profitability (reuse the gate logic without resting check)
        other_side = side.other
        if ledger.filled_count(other_side) > 0:
            other_price = ledger.filled_total_cost(other_side) / ledger.filled_count(other_side)
        elif ledger.resting_count(other_side) > 0:
            other_price = ledger.resting_price(other_side)
        else:
            return True, ""

        rate = self._fee_rate_for(ledger.event_ticker)
        effective_this = fee_adjusted_cost(new_price, rate=rate)
        effective_other = fee_adjusted_cost(int(round(other_price)), rate=rate)
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
        this_parts: list[str] = []
        if ledger.filled_count(side) > 0:
            avg = ledger.avg_filled_price(side)
            this_parts.append(f"{ledger.filled_count(side)} filled @ {avg:.1f}c")
        this_parts.append(f"{new_count} resting @ {new_price}c")

        return (
            f"{this_label}: {', '.join(this_parts)} | "
            f"{other_label}: {ledger.format_position(other)}"
        )
