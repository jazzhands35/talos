"""Tests for ProposalPanel widget."""

from datetime import UTC, datetime
from typing import Literal

import pytest
from textual.app import App, ComposeResult

from talos.models.adjustment import ProposedAdjustment
from talos.models.proposal import Proposal, ProposalKey
from talos.proposal_queue import ProposalQueue
from talos.ui.proposal_panel import ProposalPanel


def _make_proposal(
    event_ticker: str = "EVT-1",
    side: Literal["A", "B"] = "A",
    new_price: int = 48,
) -> Proposal:
    adj = ProposedAdjustment(
        event_ticker=event_ticker,
        side=side,
        action="follow_jump",
        cancel_order_id="ord-1",
        cancel_count=10,
        cancel_price=47,
        new_count=10,
        new_price=new_price,
        reason=f"jumped 47->{new_price}c",
        position_before="before",
        position_after="after",
        safety_check="ok",
    )
    key = ProposalKey(event_ticker=event_ticker, side=side, kind="adjustment")
    return Proposal(
        key=key,
        kind="adjustment",
        summary=f"ADJ {event_ticker} {side} 47→{new_price}c",
        detail=f"jumped 47->{new_price}c",
        created_at=datetime.now(UTC),
        adjustment=adj,
    )


class PanelTestApp(App):
    def __init__(self, queue: ProposalQueue):
        super().__init__()
        self._queue = queue

    def compose(self) -> ComposeResult:
        yield ProposalPanel(self._queue, id="proposal-panel")


@pytest.mark.asyncio
async def test_panel_clears_rows_when_empty():
    """Panel stays visible (toggled by user) but clears dynamic rows."""
    queue = ProposalQueue()
    queue.add(_make_proposal())
    async with PanelTestApp(queue).run_test() as pilot:
        panel = pilot.app.query_one(ProposalPanel)
        panel.refresh_proposals()
        await pilot.pause()  # Let Textual process deferred mounts
        assert len(panel.query(".proposal-row")) == 1
        # Remove the proposal and refresh — rows should be gone
        queue.reject(_make_proposal().key)
        panel.refresh_proposals()
        await pilot.pause()  # Let Textual process deferred removals
        await pilot.pause()  # Second pause for full DOM cleanup
        assert len(panel.query(".proposal-row")) == 0


@pytest.mark.asyncio
async def test_panel_visible_with_proposals():
    queue = ProposalQueue()
    queue.add(_make_proposal())
    async with PanelTestApp(queue).run_test() as pilot:
        panel = pilot.app.query_one(ProposalPanel)
        panel.refresh_proposals()
        assert panel.display is True


@pytest.mark.asyncio
async def test_panel_shows_proposal_summary():
    queue = ProposalQueue()
    queue.add(_make_proposal(event_ticker="EVT-1", side="A", new_price=48))
    async with PanelTestApp(queue).run_test() as pilot:
        panel = pilot.app.query_one(ProposalPanel)
        panel.refresh_proposals()
        # Check that a proposal-row child exists with the summary text
        rows = panel.query(".proposal-row")
        assert len(rows) == 1
        assert "EVT-1" in rows[0].content  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_approve_posts_message():
    queue = ProposalQueue()
    p = _make_proposal()
    queue.add(p)
    messages = []

    class CapturingApp(PanelTestApp):
        def on_proposal_panel_approved(self, event: ProposalPanel.Approved):
            messages.append(event.key)

    async with CapturingApp(queue).run_test() as pilot:
        panel = pilot.app.query_one(ProposalPanel)
        panel.refresh_proposals()
        panel.approve_selected()
        await pilot.pause()
        assert len(messages) == 1
        assert messages[0] == p.key


@pytest.mark.asyncio
async def test_reject_posts_message():
    queue = ProposalQueue()
    p = _make_proposal()
    queue.add(p)
    messages = []

    class CapturingApp(PanelTestApp):
        def on_proposal_panel_rejected(self, event: ProposalPanel.Rejected):
            messages.append(event.key)

    async with CapturingApp(queue).run_test() as pilot:
        panel = pilot.app.query_one(ProposalPanel)
        panel.refresh_proposals()
        panel.reject_selected()
        await pilot.pause()
        assert len(messages) == 1
        assert messages[0] == p.key
