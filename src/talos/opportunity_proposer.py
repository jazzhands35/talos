"""OpportunityProposer — pure state machine for initial bid proposals.

Watches scanner output and proposes bids when all gates pass:
edge threshold, position gate, duplicate check, rejection cooldown,
and stability filter. No I/O, no async — just decision logic.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from talos.automation_config import AutomationConfig
from talos.data_collector import DataCollector
from talos.drip import DripConfig
from talos.models.proposal import Proposal, ProposalKey, ProposedBid
from talos.models.strategy import ArbPair, Opportunity
from talos.position_ledger import PositionLedger, Side
from talos.strategy import per_side_max_ahead
from talos.units import ONE_CENT_BPS

logger = structlog.get_logger()


class OpportunityProposer:
    """Evaluates scanner opportunities and proposes initial bids."""

    def __init__(
        self,
        config: AutomationConfig,
        data_collector: DataCollector | None = None,
    ) -> None:
        self._config = config
        # Operator-facing edge threshold stays cents-facing on AutomationConfig;
        # convert once at module entry so the internal gate does bps↔bps math
        # (handles fractional-cent edges exactly, matches scanner's exact bps edge).
        self._edge_threshold_bps = int(round(config.edge_threshold_cents * ONE_CENT_BPS))
        self._stable_since: dict[str, datetime] = {}  # event_ticker -> first seen
        self._rejected_at: dict[str, datetime] = {}  # event_ticker -> rejection time
        self._failed_at: dict[str, datetime] = {}  # event_ticker -> placement failure time
        self._data_collector = data_collector
        # Dedup log rows: only write when outcome transitions for an event.
        self._last_outcome: dict[str, str] = {}

    def _emit(
        self,
        event_ticker: str,
        outcome: str,
        reason: str,
        *,
        fee_edge: float | None = None,
    ) -> None:
        """Write a decision row only when the outcome differs from the last one."""
        if self._data_collector is None:
            return
        if self._last_outcome.get(event_ticker) == outcome:
            return
        self._last_outcome[event_ticker] = outcome
        self._data_collector.log_decision(
            event_ticker=event_ticker,
            trigger="opportunity_eval",
            outcome=outcome,
            reason=reason,
            fee_edge=fee_edge,
        )

    def stability_elapsed(self, event_ticker: str, now: datetime | None = None) -> float | None:
        """Seconds since edge was first seen, or None if not tracking."""
        first_seen = self._stable_since.get(event_ticker)
        if first_seen is None:
            return None
        if now is None:
            now = datetime.now(UTC)
        return (now - first_seen).total_seconds()

    def cooldown_elapsed(self, event_ticker: str, now: datetime | None = None) -> float | None:
        """Seconds since last rejection, or None if not in cooldown."""
        rejected_at = self._rejected_at.get(event_ticker)
        if rejected_at is None:
            return None
        if now is None:
            now = datetime.now(UTC)
        return (now - rejected_at).total_seconds()

    # Minimum 24h volume (contracts) on BOTH sides to enter a new pair.
    # Markets below this threshold are too illiquid — high risk of
    # one-sided exposure where the second leg never fills.
    MIN_VOLUME_24H: int = 50

    def evaluate(
        self,
        pair: ArbPair,
        opportunity: Opportunity,
        ledger: PositionLedger,
        pending_keys: set[ProposalKey],
        now: datetime | None = None,
        display_name: str = "",
        exit_only: bool = False,
        pair_volume_24h: int | None = None,
        drip_config: DripConfig | None = None,
    ) -> Proposal | None:
        """Return a bid proposal if all gates pass, None otherwise."""
        if now is None:
            now = datetime.now(UTC)

        event = pair.event_ticker

        # Gate 0: exit-only — no new bids
        if exit_only:
            self._emit(event, "block_exit_only", "exit-only mode, no new bids")
            return None
        # Gate 0b: volume — skip illiquid markets where pair completion
        # is unlikely. Uses min(side_a_vol, side_b_vol) since both sides
        # need liquidity for a pair to complete.
        if pair_volume_24h is not None and pair_volume_24h < self.MIN_VOLUME_24H:
            self._emit(
                event,
                "block_low_volume",
                f"24h volume {pair_volume_24h} < min {self.MIN_VOLUME_24H}",
            )
            return None

        # Gate 1: edge threshold — bps-vs-bps comparison (exact; handles sub-cent
        # edges without float-rounding drift). Falls back to legacy float-cent
        # edge when the opportunity was produced before the scanner populated
        # fee_edge_bps (pre-migration fixtures).
        opp_fee_edge_bps = (
            opportunity.fee_edge_bps
            if opportunity.fee_edge_bps
            else int(round(opportunity.fee_edge * ONE_CENT_BPS))
        )
        if opp_fee_edge_bps < self._edge_threshold_bps:
            # Edge dropped — reset stability timer
            self._stable_since.pop(event, None)
            self._emit(
                event,
                "block_low_edge",
                f"edge {opportunity.fee_edge:.1f}c < threshold "
                f"{self._config.edge_threshold_cents:.1f}c",
                fee_edge=opportunity.fee_edge,
            )
            return None

        # Gate 2a: fill-delta gate — don't place equal bids when fills are imbalanced.
        # Placing on both sides creates a committed delta that rebalance immediately
        # cancels, wasting API calls. Let catch-up close the fill gap first.
        if ledger.filled_count(Side.A) != ledger.filled_count(Side.B):
            self._emit(
                event,
                "block_fill_imbalance",
                f"fills A={ledger.filled_count(Side.A)} != B={ledger.filled_count(Side.B)}",
                fee_edge=opportunity.fee_edge,
            )
            return None

        # Gate 2b: pending-change gate — don't bid while a cancel or placement
        # hasn't been confirmed by Kalshi sync. Prevents orphaned orders where
        # the old order survives on Kalshi alongside the new one.
        if ledger.has_pending_change():
            self._emit(
                event,
                "block_pending_change",
                "ledger has pending cancel/placement awaiting sync",
                fee_edge=opportunity.fee_edge,
            )
            return None

        # Gate 2: position gate — skip if both sides are covered for the current unit
        if ledger.both_sides_complete() and ledger.filled_count(Side.A) == ledger.filled_count(
            Side.B
        ):
            # Both sides equally filled at unit boundary — eligible for next pair
            # Still block if resting orders already placed for next pair
            if ledger.resting_count(Side.A) > 0 or ledger.resting_count(Side.B) > 0:
                self._emit(
                    event,
                    "block_next_unit_resting",
                    "unit complete but next-unit resting orders present",
                    fee_edge=opportunity.fee_edge,
                )
                return None
        else:
            has_a = ledger.unit_remaining(Side.A) == 0 or ledger.resting_count(
                Side.A
            ) >= ledger.unit_remaining(Side.A)
            has_b = ledger.unit_remaining(Side.B) == 0 or ledger.resting_count(
                Side.B
            ) >= ledger.unit_remaining(Side.B)
            if has_a and has_b:
                self._emit(
                    event,
                    "block_unit_covered",
                    "both sides already covered for current unit",
                    fee_edge=opportunity.fee_edge,
                )
                return None

        # Gate 3: no pending bid proposal for this event
        bid_key = ProposalKey(event_ticker=event, side="", kind="bid")
        if bid_key in pending_keys:
            self._emit(
                event,
                "block_pending_proposal",
                "bid proposal already pending approval",
                fee_edge=opportunity.fee_edge,
            )
            return None

        # Gate 4: rejection cooldown
        rejected_at = self._rejected_at.get(event)
        if rejected_at is not None:
            elapsed = (now - rejected_at).total_seconds()
            if elapsed < self._config.rejection_cooldown_seconds:
                self._emit(
                    event,
                    "block_rejection_cooldown",
                    f"in cooldown {elapsed:.0f}s / {self._config.rejection_cooldown_seconds:.0f}s",
                    fee_edge=opportunity.fee_edge,
                )
                return None

        # Gate 4b: placement failure cooldown — prevents re-proposing when the
        # last attempt failed (e.g. post-only cross). The orderbook condition
        # that caused the failure likely persists, so use a longer cooldown.
        failed_at = self._failed_at.get(event)
        if failed_at is not None:
            elapsed = (now - failed_at).total_seconds()
            if elapsed < self._config.placement_failure_cooldown_seconds:
                self._emit(
                    event,
                    "block_placement_cooldown",
                    f"placement-failure cooldown {elapsed:.0f}s / "
                    f"{self._config.placement_failure_cooldown_seconds:.0f}s",
                    fee_edge=opportunity.fee_edge,
                )
                return None
            # Cooldown expired — clear the failure record
            del self._failed_at[event]

        # Gate 5: stability filter
        if self._config.stability_seconds > 0:
            first_seen = self._stable_since.get(event)
            if first_seen is None:
                self._stable_since[event] = now
                self._emit(
                    event,
                    "wait_stability_start",
                    f"edge first seen; need {self._config.stability_seconds:.0f}s stable",
                    fee_edge=opportunity.fee_edge,
                )
                return None
            stable_for = (now - first_seen).total_seconds()
            if stable_for < self._config.stability_seconds:
                self._emit(
                    event,
                    "wait_stability",
                    f"stable {stable_for:.0f}s / {self._config.stability_seconds:.0f}s",
                    fee_edge=opportunity.fee_edge,
                )
                return None
        else:
            # stability_seconds == 0 → skip stability gate
            stable_for = 0.0

        # All gates passed — build proposal
        # Qty = remaining capacity bounded by the strategy cap.
        # After a unit_size change, existing fills/resting are a partial unit.
        # Exception: re-entry after both sides complete → start a fresh full unit.
        if ledger.both_sides_complete() and ledger.filled_count(Side.A) == ledger.filled_count(
            Side.B
        ):
            base_qty = ledger.unit_size
        else:
            need_a = ledger.unit_remaining(Side.A) - ledger.resting_count(Side.A)
            need_b = ledger.unit_remaining(Side.B) - ledger.resting_count(Side.B)
            base_qty = min(need_a, need_b)
            if base_qty <= 0:
                self._emit(
                    event,
                    "block_no_qty",
                    f"no remaining capacity: need_a={need_a} need_b={need_b}",
                    fee_edge=opportunity.fee_edge,
                )
                return None

        cap_a = per_side_max_ahead(ledger, Side.A, drip_config) - ledger.resting_count(Side.A)
        cap_b = per_side_max_ahead(ledger, Side.B, drip_config) - ledger.resting_count(Side.B)
        qty = min(base_qty, max(0, cap_a), max(0, cap_b))
        if qty <= 0:
            self._emit(
                event,
                "block_strategy_cap",
                f"strategy cap leaves no room: cap_a={cap_a} cap_b={cap_b}",
                fee_edge=opportunity.fee_edge,
            )
            return None

        # Gate 6: profitability — pair placements use the two NEW prices
        # (not new vs historical avg, which blocks re-entry after market moves).
        # P16 unit gating still checked per-side. Internal math in bps space
        # (exact); falls back to cents×100 when the opportunity lacks the
        # exact-precision siblings (pre-migration fixture).
        from talos.fees import fee_adjusted_edge_bps

        no_a_bps = opportunity.no_a_bps if opportunity.no_a_bps else opportunity.no_a * ONE_CENT_BPS
        no_b_bps = opportunity.no_b_bps if opportunity.no_b_bps else opportunity.no_b * ONE_CENT_BPS
        if fee_adjusted_edge_bps(no_a_bps, no_b_bps, rate=pair.fee_rate) < 0:
            self._emit(
                event,
                "block_unprofitable",
                f"fee-adjusted edge negative at {opportunity.no_a}+{opportunity.no_b}",
                fee_edge=opportunity.fee_edge,
            )
            return None
        for side in (Side.A, Side.B):
            price = opportunity.no_a if side == Side.A else opportunity.no_b
            ok, _ = ledger.is_placement_safe(side, qty, price, rate=pair.fee_rate)
            if not ok and "unit" in _.lower():
                # Only block on P16 (unit capacity), not P18 (profitability vs historical)
                self._emit(
                    event,
                    "block_unit_capacity",
                    f"unit capacity violated on side {side.value}: {_}",
                    fee_edge=opportunity.fee_edge,
                )
                return None

        # Gate 7: committed-delta — don't place equal bids when committed counts
        # are already unequal. Placement adds equal qty to both sides, so any
        # pre-existing delta persists and rebalance immediately cancels one side.
        if ledger.total_committed(Side.A) != ledger.total_committed(Side.B):
            self._emit(
                event,
                "block_committed_delta",
                f"committed A={ledger.total_committed(Side.A)} "
                f"!= B={ledger.total_committed(Side.B)}",
                fee_edge=opportunity.fee_edge,
            )
            return None

        bid = ProposedBid(
            event_ticker=event,
            ticker_a=pair.ticker_a,
            ticker_b=pair.ticker_b,
            no_a=opportunity.no_a,
            no_b=opportunity.no_b,
            qty=qty,
            edge_cents=opportunity.fee_edge,
            stable_for_seconds=stable_for,
            reason=f"Edge {opportunity.fee_edge:.1f}c stable for {stable_for:.0f}s",
        )

        proposal = Proposal(
            key=bid_key,
            kind="bid",
            summary=(
                f"Bid {display_name or event} @ {opportunity.no_a}/{opportunity.no_b} NO"
                f" ({opportunity.fee_edge:.1f}c edge)"
            ),
            detail=(
                f"NO-A {opportunity.no_a}c + NO-B {opportunity.no_b}c = "
                f"{opportunity.no_a + opportunity.no_b}c cost, "
                f"{opportunity.fee_edge:.1f}c fee-adjusted edge, "
                f"stable {stable_for:.0f}s, qty {qty}"
            ),
            created_at=now,
            bid=bid,
        )

        logger.info(
            "opportunity_proposed",
            event_ticker=event,
            edge_cents=opportunity.fee_edge,
            stable_for=stable_for,
            no_a=opportunity.no_a,
            no_b=opportunity.no_b,
        )

        self._emit(
            event,
            "propose_bid",
            f"{opportunity.no_a}+{opportunity.no_b} qty={qty} "
            f"edge={opportunity.fee_edge:.1f}c stable={stable_for:.0f}s",
            fee_edge=opportunity.fee_edge,
        )

        return proposal

    def record_rejection(self, event_ticker: str, now: datetime | None = None) -> None:
        """Record rejection time, clear stability timer."""
        if now is None:
            now = datetime.now(UTC)
        self._rejected_at[event_ticker] = now
        self._stable_since.pop(event_ticker, None)
        # Force next eval to log transition back into cooldown/etc.
        self._last_outcome.pop(event_ticker, None)
        if self._data_collector is not None:
            self._data_collector.log_decision(
                event_ticker=event_ticker,
                trigger="opportunity_eval",
                outcome="proposal_rejected",
                reason="user rejected bid proposal",
            )

    def record_placement_failure(self, event_ticker: str, now: datetime | None = None) -> None:
        """Record placement failure time, clear stability timer.

        Called when place_bids fails (e.g. post-only cross). Prevents
        re-proposing until placement_failure_cooldown_seconds expires.
        """
        if now is None:
            now = datetime.now(UTC)
        self._failed_at[event_ticker] = now
        self._stable_since.pop(event_ticker, None)
        self._last_outcome.pop(event_ticker, None)
        if self._data_collector is not None:
            self._data_collector.log_decision(
                event_ticker=event_ticker,
                trigger="opportunity_eval",
                outcome="placement_failed",
                reason="bid placement failed (e.g. post-only cross)",
            )

    def record_approval(self, event_ticker: str) -> None:
        """Reset stability timer after approval.

        Prevents re-proposing during the ledger sync gap — the proposer must
        re-observe stable edge for stability_seconds before proposing again.
        """
        self._stable_since.pop(event_ticker, None)
        self._last_outcome.pop(event_ticker, None)
        if self._data_collector is not None:
            self._data_collector.log_decision(
                event_ticker=event_ticker,
                trigger="opportunity_eval",
                outcome="proposal_approved",
                reason="user approved bid proposal",
            )
