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

    def evaluate(
        self,
        pair: ArbPair,
        opportunity: Opportunity,
        ledger: PositionLedger,
        pending_keys: set[ProposalKey],
        now: datetime | None = None,
    ) -> Proposal | None:
        """Return a bid proposal if all gates pass, None otherwise."""
        if now is None:
            now = datetime.now(UTC)

        event = pair.event_ticker

        # Gate 1: edge threshold
        if opportunity.fee_edge < self._config.edge_threshold_cents:
            # Edge dropped — reset stability timer
            self._stable_since.pop(event, None)
            return None

        # Gate 2: position gate — skip if both sides are covered
        has_a = (
            ledger.resting_count(Side.A) > 0
            or ledger.filled_count(Side.A) >= ledger.unit_size
        )
        has_b = (
            ledger.resting_count(Side.B) > 0
            or ledger.filled_count(Side.B) >= ledger.unit_size
        )
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
        bid = ProposedBid(
            event_ticker=event,
            ticker_a=pair.ticker_a,
            ticker_b=pair.ticker_b,
            no_a=opportunity.no_a,
            no_b=opportunity.no_b,
            qty=ledger.unit_size,
            edge_cents=opportunity.fee_edge,
            stable_for_seconds=stable_for,
            reason=f"Edge {opportunity.fee_edge:.1f}c stable for {stable_for:.0f}s",
        )

        proposal = Proposal(
            key=bid_key,
            kind="bid",
            summary=(
                f"Bid {event} @ {opportunity.no_a}/{opportunity.no_b} NO"
                f" ({opportunity.fee_edge:.1f}c edge)"
            ),
            detail=(
                f"NO-A {opportunity.no_a}c + NO-B {opportunity.no_b}c = "
                f"{opportunity.no_a + opportunity.no_b}c cost, "
                f"{opportunity.fee_edge:.1f}c fee-adjusted edge, "
                f"stable {stable_for:.0f}s, qty {ledger.unit_size}"
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
