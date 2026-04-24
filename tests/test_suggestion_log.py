"""Tests for suggestion_log — human-readable proposal audit trail."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from talos.models.proposal import Proposal, ProposalKey, ProposedBid
from talos.suggestion_log import SuggestionLog, format_entry


def _make_bid_proposal(event: str = "EVT-1") -> Proposal:
    return Proposal(
        key=ProposalKey(event_ticker=event, side="", kind="bid"),
        kind="bid",
        summary=f"Bid {event} @ 46/48 NO (2.1c edge)",
        detail="NO-A 46c + NO-B 48c = 94c cost, 2.1c fee-adjusted edge, stable 15s, qty 10",
        created_at=datetime(2026, 3, 10, 9, 38, 12, tzinfo=UTC),
        bid=ProposedBid(
            event_ticker=event,
            ticker_a="TK-A",
            ticker_b="TK-B",
            no_a=46,
            no_b=48,
            qty=10,
            edge_cents=2.1,
            stable_for_seconds=15.0,
            reason="Edge 2.1c stable for 15s",
        ),
    )


def _make_hold_proposal(event: str = "EVT-1") -> Proposal:
    return Proposal(
        key=ProposalKey(event_ticker=event, side="B", kind="hold"),
        kind="hold",
        summary="HOLD ...EVT-1 side B",
        detail="stay at 47c — following to 48c not profitable (50.9+50.9=101.8 >= 100)",
        created_at=datetime(2026, 3, 10, 9, 39, 1, tzinfo=UTC),
    )


class TestFormatEntry:
    def test_proposed_includes_summary_and_detail(self):
        proposal = _make_bid_proposal()
        ts = datetime(2026, 3, 10, 9, 38, 12, tzinfo=UTC)
        entry = format_entry("PROPOSED", proposal, timestamp=ts)

        assert "[2026-03-10 09:38:12]" in entry
        assert "PROPOSED" in entry
        assert "bid" in entry
        assert "EVT-1" in entry
        assert "46/48 NO" in entry
        assert "2.1c" in entry

    def test_approved_includes_summary_only(self):
        proposal = _make_bid_proposal()
        ts = datetime(2026, 3, 10, 9, 38, 15, tzinfo=UTC)
        entry = format_entry("APPROVED", proposal, timestamp=ts)

        assert "APPROVED" in entry
        assert "46/48 NO" in entry
        # Detail should NOT be in approved entries
        assert "fee-adjusted edge" not in entry

    def test_rejected_is_compact(self):
        proposal = _make_bid_proposal()
        entry = format_entry("REJECTED", proposal)

        assert "REJECTED" in entry
        # No summary or detail for rejected
        lines = entry.strip().split("\n")
        assert len(lines) == 1

    def test_hold_with_side(self):
        proposal = _make_hold_proposal()
        entry = format_entry("PROPOSED", proposal)

        assert "side B" in entry
        assert "hold" in entry

    def test_expired_is_compact(self):
        proposal = _make_bid_proposal()
        entry = format_entry("EXPIRED", proposal)

        assert "EXPIRED" in entry
        lines = entry.strip().split("\n")
        assert len(lines) == 1


class TestSuggestionLog:
    def test_appends_to_file(self, tmp_path: Path):
        log_file = tmp_path / "suggestions.log"
        logger = SuggestionLog(log_file)

        proposal = _make_bid_proposal()
        ts = datetime(2026, 3, 10, 9, 38, 12, tzinfo=UTC)
        logger.log("PROPOSED", proposal, timestamp=ts)

        content = log_file.read_text(encoding="utf-8")
        assert "[2026-03-10 09:38:12]" in content
        assert "PROPOSED" in content
        assert "EVT-1" in content

    def test_multiple_entries_appended(self, tmp_path: Path):
        log_file = tmp_path / "suggestions.log"
        logger = SuggestionLog(log_file)

        p1 = _make_bid_proposal("EVT-1")
        p2 = _make_hold_proposal("EVT-2")
        logger.log("PROPOSED", p1)
        logger.log("PROPOSED", p2)

        content = log_file.read_text(encoding="utf-8")
        assert "EVT-1" in content
        assert "EVT-2" in content
        # Two entries separated by blank lines
        assert content.count("PROPOSED") == 2


class TestProposalQueueLifecycle:
    def test_add_fires_proposed(self):
        from talos.proposal_queue import ProposalQueue

        events: list[tuple[str, Proposal]] = []
        queue = ProposalQueue()
        queue.on_lifecycle = lambda action, p: events.append((action, p))

        proposal = _make_bid_proposal()
        queue.add(proposal)

        assert len(events) == 1
        assert events[0][0] == "PROPOSED"

    def test_supersede_fires_both(self):
        from talos.proposal_queue import ProposalQueue

        events: list[tuple[str, Proposal]] = []
        queue = ProposalQueue()
        queue.on_lifecycle = lambda action, p: events.append((action, p))

        p1 = _make_bid_proposal()
        p2 = _make_bid_proposal()  # same key
        queue.add(p1)
        queue.add(p2)

        actions = [a for a, _ in events]
        assert actions == ["PROPOSED", "SUPERSEDED", "PROPOSED"]

    def test_approve_fires(self):
        from talos.proposal_queue import ProposalQueue

        events: list[tuple[str, Proposal]] = []
        queue = ProposalQueue()
        queue.on_lifecycle = lambda action, p: events.append((action, p))

        proposal = _make_bid_proposal()
        queue.add(proposal)
        queue.approve(proposal.key)

        actions = [a for a, _ in events]
        assert "APPROVED" in actions

    def test_reject_fires(self):
        from talos.proposal_queue import ProposalQueue

        events: list[tuple[str, Proposal]] = []
        queue = ProposalQueue()
        queue.on_lifecycle = lambda action, p: events.append((action, p))

        proposal = _make_bid_proposal()
        queue.add(proposal)
        queue.reject(proposal.key)

        actions = [a for a, _ in events]
        assert "REJECTED" in actions
