"""Pure state-machine queue for proposals awaiting operator approval."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import structlog

from talos.models.proposal import Proposal, ProposalKey

log = structlog.get_logger()


class ProposalQueue:
    """Holds pending proposals keyed by ProposalKey.

    Same-key ``add()`` supersedes the previous proposal.  ``tick()`` marks
    adjustment proposals stale when their ``cancel_order_id`` disappears from
    the active-order set, and removes them after a grace period.
    """

    def __init__(self, staleness_grace_seconds: float = 5.0) -> None:
        self._proposals: dict[ProposalKey, Proposal] = {}
        self._staleness_grace = timedelta(seconds=staleness_grace_seconds)

        # Lifecycle callback for audit logging — (action, proposal) -> None
        self.on_lifecycle: Callable[[str, Proposal], None] | None = None

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add(self, proposal: Proposal) -> None:
        """Add or supersede a proposal (same key replaces)."""
        prev = self._proposals.get(proposal.key)
        self._proposals[proposal.key] = proposal
        if prev is not None:
            log.info(
                "proposal_superseded",
                key=str(proposal.key),
                summary=proposal.summary,
            )
            self._emit("SUPERSEDED", prev)
            self._emit("PROPOSED", proposal)
        else:
            log.info(
                "proposal_added",
                key=str(proposal.key),
                summary=proposal.summary,
            )
            self._emit("PROPOSED", proposal)

    def approve(self, key: ProposalKey) -> Proposal:
        """Pop and return the proposal.  Raises ``KeyError`` if missing."""
        proposal = self._proposals.pop(key)  # KeyError if absent
        log.info("proposal_approved", key=str(key))
        self._emit("APPROVED", proposal)
        return proposal

    def reject(self, key: ProposalKey) -> None:
        """Remove the proposal.  No error if already missing."""
        removed = self._proposals.pop(key, None)
        if removed is not None:
            log.info("proposal_rejected", key=str(key))
            self._emit("REJECTED", removed)

    # ------------------------------------------------------------------
    # Staleness sweep
    # ------------------------------------------------------------------

    def tick(self, active_order_ids: set[str], now: datetime | None = None) -> None:
        """Mark / clear / purge stale adjustment proposals.

        Bid proposals are never checked against order IDs.
        """
        now = now or datetime.now(UTC)
        to_remove: list[ProposalKey] = []

        for key, proposal in self._proposals.items():
            if proposal.kind != "adjustment" or proposal.adjustment is None:
                continue

            order_id = proposal.adjustment.cancel_order_id
            order_alive = order_id in active_order_ids

            if order_alive:
                # Order reappeared — clear stale flag
                if proposal.stale:
                    proposal.stale = False
                    proposal.stale_since = None
                    log.info("proposal_stale_cleared", key=str(key))
                continue

            # Order is gone
            if not proposal.stale:
                proposal.stale = True
                proposal.stale_since = now
                log.info("proposal_marked_stale", key=str(key))
            elif (
                proposal.stale_since is not None
                and (now - proposal.stale_since) >= self._staleness_grace
            ):
                to_remove.append(key)

        for key in to_remove:
            proposal = self._proposals.pop(key)
            log.info("proposal_stale_removed", key=str(key))
            self._emit("EXPIRED", proposal)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def pending(self) -> list[Proposal]:
        """Return proposals ordered by ``created_at`` (oldest first)."""
        return sorted(self._proposals.values(), key=lambda p: p.created_at)

    def has_pending(self, key: ProposalKey) -> bool:
        """Check whether a proposal with this key exists."""
        return key in self._proposals

    def __len__(self) -> int:
        return len(self._proposals)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _emit(self, action: str, proposal: Proposal) -> None:
        """Fire the lifecycle callback if set."""
        if self.on_lifecycle is not None:
            self.on_lifecycle(action, proposal)
