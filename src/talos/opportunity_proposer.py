"""OpportunityProposer — pure state machine for initial bid proposals.

Watches scanner output and proposes bids when all gates pass:
edge threshold, position gate, duplicate check, rejection cooldown,
and stability filter. No I/O, no async — just decision logic.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from talos.automation_config import AutomationConfig
from talos.models.proposal import Proposal, ProposalKey, ProposedBid
from talos.models.strategy import ArbPair, Opportunity
from talos.position_ledger import PositionLedger, Side

logger = structlog.get_logger()


class OpportunityProposer:
    """Evaluates scanner opportunities and proposes initial bids."""

    def __init__(self, config: AutomationConfig) -> None:
        self._config = config
        self._stable_since: dict[str, datetime] = {}  # event_ticker -> first seen
        self._rejected_at: dict[str, datetime] = {}  # event_ticker -> rejection time

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

    def evaluate(
        self,
        pair: ArbPair,
        opportunity: Opportunity,
        ledger: PositionLedger,
        pending_keys: set[ProposalKey],
        now: datetime | None = None,
        display_name: str = "",
        exit_only: bool = False,
    ) -> Proposal | None:
        """Return a bid proposal if all gates pass, None otherwise."""
        if now is None:
            now = datetime.now(UTC)

        event = pair.event_ticker

        # Gate 0: exit-only — no new bids
        if exit_only:
            return None

        # Gate 1: edge threshold
        if opportunity.fee_edge < self._config.edge_threshold_cents:
            # Edge dropped — reset stability timer
            self._stable_since.pop(event, None)
            return None

        # Gate 2a: fill-delta gate — don't place equal bids when fills are imbalanced.
        # Placing on both sides creates a committed delta that rebalance immediately
        # cancels, wasting API calls. Let catch-up close the fill gap first.
        if ledger.filled_count(Side.A) != ledger.filled_count(Side.B):
            return None

        # Gate 2b: pending-change gate — don't bid while a cancel or placement
        # hasn't been confirmed by Kalshi sync. Prevents orphaned orders where
        # the old order survives on Kalshi alongside the new one.
        if ledger.has_pending_change():
            return None

        # Gate 2: position gate — skip if both sides are covered for the current unit
        if ledger.both_sides_complete() and ledger.filled_count(Side.A) == ledger.filled_count(
            Side.B
        ):
            # Both sides equally filled at unit boundary — eligible for next pair
            # Still block if resting orders already placed for next pair
            if ledger.resting_count(Side.A) > 0 or ledger.resting_count(Side.B) > 0:
                return None
        else:
            has_a = ledger.unit_remaining(Side.A) == 0 or ledger.resting_count(
                Side.A
            ) >= ledger.unit_remaining(Side.A)
            has_b = ledger.unit_remaining(Side.B) == 0 or ledger.resting_count(
                Side.B
            ) >= ledger.unit_remaining(Side.B)
            if has_a and has_b:
                return None

        # Gate 3: no pending bid proposal for this event
        bid_key = ProposalKey(event_ticker=event, side="", kind="bid")
        if bid_key in pending_keys:
            return None

        # Gate 4: rejection cooldown
        rejected_at = self._rejected_at.get(event)
        if rejected_at is not None:
            elapsed = (now - rejected_at).total_seconds()
            if elapsed < self._config.rejection_cooldown_seconds:
                return None

        # Gate 5: stability filter
        if self._config.stability_seconds > 0:
            first_seen = self._stable_since.get(event)
            if first_seen is None:
                self._stable_since[event] = now
                return None
            stable_for = (now - first_seen).total_seconds()
            if stable_for < self._config.stability_seconds:
                return None
        else:
            # stability_seconds == 0 → skip stability gate
            stable_for = 0.0

        # All gates passed — build proposal
        # Qty = remaining capacity minus what's already resting.
        # After a unit_size change, existing fills/resting are a partial unit.
        # Exception: re-entry after both sides complete → start a fresh full unit.
        if ledger.both_sides_complete() and ledger.filled_count(Side.A) == ledger.filled_count(
            Side.B
        ):
            qty = ledger.unit_size
        else:
            need_a = ledger.unit_remaining(Side.A) - ledger.resting_count(Side.A)
            need_b = ledger.unit_remaining(Side.B) - ledger.resting_count(Side.B)
            qty = min(need_a, need_b)
            if qty <= 0:
                return None

        # Gate 6: per-side profitability — don't propose if either side would
        # fail is_placement_safe (prevents "Bid BLOCKED" spam from place_bids).
        for side, price in [(Side.A, opportunity.no_a), (Side.B, opportunity.no_b)]:
            ok, _ = ledger.is_placement_safe(
                side, qty, price, rate=pair.fee_rate,
            )
            if not ok:
                return None

        # Gate 7: committed-delta — don't place equal bids when committed counts
        # are already unequal. Placement adds equal qty to both sides, so any
        # pre-existing delta persists and rebalance immediately cancels one side.
        if ledger.total_committed(Side.A) != ledger.total_committed(Side.B):
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

        return proposal

    def record_rejection(self, event_ticker: str, now: datetime | None = None) -> None:
        """Record rejection time, clear stability timer."""
        if now is None:
            now = datetime.now(UTC)
        self._rejected_at[event_ticker] = now
        self._stable_since.pop(event_ticker, None)

    def record_approval(self, event_ticker: str) -> None:
        """Reset stability timer after approval.

        Prevents re-proposing during the ledger sync gap — the proposer must
        re-observe stable edge for stability_seconds before proposing again.
        """
        self._stable_since.pop(event_ticker, None)
