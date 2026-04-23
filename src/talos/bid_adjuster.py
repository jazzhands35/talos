"""BidAdjuster — async orchestrator for bid adjustment on jumps.

Receives jump events from TopOfMarketTracker, queries PositionLedger
for current state, and proposes adjustments.

See brain/principles.md Principles 15-19 for safety invariants.
"""

from __future__ import annotations

import structlog

from talos.automation_config import DEFAULT_UNIT_SIZE
from talos.data_collector import DataCollector
from talos.errors import KalshiAPIError
from talos.fees import MAKER_FEE_RATE, fee_adjusted_cost_bps
from talos.models.adjustment import ProposedAdjustment
from talos.models.strategy import ArbPair
from talos.orderbook import OrderBookManager
from talos.position_ledger import PositionLedger, Side
from talos.units import (
    ONE_CENT_BPS,
    ONE_CONTRACT_FP100,
    ONE_DOLLAR_BPS,
    bps_to_cents_round,
    cents_to_bps,
)

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
        unit_size: int = DEFAULT_UNIT_SIZE,
        data_collector: DataCollector | None = None,
    ) -> None:
        self._books = book_manager
        self._unit_size = unit_size
        self._data_collector = data_collector

        # Ticker → list of (pair, side) — list handles same-ticker pairs
        self._ticker_map: dict[str, list[tuple[ArbPair, Side]]] = {}
        for pair in pairs:
            self._register_pair(pair)

        # Per-event ledgers
        self._ledgers: dict[str, PositionLedger] = {}
        for pair in pairs:
            self._ledgers[pair.event_ticker] = PositionLedger(
                event_ticker=pair.event_ticker,
                unit_size=unit_size,
                side_a_str=pair.side_a,
                side_b_str=pair.side_b,
                is_same_ticker=pair.is_same_ticker,
            )

        # Pending proposals: event_ticker → {side → proposal}
        self._proposals: dict[str, dict[Side, ProposedAdjustment]] = {}


    @property
    def ledgers(self) -> dict[str, PositionLedger]:
        """Read-only access to all per-event ledgers."""
        return self._ledgers

    @property
    def unit_size(self) -> int:
        """Current unit size used for new and existing ledgers."""
        return self._unit_size

    def get_ledger(self, event_ticker: str) -> PositionLedger:
        """Get the position ledger for an event."""
        return self._ledgers[event_ticker]

    def set_unit_size(self, unit_size: int) -> None:
        """Update unit size for future and existing ledgers."""
        self._unit_size = unit_size
        for ledger in self._ledgers.values():
            ledger.unit_size = unit_size

    def _register_pair(self, pair: ArbPair) -> None:
        """Add a pair to the ticker map. Handles same-ticker pairs."""
        self._ticker_map.setdefault(pair.ticker_a, []).append((pair, Side.A))
        self._ticker_map.setdefault(pair.ticker_b, []).append((pair, Side.B))

    def resolve_pair(
        self,
        ticker: str,
        order_side: str | None = None,
    ) -> tuple[ArbPair, Side] | None:
        """Resolve a ticker to (pair, side). Disambiguates same-ticker pairs by order_side."""
        entries = self._ticker_map.get(ticker)
        if not entries:
            return None
        if len(entries) == 1:
            return entries[0]
        # Same-ticker: disambiguate by order side
        if order_side is not None:
            for pair, side in entries:
                pair_side = pair.side_a if side == Side.A else pair.side_b
                if pair_side == order_side:
                    return (pair, side)
        return entries[0]  # fallback

    def add_event(self, pair: ArbPair) -> None:
        """Register a new event pair.

        If a ledger already exists for this event_ticker (multi-market event
        with a different pair), skip creation to avoid clobbering the
        existing ledger's state.
        """
        self._register_pair(pair)
        if pair.event_ticker in self._ledgers:
            return  # Preserve existing ledger for this event
        self._ledgers[pair.event_ticker] = PositionLedger(
            event_ticker=pair.event_ticker,
            unit_size=self._unit_size,
            side_a_str=pair.side_a,
            side_b_str=pair.side_b,
            is_same_ticker=pair.is_same_ticker,
            ticker_a=pair.ticker_a,
            ticker_b=pair.ticker_b,
        )

    def remove_event(self, event_ticker: str) -> None:
        """Unregister an event pair."""
        self._ledgers.pop(event_ticker, None)
        self._proposals.pop(event_ticker, None)
        # Clean ticker map — remove entries for this event
        to_clean: list[str] = []
        for ticker, entries in self._ticker_map.items():
            self._ticker_map[ticker] = [
                (p, s) for p, s in entries if p.event_ticker != event_ticker
            ]
            if not self._ticker_map[ticker]:
                to_clean.append(ticker)
        for ticker in to_clean:
            del self._ticker_map[ticker]

    # ── Decision logic (synchronous, testable) ──────────────────────

    def _log_decision(
        self,
        *,
        event_ticker: str,
        ticker: str,
        adj_side: Side | None,
        trigger: str,
        outcome: str,
        reason: str,
        book_top: int | None = None,
        resting_price: int | None = None,
        resting_count: int | None = None,
        new_price: int | None = None,
        effective_this: float | None = None,
        effective_other: float | None = None,
        exit_only: bool | None = None,
    ) -> None:
        """No-op if no data_collector was injected."""
        if self._data_collector is None:
            return
        self._data_collector.log_decision(
            event_ticker=event_ticker,
            ticker=ticker,
            side=adj_side.value if adj_side is not None else "",
            trigger=trigger,
            outcome=outcome,
            reason=reason,
            book_top=book_top,
            resting_price=resting_price,
            resting_count=resting_count,
            new_price=new_price,
            effective_this=effective_this,
            effective_other=effective_other,
            exit_only=exit_only,
        )

    def evaluate_jump(
        self,
        ticker: str,
        at_top: bool,
        exit_only: bool = False,
        side: str = "no",
        trigger: str = "ws_top_change",
    ) -> ProposedAdjustment | None:
        """Evaluate a jump event and return a proposal if appropriate.

        Called by TopOfMarketTracker.on_change callback.
        Returns None if no action needed.

        When exit_only=True, only allows adjustments on the behind side
        (to catch up to delta neutral). The ahead side is blocked.

        ``side`` is the order side ("yes" or "no") used to disambiguate
        same-ticker YES/NO pairs.

        ``trigger`` labels the origin of the call for the replay timeline
        (e.g. "ws_top_change", "reevaluate_jumps", "side_complete").
        """
        result = self.resolve_pair(ticker, order_side=side)
        if result is None:
            self._log_decision(
                event_ticker="",
                ticker=ticker,
                adj_side=None,
                trigger=trigger,
                outcome="skip_resolve_fail",
                reason="ticker not registered in adjuster",
                exit_only=exit_only,
            )
            return None

        pair, adj_side = result

        # Back at top — nothing to do
        if at_top:
            self._log_decision(
                event_ticker=pair.event_ticker,
                ticker=ticker,
                adj_side=adj_side,
                trigger=trigger,
                outcome="skip_at_top",
                reason="back at top of book",
                exit_only=exit_only,
            )
            return None

        ledger = self._ledgers[pair.event_ticker]

        # Determine the order side string for this adj_side
        pair_side = pair.side_a if adj_side == Side.A else pair.side_b
        book_top_price = None
        best_probe = self._books.best_ask(ticker, side=pair_side)
        if best_probe is not None:
            # Prefer exact-precision price_bps; fall back to legacy cents for
            # fixtures that only populate the legacy field.
            probe_bps = (
                best_probe.price_bps
                if best_probe.price_bps
                else cents_to_bps(best_probe.price)
            )
            book_top_price = bps_to_cents_round(probe_bps)
        cur_resting_price = ledger.resting_price(adj_side)
        cur_resting_count = ledger.resting_count(adj_side)

        # Exit-only gate: block adjustments on the ahead side
        if exit_only:
            filled_a = ledger.filled_count(Side.A)
            filled_b = ledger.filled_count(Side.B)
            if filled_a == filled_b:
                # Balanced — block all adjustments
                self._log_decision(
                    event_ticker=pair.event_ticker,
                    ticker=ticker,
                    adj_side=adj_side,
                    trigger=trigger,
                    outcome="skip_exit_only_balanced",
                    reason=f"exit-only, balanced ({filled_a}=={filled_b})",
                    book_top=book_top_price,
                    resting_price=cur_resting_price,
                    resting_count=cur_resting_count,
                    exit_only=True,
                )
                return None
            ahead = Side.A if filled_a > filled_b else Side.B
            if adj_side is ahead:
                # This is the ahead side — don't adjust
                self._log_decision(
                    event_ticker=pair.event_ticker,
                    ticker=ticker,
                    adj_side=adj_side,
                    trigger=trigger,
                    outcome="skip_exit_only_ahead",
                    reason=f"exit-only, this side ahead ({filled_a} vs {filled_b})",
                    book_top=book_top_price,
                    resting_price=cur_resting_price,
                    resting_count=cur_resting_count,
                    exit_only=True,
                )
                return None

        # No resting order on this side — nothing to adjust
        if ledger.resting_order_id(adj_side) is None:
            self._log_decision(
                event_ticker=pair.event_ticker,
                ticker=ticker,
                adj_side=adj_side,
                trigger=trigger,
                outcome="skip_no_resting",
                reason="no resting order on this side",
                book_top=book_top_price,
                exit_only=exit_only,
            )
            return None

        # Get new top-of-market price
        best = self._books.best_ask(ticker, side=pair_side)
        if best is None:
            self._log_decision(
                event_ticker=pair.event_ticker,
                ticker=ticker,
                adj_side=adj_side,
                trigger=trigger,
                outcome="skip_no_book",
                reason="no best_ask available",
                resting_price=cur_resting_price,
                resting_count=cur_resting_count,
                exit_only=exit_only,
            )
            return None
        # Internal profitability math runs in bps; the proposal's new_price is
        # the whole-cent value Talos will submit to Kalshi (output boundary —
        # Kalshi accepts whole-cent prices only for amend/place).
        new_price_bps = (
            best.price_bps if best.price_bps else cents_to_bps(best.price)
        )
        new_price = bps_to_cents_round(new_price_bps)

        # If new price equals current resting price, no action needed
        if new_price <= ledger.resting_price(adj_side):
            self._log_decision(
                event_ticker=pair.event_ticker,
                ticker=ticker,
                adj_side=adj_side,
                trigger=trigger,
                outcome="skip_stale_book",
                reason=(
                    f"new_price {new_price} <= resting "
                    f"{ledger.resting_price(adj_side)}"
                ),
                book_top=new_price,
                resting_price=cur_resting_price,
                resting_count=cur_resting_count,
                new_price=new_price,
                exit_only=exit_only,
            )
            return None

        def _hold(reason: str) -> ProposedAdjustment:
            return ProposedAdjustment(
                event_ticker=pair.event_ticker,
                side=adj_side.value,
                action="hold",
                reason=reason,
                position_before=(
                    f"A: {ledger.format_position(Side.A)} | B: {ledger.format_position(Side.B)}"
                ),
            )

        # Profitability check (Principle 18) — internal math in bps; display
        # float-cents values are derived at the end for log/reason strings.
        rate = pair.fee_rate
        other_side = adj_side.other
        if ledger.open_count(other_side) > 0:
            # avg_filled_price_bps returns bps-per-whole-contract; round to the
            # nearest whole cent (100 bps) for the per-contract fee formula.
            other_avg_bps = ledger.avg_filled_price_bps(other_side)
            other_price_bps = int(round(other_avg_bps / ONE_CENT_BPS)) * ONE_CENT_BPS
            other_effective_bps = fee_adjusted_cost_bps(other_price_bps, rate=rate)
        elif ledger.resting_count(other_side) > 0:
            # Use top-of-market for other side (worst case / most conservative)
            other_ticker = pair.ticker_a if other_side is Side.A else pair.ticker_b
            other_pair_side = pair.side_a if other_side is Side.A else pair.side_b
            other_best = self._books.best_ask(other_ticker, side=other_pair_side)
            if other_best is not None:
                other_price_bps = (
                    other_best.price_bps
                    if other_best.price_bps
                    else cents_to_bps(other_best.price)
                )
            else:
                other_price_bps = ledger.resting_price_bps(other_side)
            other_effective_bps = fee_adjusted_cost_bps(other_price_bps, rate=rate)
        else:
            other_effective_bps = 0

        this_effective_bps = fee_adjusted_cost_bps(new_price_bps, rate=rate)
        # Float-cent views for log fields and human-readable reason strings.
        this_effective = this_effective_bps / ONE_CENT_BPS
        other_effective = other_effective_bps / ONE_CENT_BPS
        if (
            other_effective_bps > 0
            and this_effective_bps + other_effective_bps >= ONE_DOLLAR_BPS
        ):
            # No fills on either side → withdraw both orders entirely.
            # With fills → hold and wait for market to return (P16).
            if ledger.filled_count(Side.A) == 0 and ledger.filled_count(Side.B) == 0:
                logger.info(
                    "jump_withdraw",
                    ticker=ticker,
                    new_price=new_price,
                    effective_sum=this_effective + other_effective,
                )
                withdraw_reason = (
                    f"no fills — withdraw both sides, "
                    f"following to {new_price}c not profitable "
                    f"({this_effective:.1f}+{other_effective:.1f}"
                    f"={this_effective + other_effective:.1f} >= 100)"
                )
                self._log_decision(
                    event_ticker=pair.event_ticker,
                    ticker=ticker,
                    adj_side=adj_side,
                    trigger=trigger,
                    outcome="withdraw",
                    reason=withdraw_reason,
                    book_top=new_price,
                    resting_price=cur_resting_price,
                    resting_count=cur_resting_count,
                    new_price=new_price,
                    effective_this=this_effective,
                    effective_other=other_effective,
                    exit_only=exit_only,
                )
                return ProposedAdjustment(
                    event_ticker=pair.event_ticker,
                    side=adj_side.value,
                    action="withdraw",
                    reason=withdraw_reason,
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
            hold_reason = (
                f"stay at {ledger.resting_price(adj_side)}c — "
                f"following to {new_price}c not profitable "
                f"({this_effective:.1f}+{other_effective:.1f}"
                f"={this_effective + other_effective:.1f} >= 100)"
            )
            self._log_decision(
                event_ticker=pair.event_ticker,
                ticker=ticker,
                adj_side=adj_side,
                trigger=trigger,
                outcome="hold_unprofitable",
                reason=hold_reason,
                book_top=new_price,
                resting_price=cur_resting_price,
                resting_count=cur_resting_count,
                new_price=new_price,
                effective_this=this_effective,
                effective_other=other_effective,
                exit_only=exit_only,
            )
            return _hold(hold_reason)

        # Build proposal — resting_order_id is guaranteed non-None (checked above)
        cancel_id = ledger.resting_order_id(adj_side)
        assert cancel_id is not None
        cancel_count = ledger.resting_count(adj_side)
        cancel_price = ledger.resting_price(adj_side)
        new_count = cancel_count  # same quantity at new price

        # Safety gate check (simulating the post-cancel state)
        test_ok, test_reason = self._check_post_cancel_safety(
            ledger,
            adj_side,
            new_count,
            new_price,
        )
        if not test_ok:
            logger.info("jump_blocked_by_safety", ticker=ticker, reason=test_reason)
            safety_reason = f"stay — safety gate: {test_reason}"
            self._log_decision(
                event_ticker=pair.event_ticker,
                ticker=ticker,
                adj_side=adj_side,
                trigger=trigger,
                outcome="hold_safety",
                reason=safety_reason,
                book_top=new_price,
                resting_price=cur_resting_price,
                resting_count=cur_resting_count,
                new_price=new_price,
                effective_this=this_effective,
                effective_other=other_effective,
                exit_only=exit_only,
            )
            return _hold(safety_reason)

        proposal = ProposedAdjustment(
            event_ticker=pair.event_ticker,
            side=adj_side.value,
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
            position_after=self._format_position_after(ledger, adj_side, new_count, new_price),
            safety_check=(
                f"filled_in_unit+new="
                f"{ledger.filled_count(adj_side) % ledger.unit_size + new_count}"
                f" <= unit({ledger.unit_size}), "
                f"arb={this_effective + other_effective:.1f}c < 100"
            ),
        )

        # Store as pending (supersedes any existing proposal on this side)
        evt_proposals = self._proposals.setdefault(pair.event_ticker, {})
        old = evt_proposals.get(adj_side)
        if old is not None:
            logger.info("proposal_superseded", event_ticker=pair.event_ticker, side=adj_side.value)
        evt_proposals[adj_side] = proposal

        self._log_decision(
            event_ticker=pair.event_ticker,
            ticker=ticker,
            adj_side=adj_side,
            trigger=trigger,
            outcome="follow_jump",
            reason=proposal.reason,
            book_top=new_price,
            resting_price=cancel_price,
            resting_count=cancel_count,
            new_price=new_price,
            effective_this=this_effective,
            effective_other=other_effective,
            exit_only=exit_only,
        )

        return proposal

    def resolve_event(self, ticker: str) -> str | None:
        """Resolve a market ticker to its event ticker, or None if unknown."""
        result = self.resolve_pair(ticker)
        return result[0].event_ticker if result is not None else None

    # ── Query methods ───────────────────────────────────────────────

    def has_pending_proposal(self, event_ticker: str, side: Side) -> bool:
        return side in self._proposals.get(event_ticker, {})

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
        adj_side = Side(proposal.side)
        ledger = self._ledgers[proposal.event_ticker]

        # Staleness check: verify the proposal's order still matches ledger state.
        # Silently dismiss if stale — this commonly happens when rebalance
        # cancelled the order between proposal creation and execution.
        current_resting = ledger.resting_order_id(adj_side)
        if current_resting != proposal.cancel_order_id:
            logger.info(
                "adjustment_stale_dismissed",
                event_ticker=proposal.event_ticker,
                side=adj_side.value,
                expected=proposal.cancel_order_id,
                actual=current_resting,
            )
            self.clear_proposal(proposal.event_ticker, adj_side)
            return

        # Find the ticker for this side
        ticker = self._side_ticker(proposal.event_ticker, adj_side)

        # Determine the order side string (yes/no) for this adj_side
        result = self.resolve_pair(ticker, order_side=None)
        if result is not None:
            pair, _ = result
            # Find the pair entry that matches adj_side for this event
            for p, s in self._ticker_map.get(ticker, []):
                if p.event_ticker == proposal.event_ticker and s is adj_side:
                    pair = p
                    break
            pair_side = pair.side_a if adj_side == Side.A else pair.side_b
        else:
            pair_side = "no"  # fallback

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
                    side=adj_side.value,
                    order_id=proposal.cancel_order_id,
                )
                self.clear_proposal(proposal.event_ticker, adj_side)
                return
            raise
        # Exact-precision fp100 counts; fall back to legacy contract counts for
        # fixtures that don't populate the _fp100 fields. The REST amend accepts
        # whole-contract counts only (Talos never submits fractional contracts),
        # so demote via integer-floor on the fp100 total.
        fresh_fill_fp100 = (
            fresh_order.fill_count_fp100
            if fresh_order.fill_count_fp100
            else fresh_order.fill_count * ONE_CONTRACT_FP100
        )
        fresh_remaining_fp100 = (
            fresh_order.remaining_count_fp100
            if fresh_order.remaining_count_fp100
            else fresh_order.remaining_count * ONE_CONTRACT_FP100
        )
        total_count_fp100 = fresh_fill_fp100 + fresh_remaining_fp100
        total_count = total_count_fp100 // ONE_CONTRACT_FP100

        # Skip if the order is already at the target price (avoids AMEND_ORDER_NO_OP).
        # Compare in bps: proposal.new_price is cents-valued, promote to bps.
        if pair_side == "no":
            fresh_price_bps = (
                fresh_order.no_price_bps
                if fresh_order.no_price_bps
                else cents_to_bps(fresh_order.no_price)
            )
        else:
            fresh_price_bps = (
                fresh_order.yes_price_bps
                if fresh_order.yes_price_bps
                else cents_to_bps(fresh_order.yes_price)
            )
        if fresh_price_bps == cents_to_bps(proposal.new_price):
            logger.info(
                "adjustment_already_at_target",
                event_ticker=proposal.event_ticker,
                side=adj_side.value,
                price=proposal.new_price,
            )
            self.clear_proposal(proposal.event_ticker, adj_side)
            return

        # Re-check P18 profitability with current ledger state.
        # Between proposal and execution, the other side may have filled
        # at a different price than expected at proposal time.
        # is_placement_safe takes legacy (whole-contract, cents) — demote the
        # exact fp100 remaining count to whole contracts for the gate call.
        pair_lookup = self.resolve_pair(ticker)
        if pair_lookup is not None:
            pair, _ = pair_lookup
            ok, reason = ledger.is_placement_safe(
                adj_side,
                fresh_remaining_fp100 // ONE_CONTRACT_FP100,
                proposal.new_price,
                rate=pair.fee_rate,
                catchup=True,
            )
            if not ok:
                logger.warning(
                    "adjustment_blocked_p18_recheck",
                    event_ticker=proposal.event_ticker,
                    side=adj_side.value,
                    new_price=proposal.new_price,
                    reason=reason,
                )
                self.clear_proposal(proposal.event_ticker, adj_side)
                return

        logger.info(
            "adjustment_amend",
            event_ticker=proposal.event_ticker,
            side=adj_side.value,
            order_id=proposal.cancel_order_id,
            old_price=proposal.cancel_price,
            new_price=proposal.new_price,
            total_count=total_count,
            order_fills=fresh_fill_fp100 // ONE_CONTRACT_FP100,
            order_remaining=fresh_remaining_fp100 // ONE_CONTRACT_FP100,
        )

        # Build side-aware amend kwargs
        amend_kwargs: dict[str, object] = {
            "ticker": ticker,
            "side": pair_side,
            "action": "buy",
            "count": total_count,
        }
        if pair_side == "yes":
            amend_kwargs["yes_price"] = proposal.new_price
        else:
            amend_kwargs["no_price"] = proposal.new_price

        # Single atomic amend call
        old_order, amended_order = await rest_client.amend_order(  # type: ignore[attr-defined]
            proposal.cancel_order_id,
            **amend_kwargs,
        )

        # Update fills from amend response (handles fills that arrived during approval).
        # Compare against fresh_order (same order, pre-amend) — NOT the ledger
        # aggregate, which includes fills from other orders on this side.
        # Exact-precision fp100/bps reads with legacy fallback for fixtures.
        old_fill_fp100 = (
            old_order.fill_count_fp100
            if old_order.fill_count_fp100
            else old_order.fill_count * ONE_CONTRACT_FP100
        )
        fill_delta_fp100 = old_fill_fp100 - fresh_fill_fp100
        if fill_delta_fp100 > 0:
            if pair_side == "no":
                old_price_bps = (
                    old_order.no_price_bps
                    if old_order.no_price_bps
                    else cents_to_bps(old_order.no_price)
                )
            else:
                old_price_bps = (
                    old_order.yes_price_bps
                    if old_order.yes_price_bps
                    else cents_to_bps(old_order.yes_price)
                )
            old_maker_fees_bps = (
                old_order.maker_fees_bps
                if old_order.maker_fees_bps
                else cents_to_bps(old_order.maker_fees)
            )
            fresh_maker_fees_bps = (
                fresh_order.maker_fees_bps
                if fresh_order.maker_fees_bps
                else cents_to_bps(fresh_order.maker_fees)
            )
            fee_delta_bps = old_maker_fees_bps - fresh_maker_fees_bps
            ledger.record_fill_bps(
                adj_side,
                count_fp100=fill_delta_fp100,
                price_bps=old_price_bps,
                fees_bps=max(0, fee_delta_bps),
            )

        # Update ledger from amend response — exact-precision path.
        if pair_side == "no":
            amended_price_bps = (
                amended_order.no_price_bps
                if amended_order.no_price_bps
                else cents_to_bps(amended_order.no_price)
            )
        else:
            amended_price_bps = (
                amended_order.yes_price_bps
                if amended_order.yes_price_bps
                else cents_to_bps(amended_order.yes_price)
            )
        amended_remaining_fp100 = (
            amended_order.remaining_count_fp100
            if amended_order.remaining_count_fp100
            else amended_order.remaining_count * ONE_CONTRACT_FP100
        )
        ledger.record_resting_bps(
            adj_side,
            order_id=amended_order.order_id,
            count_fp100=amended_remaining_fp100,
            price_bps=amended_price_bps,
        )

        # Clear the proposal
        self.clear_proposal(proposal.event_ticker, adj_side)

        logger.info(
            "adjustment_complete",
            event_ticker=proposal.event_ticker,
            side=adj_side.value,
            order_id=amended_order.order_id,
            new_price=proposal.new_price,
        )

    def _side_ticker(self, event_ticker: str, side: Side) -> str:
        """Look up the market ticker for a given event + side."""
        for ticker, entries in self._ticker_map.items():
            for pair, s in entries:
                if pair.event_ticker == event_ticker and s is side:
                    return ticker
        raise ValueError(f"No ticker found for {event_ticker} side {side.value}")

    # ── Internal helpers ────────────────────────────────────────────

    def _fee_rate_for(self, event_ticker: str) -> float:
        """Look up the fee rate for a pair by event ticker."""
        for entries in self._ticker_map.values():
            for pair, _ in entries:
                if pair.event_ticker == event_ticker:
                    return pair.fee_rate
        return MAKER_FEE_RATE

    def _check_post_cancel_safety(
        self,
        ledger: PositionLedger,
        side: Side,
        new_count: int,
        new_price: int,
    ) -> tuple[bool, str]:
        """Check safety as if the existing resting order were already cancelled.

        ``new_price`` is integer cents (Kalshi submission boundary). Internal
        profitability math runs in bps and rounds back to whole cents for the
        per-contract fee formula.
        """
        # Simulate post-cancel state (use fills in current unit, not total)
        filled_in_unit = ledger.filled_count(side) % ledger.unit_size
        if filled_in_unit + new_count > ledger.unit_size:
            return (
                False,
                f"would exceed unit after cancel: filled_in_unit={filled_in_unit} + "
                f"new={new_count} > {ledger.unit_size}",
            )
        # Check profitability (open-unit scoped — same as is_placement_safe P18)
        other_side = side.other
        if ledger.open_count(other_side) > 0:
            other_avg_bps = ledger.avg_filled_price_bps(other_side)
            other_price_bps = int(round(other_avg_bps / ONE_CENT_BPS)) * ONE_CENT_BPS
        elif ledger.resting_count(other_side) > 0:
            other_price_bps = ledger.resting_price_bps(other_side)
        else:
            return True, ""

        rate = self._fee_rate_for(ledger.event_ticker)
        effective_this_bps = fee_adjusted_cost_bps(cents_to_bps(new_price), rate=rate)
        effective_other_bps = fee_adjusted_cost_bps(other_price_bps, rate=rate)
        if effective_this_bps + effective_other_bps >= ONE_DOLLAR_BPS:
            effective_this = effective_this_bps / ONE_CENT_BPS
            effective_other = effective_other_bps / ONE_CENT_BPS
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
