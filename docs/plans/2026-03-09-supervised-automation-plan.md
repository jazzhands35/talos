# Supervised Automation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move Talos from "assisted" to "supervised" automation — system proposes decisions (adjustments + bids), human approves/rejects via a collapsible sidebar, with auto-expiry for stale proposals.

**Architecture:** New pure state machines (ProposalQueue, OpportunityProposer) feed a unified Proposal model into a Textual sidebar widget (ProposalPanel). BidAdjuster and engine wire through the queue instead of direct callbacks. Nothing executes without human approval.

**Tech Stack:** Python 3.12+, Pydantic v2, Textual, pytest, structlog

---

## Phase 1: ProposalQueue + Models

### Task 1: Proposal Models

**Files:**
- Create: `src/talos/models/proposal.py`
- Test: `tests/test_models_proposal.py`

**Step 1: Write the failing test**

```python
# tests/test_models_proposal.py
"""Tests for Proposal and ProposedBid models."""

from datetime import UTC, datetime

from talos.models.proposal import Proposal, ProposalKey, ProposedBid


class TestProposalKey:
    def test_key_equality(self):
        k1 = ProposalKey(event_ticker="EVT-1", side="A", kind="adjustment")
        k2 = ProposalKey(event_ticker="EVT-1", side="A", kind="adjustment")
        assert k1 == k2

    def test_key_different_kind(self):
        k1 = ProposalKey(event_ticker="EVT-1", side="A", kind="adjustment")
        k2 = ProposalKey(event_ticker="EVT-1", side="A", kind="bid")
        assert k1 != k2

    def test_key_hashable(self):
        k = ProposalKey(event_ticker="EVT-1", side="A", kind="adjustment")
        d = {k: True}
        assert d[k] is True


class TestProposedBid:
    def test_create_proposed_bid(self):
        bid = ProposedBid(
            event_ticker="EVT-1",
            ticker_a="TK-A",
            ticker_b="TK-B",
            no_a=48,
            no_b=50,
            qty=10,
            edge_cents=1.5,
            stable_for_seconds=5.2,
            reason="edge 1.5c stable 5.2s, no position",
        )
        assert bid.event_ticker == "EVT-1"
        assert bid.qty == 10


class TestProposal:
    def test_create_adjustment_proposal(self):
        from talos.models.adjustment import ProposedAdjustment

        adj = ProposedAdjustment(
            event_ticker="EVT-1",
            side="A",
            action="follow_jump",
            cancel_order_id="ord-1",
            cancel_count=10,
            cancel_price=47,
            new_count=10,
            new_price=48,
            reason="jumped",
            position_before="before",
            position_after="after",
            safety_check="ok",
        )
        key = ProposalKey(event_ticker="EVT-1", side="A", kind="adjustment")
        p = Proposal(
            key=key,
            kind="adjustment",
            summary="ADJ EVT-1 A 47→48c",
            detail="jumped",
            created_at=datetime.now(UTC),
            adjustment=adj,
        )
        assert p.kind == "adjustment"
        assert p.adjustment is not None
        assert p.bid is None
        assert p.stale is False
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_models_proposal.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'talos.models.proposal'`

**Step 3: Write minimal implementation**

```python
# src/talos/models/proposal.py
"""Unified proposal models for supervised automation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from talos.models.adjustment import ProposedAdjustment


class ProposedBid(BaseModel):
    """A proposed initial bid for operator approval."""

    event_ticker: str
    ticker_a: str
    ticker_b: str
    no_a: int
    no_b: int
    qty: int
    edge_cents: float
    stable_for_seconds: float
    reason: str


class ProposalKey(BaseModel, frozen=True):
    """Hashable key for deduplicating proposals."""

    event_ticker: str
    side: str  # "A", "B", or "" for bids (both sides)
    kind: Literal["adjustment", "bid"]


class Proposal(BaseModel):
    """Unified envelope for all proposal types."""

    key: ProposalKey
    kind: Literal["adjustment", "bid"]
    summary: str
    detail: str
    created_at: datetime
    stale: bool = False
    stale_since: datetime | None = None
    adjustment: ProposedAdjustment | None = None
    bid: ProposedBid | None = None
```

**Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_models_proposal.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/talos/models/proposal.py tests/test_models_proposal.py
git commit -m "feat: add Proposal, ProposalKey, ProposedBid models"
```

---

### Task 2: ProposalQueue Pure State Machine

**Files:**
- Create: `src/talos/proposal_queue.py`
- Test: `tests/test_proposal_queue.py`

**Step 1: Write the failing tests**

```python
# tests/test_proposal_queue.py
"""Tests for ProposalQueue — pure state machine."""

from datetime import UTC, datetime, timedelta

from talos.models.adjustment import ProposedAdjustment
from talos.models.proposal import Proposal, ProposalKey, ProposedBid
from talos.proposal_queue import ProposalQueue


def _make_adj_proposal(
    event_ticker: str = "EVT-1",
    side: str = "A",
    cancel_price: int = 47,
    new_price: int = 48,
    order_id: str = "ord-1",
    now: datetime | None = None,
) -> Proposal:
    adj = ProposedAdjustment(
        event_ticker=event_ticker,
        side=side,
        action="follow_jump",
        cancel_order_id=order_id,
        cancel_count=10,
        cancel_price=cancel_price,
        new_count=10,
        new_price=new_price,
        reason=f"jumped {cancel_price}->{new_price}",
        position_before="before",
        position_after="after",
        safety_check="ok",
    )
    key = ProposalKey(event_ticker=event_ticker, side=side, kind="adjustment")
    return Proposal(
        key=key,
        kind="adjustment",
        summary=f"ADJ {event_ticker} {side} {cancel_price}→{new_price}c",
        detail=adj.reason,
        created_at=now or datetime.now(UTC),
        adjustment=adj,
    )


def _make_bid_proposal(
    event_ticker: str = "EVT-1",
    now: datetime | None = None,
) -> Proposal:
    bid = ProposedBid(
        event_ticker=event_ticker,
        ticker_a="TK-A",
        ticker_b="TK-B",
        no_a=48,
        no_b=50,
        qty=10,
        edge_cents=1.5,
        stable_for_seconds=5.2,
        reason="edge 1.5c stable 5.2s",
    )
    key = ProposalKey(event_ticker=event_ticker, side="", kind="bid")
    return Proposal(
        key=key,
        kind="bid",
        summary=f"BID {event_ticker} edge 1.5c",
        detail=bid.reason,
        created_at=now or datetime.now(UTC),
        bid=bid,
    )


class TestAdd:
    def test_add_proposal(self):
        q = ProposalQueue()
        p = _make_adj_proposal()
        q.add(p)
        assert len(q.pending()) == 1
        assert q.pending()[0].key == p.key

    def test_add_multiple_proposals(self):
        q = ProposalQueue()
        q.add(_make_adj_proposal(side="A"))
        q.add(_make_adj_proposal(side="B"))
        assert len(q.pending()) == 2

    def test_supersede_same_key(self):
        q = ProposalQueue()
        p1 = _make_adj_proposal(new_price=48)
        p2 = _make_adj_proposal(new_price=49)
        q.add(p1)
        q.add(p2)
        assert len(q.pending()) == 1
        assert q.pending()[0].adjustment.new_price == 49


class TestApproveReject:
    def test_approve_removes_and_returns(self):
        q = ProposalQueue()
        p = _make_adj_proposal()
        q.add(p)
        approved = q.approve(p.key)
        assert approved.key == p.key
        assert len(q.pending()) == 0

    def test_approve_missing_key_raises(self):
        q = ProposalQueue()
        key = ProposalKey(event_ticker="EVT-X", side="A", kind="adjustment")
        import pytest
        with pytest.raises(KeyError):
            q.approve(key)

    def test_reject_removes(self):
        q = ProposalQueue()
        p = _make_adj_proposal()
        q.add(p)
        q.reject(p.key)
        assert len(q.pending()) == 0


class TestStaleness:
    def test_tick_marks_stale_when_order_gone(self):
        q = ProposalQueue(staleness_grace_seconds=5.0)
        p = _make_adj_proposal(order_id="ord-1")
        q.add(p)
        # Tick with no matching orders — proposal should be marked stale
        q.tick(active_order_ids=set())
        assert q.pending()[0].stale is True

    def test_tick_does_not_mark_stale_when_order_present(self):
        q = ProposalQueue(staleness_grace_seconds=5.0)
        p = _make_adj_proposal(order_id="ord-1")
        q.add(p)
        q.tick(active_order_ids={"ord-1"})
        assert q.pending()[0].stale is False

    def test_stale_removed_after_grace_period(self):
        q = ProposalQueue(staleness_grace_seconds=5.0)
        past = datetime.now(UTC) - timedelta(seconds=10)
        p = _make_adj_proposal(order_id="ord-1", now=past)
        q.add(p)
        # First tick: mark stale
        q.tick(active_order_ids=set(), now=past + timedelta(seconds=1))
        assert len(q.pending()) == 1
        # Second tick: past grace period — remove
        q.tick(active_order_ids=set(), now=past + timedelta(seconds=7))
        assert len(q.pending()) == 0

    def test_bid_proposals_not_checked_against_orders(self):
        q = ProposalQueue(staleness_grace_seconds=5.0)
        p = _make_bid_proposal()
        q.add(p)
        q.tick(active_order_ids=set())
        assert q.pending()[0].stale is False


class TestOrdering:
    def test_pending_ordered_by_creation_time(self):
        q = ProposalQueue()
        t1 = datetime.now(UTC)
        t2 = t1 + timedelta(seconds=1)
        q.add(_make_adj_proposal(side="B", now=t2))
        q.add(_make_adj_proposal(side="A", now=t1))
        pending = q.pending()
        assert pending[0].key.side == "A"  # older first
        assert pending[1].key.side == "B"
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_proposal_queue.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'talos.proposal_queue'`

**Step 3: Write minimal implementation**

```python
# src/talos/proposal_queue.py
"""ProposalQueue — pure state machine for pending proposals.

Single choke point for all automated decisions. Nothing executes
without passing through this queue and being approved by the operator.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from talos.models.proposal import Proposal, ProposalKey

logger = structlog.get_logger()


class ProposalQueue:
    """Holds pending proposals awaiting operator approval.

    Pure state machine — no I/O, no async. Handles add/supersede,
    approve/reject, and staleness expiry.
    """

    def __init__(self, staleness_grace_seconds: float = 5.0) -> None:
        self._proposals: dict[ProposalKey, Proposal] = {}
        self._staleness_grace_seconds = staleness_grace_seconds

    def add(self, proposal: Proposal) -> None:
        """Add or supersede a proposal."""
        old = self._proposals.get(proposal.key)
        if old is not None:
            logger.info(
                "proposal_superseded",
                key=str(proposal.key),
            )
        self._proposals[proposal.key] = proposal

    def approve(self, key: ProposalKey) -> Proposal:
        """Remove and return a proposal for execution.

        Raises KeyError if key not found.
        """
        return self._proposals.pop(key)

    def reject(self, key: ProposalKey) -> None:
        """Remove a proposal without executing."""
        self._proposals.pop(key, None)

    def tick(
        self,
        active_order_ids: set[str],
        now: datetime | None = None,
    ) -> None:
        """Sweep for stale proposals.

        Adjustment proposals whose cancel_order_id is no longer in
        active_order_ids get marked stale. Stale proposals are removed
        after the grace period.
        """
        now = now or datetime.now(UTC)
        to_remove: list[ProposalKey] = []

        for key, proposal in self._proposals.items():
            # Only check adjustment proposals against active orders
            if proposal.kind == "adjustment" and proposal.adjustment is not None:
                order_id = proposal.adjustment.cancel_order_id
                if order_id not in active_order_ids:
                    if not proposal.stale:
                        proposal.stale = True
                        proposal.stale_since = now
                else:
                    # Order reappeared (unlikely but safe)
                    proposal.stale = False
                    proposal.stale_since = None

            # Remove stale proposals past grace period
            if proposal.stale and proposal.stale_since is not None:
                elapsed = (now - proposal.stale_since).total_seconds()
                if elapsed >= self._staleness_grace_seconds:
                    to_remove.append(key)

        for key in to_remove:
            logger.info("proposal_expired", key=str(key))
            del self._proposals[key]

    def pending(self) -> list[Proposal]:
        """All pending proposals, ordered by creation time (oldest first)."""
        return sorted(self._proposals.values(), key=lambda p: p.created_at)

    def has_pending(self, key: ProposalKey) -> bool:
        """Check if a proposal exists for this key."""
        return key in self._proposals

    def __len__(self) -> int:
        return len(self._proposals)
```

**Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_proposal_queue.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/talos/proposal_queue.py tests/test_proposal_queue.py
git commit -m "feat: add ProposalQueue pure state machine"
```

---

### Task 3: AutomationConfig

**Files:**
- Create: `src/talos/automation_config.py`
- Test: `tests/test_automation_config.py`

**Step 1: Write the failing test**

```python
# tests/test_automation_config.py
"""Tests for AutomationConfig."""

from talos.automation_config import AutomationConfig


class TestDefaults:
    def test_default_values(self):
        cfg = AutomationConfig()
        assert cfg.edge_threshold_cents == 1.5
        assert cfg.stability_seconds == 5.0
        assert cfg.staleness_grace_seconds == 5.0
        assert cfg.rejection_cooldown_seconds == 30.0
        assert cfg.unit_size == 10
        assert cfg.enabled is False

    def test_custom_values(self):
        cfg = AutomationConfig(
            edge_threshold_cents=2.0,
            stability_seconds=10.0,
            enabled=True,
        )
        assert cfg.edge_threshold_cents == 2.0
        assert cfg.stability_seconds == 10.0
        assert cfg.enabled is True
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_automation_config.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# src/talos/automation_config.py
"""Configuration for supervised automation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AutomationConfig:
    """Settings for the proposal system. Off by default."""

    edge_threshold_cents: float = 1.5
    stability_seconds: float = 5.0
    staleness_grace_seconds: float = 5.0
    rejection_cooldown_seconds: float = 30.0
    unit_size: int = 10
    enabled: bool = False
```

**Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_automation_config.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/talos/automation_config.py tests/test_automation_config.py
git commit -m "feat: add AutomationConfig dataclass"
```

---

## Phase 2: Wire Adjustments Through ProposalQueue

### Task 4: Engine + ProposalQueue Integration

**Files:**
- Modify: `src/talos/engine.py`
- Modify: `tests/test_engine.py`

This task wires BidAdjuster proposals through ProposalQueue instead of firing a direct callback. The engine creates `Proposal` envelopes from `ProposedAdjustment` and adds them to the queue.

**Step 1: Write the failing test**

Add to `tests/test_engine.py`:

```python
# Add these tests to the existing test file.
# The engine needs a ProposalQueue injected. Test that:
# 1. on_top_of_market_change adds proposals to the queue
# 2. approve_adjustment pops from queue and executes
# 3. reject_adjustment removes from queue

def test_jump_adds_proposal_to_queue(engine_with_queue):
    """When a jump is detected, the proposal lands in the queue."""
    engine = engine_with_queue
    # Setup: resting order on side B, side A filled
    ledger = engine.adjuster.get_ledger("EVT-1")
    ledger.record_fill(Side.A, count=10, price=50)
    ledger.record_resting(Side.B, order_id="ord-b", count=10, price=47)
    # Trigger jump on side B
    engine.on_top_of_market_change("TK-B", at_top=False)
    # Proposal should be in the queue
    assert len(engine.proposal_queue) == 1
    p = engine.proposal_queue.pending()[0]
    assert p.kind == "adjustment"
    assert p.adjustment.new_price == 48
```

The exact fixture (`engine_with_queue`) depends on existing test infrastructure. If `test_engine.py` already has engine fixtures, extend them to include a `ProposalQueue`. Otherwise, create a minimal fixture following the `FakeBookManager` pattern from `test_bid_adjuster.py`.

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py::test_jump_adds_proposal_to_queue -v`
Expected: FAIL — `engine.proposal_queue` doesn't exist

**Step 3: Modify engine.py**

Key changes to `src/talos/engine.py`:

1. Add `proposal_queue: ProposalQueue` parameter to `__init__`
2. Expose as `self.proposal_queue` property
3. In `on_top_of_market_change`: when `evaluate_jump()` returns a proposal, wrap it in a `Proposal` envelope and call `self._proposal_queue.add()`
4. In `refresh_account`: call `self._proposal_queue.tick()` with active order IDs
5. Update `approve_adjustment` to pop from queue first
6. Update `reject_adjustment` to go through queue

```python
# In __init__, add:
from talos.proposal_queue import ProposalQueue
from talos.models.proposal import Proposal, ProposalKey

# New parameter:
proposal_queue: ProposalQueue | None = None,

# Store it:
self._proposal_queue = proposal_queue or ProposalQueue()

# New property:
@property
def proposal_queue(self) -> ProposalQueue:
    return self._proposal_queue

# In on_top_of_market_change, after evaluate_jump returns a proposal:
if proposal is not None:
    key = ProposalKey(
        event_ticker=proposal.event_ticker,
        side=proposal.side,
        kind="adjustment",
    )
    envelope = Proposal(
        key=key,
        kind="adjustment",
        summary=f"ADJ {proposal.event_ticker} {proposal.side} {proposal.cancel_price}→{proposal.new_price}c",
        detail=proposal.reason,
        created_at=datetime.now(UTC),
        adjustment=proposal,
    )
    self._proposal_queue.add(envelope)

# In refresh_account, after syncing ledgers:
active_ids = {o.order_id for o in orders if o.remaining_count > 0}
self._proposal_queue.tick(active_order_ids=active_ids)

# approve_adjustment becomes:
async def approve_adjustment(self, key: ProposalKey) -> None:
    try:
        envelope = self._proposal_queue.approve(key)
    except KeyError:
        self._notify("No pending proposal to approve", "warning")
        return
    proposal = envelope.adjustment
    if proposal is None:
        self._notify("Proposal has no adjustment payload", "error")
        return
    try:
        await self._adjuster.execute(proposal, self._rest)
        self._notify(f"Adjusted: {proposal.event_ticker} {proposal.side} → {proposal.new_price}c")
    except Exception as e:
        self._notify(f"Adjustment FAILED: {type(e).__name__}: {e}", "error")

# reject_adjustment becomes:
def reject_adjustment(self, key: ProposalKey) -> None:
    self._proposal_queue.reject(key)
    self._notify(f"Rejected: {key.event_ticker} {key.side}")
```

Note: Keep the old `approve_adjustment(event_ticker, side_value)` signature working during transition — the UI will be updated in Task 6 to use `ProposalKey` directly.

**Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -v`
Expected: PASS (all existing + new tests)

**Step 5: Commit**

```bash
git add src/talos/engine.py tests/test_engine.py
git commit -m "feat: wire BidAdjuster proposals through ProposalQueue"
```

---

## Phase 3: ProposalPanel UI

### Task 5: ProposalPanel Widget

**Files:**
- Create: `src/talos/ui/proposal_panel.py`
- Test: `tests/test_proposal_panel.py`

**Step 1: Write the failing test**

```python
# tests/test_proposal_panel.py
"""Tests for ProposalPanel widget."""

from datetime import UTC, datetime

from textual.app import App, ComposeResult

from talos.models.adjustment import ProposedAdjustment
from talos.models.proposal import Proposal, ProposalKey
from talos.proposal_queue import ProposalQueue
from talos.ui.proposal_panel import ProposalPanel


def _make_proposal() -> Proposal:
    adj = ProposedAdjustment(
        event_ticker="EVT-1",
        side="A",
        action="follow_jump",
        cancel_order_id="ord-1",
        cancel_count=10,
        cancel_price=47,
        new_count=10,
        new_price=48,
        reason="jumped 47c->48c",
        position_before="before",
        position_after="after",
        safety_check="ok",
    )
    key = ProposalKey(event_ticker="EVT-1", side="A", kind="adjustment")
    return Proposal(
        key=key,
        kind="adjustment",
        summary="ADJ EVT-1 A 47→48c",
        detail="jumped 47c->48c",
        created_at=datetime.now(UTC),
        adjustment=adj,
    )


class PanelTestApp(App):
    def __init__(self, queue: ProposalQueue):
        super().__init__()
        self._queue = queue

    def compose(self) -> ComposeResult:
        yield ProposalPanel(self._queue)


async def test_panel_hidden_when_empty():
    queue = ProposalQueue()
    async with PanelTestApp(queue).run_test() as pilot:
        panel = pilot.app.query_one(ProposalPanel)
        assert panel.display is False


async def test_panel_visible_when_proposal_added():
    queue = ProposalQueue()
    queue.add(_make_proposal())
    async with PanelTestApp(queue).run_test() as pilot:
        panel = pilot.app.query_one(ProposalPanel)
        panel.refresh_proposals()
        assert panel.display is True
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_proposal_panel.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# src/talos/ui/proposal_panel.py
"""ProposalPanel — collapsible sidebar for pending proposals."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

from talos.models.proposal import ProposalKey
from talos.proposal_queue import ProposalQueue


class ProposalPanel(Vertical):
    """Collapsible right sidebar showing pending proposals.

    Hidden when queue is empty. Each proposal rendered as a selectable
    row with approve/reject keybindings.
    """

    DEFAULT_CSS = """
    ProposalPanel {
        dock: right;
        width: 50;
        background: $surface;
        border-left: solid $primary-lighten-2;
        padding: 1;
        overflow-y: auto;
    }

    ProposalPanel .proposal-row {
        padding: 0 1;
        margin: 0 0 1 0;
    }

    ProposalPanel .proposal-row.selected {
        background: $primary-darken-1;
    }

    ProposalPanel .proposal-row.stale {
        opacity: 0.4;
    }

    ProposalPanel .proposal-header {
        text-style: bold;
    }
    """

    selected_index: reactive[int] = reactive(0)

    class Approved(Message):
        """Fired when operator approves a proposal."""

        def __init__(self, key: ProposalKey) -> None:
            super().__init__()
            self.key = key

    class Rejected(Message):
        """Fired when operator rejects a proposal."""

        def __init__(self, key: ProposalKey) -> None:
            super().__init__()
            self.key = key

    def __init__(self, queue: ProposalQueue, **kwargs) -> None:
        super().__init__(**kwargs)
        self._queue = queue
        self._keys: list[ProposalKey] = []

    def compose(self) -> ComposeResult:
        yield Static("PROPOSALS", classes="proposal-header")

    def refresh_proposals(self) -> None:
        """Re-render from current queue state."""
        pending = self._queue.pending()
        self.display = len(pending) > 0
        if not pending:
            return

        self._keys = [p.key for p in pending]

        # Remove old proposal rows
        for child in list(self.children):
            if "proposal-row" in child.classes:
                child.remove()

        # Add new rows
        for i, proposal in enumerate(pending):
            stale_cls = " stale" if proposal.stale else ""
            selected_cls = " selected" if i == self.selected_index else ""
            row = Static(
                f"[{i + 1}] {proposal.summary}",
                classes=f"proposal-row{stale_cls}{selected_cls}",
            )
            self.mount(row)

    def key_up(self) -> None:
        if self._keys:
            self.selected_index = max(0, self.selected_index - 1)
            self.refresh_proposals()

    def key_down(self) -> None:
        if self._keys:
            self.selected_index = min(len(self._keys) - 1, self.selected_index + 1)
            self.refresh_proposals()

    def key_y(self) -> None:
        """Approve selected proposal."""
        if self._keys and 0 <= self.selected_index < len(self._keys):
            self.post_message(self.Approved(self._keys[self.selected_index]))

    def key_n(self) -> None:
        """Reject selected proposal."""
        if self._keys and 0 <= self.selected_index < len(self._keys):
            self.post_message(self.Rejected(self._keys[self.selected_index]))
```

**Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_proposal_panel.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/talos/ui/proposal_panel.py tests/test_proposal_panel.py
git commit -m "feat: add ProposalPanel collapsible sidebar widget"
```

---

### Task 6: Mount ProposalPanel in TalosApp

**Files:**
- Modify: `src/talos/ui/app.py`
- Modify: `src/talos/ui/theme.py` (add panel CSS)
- Test: `tests/test_ui.py` (extend existing)

**Step 1: Write the failing test**

Add to `tests/test_ui.py`:

```python
async def test_proposal_panel_exists():
    """ProposalPanel is mounted but hidden by default."""
    from talos.ui.proposal_panel import ProposalPanel
    async with TalosApp().run_test() as pilot:
        panel = pilot.app.query_one(ProposalPanel)
        assert panel.display is False
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py::test_proposal_panel_exists -v`
Expected: FAIL — no `ProposalPanel` in the widget tree

**Step 3: Modify app.py**

Changes to `src/talos/ui/app.py`:

1. Import `ProposalPanel` and `ProposalQueue`
2. In `compose()`: add `yield ProposalPanel(queue, id="proposal-panel")`
3. In `on_mount()`: wire panel refresh to polling cycle
4. Handle `ProposalPanel.Approved` and `ProposalPanel.Rejected` messages

```python
# In compose():
from talos.ui.proposal_panel import ProposalPanel

yield ProposalPanel(
    self._engine.proposal_queue if self._engine else ProposalQueue(),
    id="proposal-panel",
)

# In on_mount(), add to the engine block:
self.set_interval(1.0, self._refresh_proposals)

# New methods:
def _refresh_proposals(self) -> None:
    panel = self.query_one(ProposalPanel)
    panel.refresh_proposals()

def on_proposal_panel_approved(self, event: ProposalPanel.Approved) -> None:
    self._execute_approval(event.key)

@work(thread=False)
async def _execute_approval(self, key: ProposalKey) -> None:
    if self._engine is not None:
        await self._engine.approve_adjustment(key)
    self.query_one(ProposalPanel).refresh_proposals()

def on_proposal_panel_rejected(self, event: ProposalPanel.Rejected) -> None:
    if self._engine is not None:
        self._engine.reject_adjustment(event.key)
    self.query_one(ProposalPanel).refresh_proposals()
```

Also remove the old `_on_adjustment_proposed` toast callback (line 78-88) since proposals now go through the panel.

**Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_ui.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/talos/ui/app.py src/talos/ui/theme.py tests/test_ui.py
git commit -m "feat: mount ProposalPanel in TalosApp with approve/reject wiring"
```

---

## Phase 4: OpportunityProposer

### Task 7: OpportunityProposer Pure State Machine

**Files:**
- Create: `src/talos/opportunity_proposer.py`
- Test: `tests/test_opportunity_proposer.py`

**Step 1: Write the failing tests**

```python
# tests/test_opportunity_proposer.py
"""Tests for OpportunityProposer — pure decision logic."""

from datetime import UTC, datetime, timedelta

from talos.automation_config import AutomationConfig
from talos.models.proposal import ProposalKey
from talos.models.strategy import ArbPair, Opportunity
from talos.opportunity_proposer import OpportunityProposer
from talos.position_ledger import PositionLedger, Side


def _make_opp(
    event_ticker: str = "EVT-1",
    no_a: int = 48,
    no_b: int = 50,
    fee_edge: float = 1.5,
) -> Opportunity:
    return Opportunity(
        event_ticker=event_ticker,
        ticker_a="TK-A",
        ticker_b="TK-B",
        no_a=no_a,
        no_b=no_b,
        qty_a=100,
        qty_b=100,
        raw_edge=100 - no_a - no_b,
        fee_edge=fee_edge,
        tradeable_qty=100,
        timestamp=datetime.now(UTC).isoformat(),
    )


class TestEdgeThreshold:
    def test_below_threshold_no_proposal(self):
        cfg = AutomationConfig(edge_threshold_cents=1.5, stability_seconds=0.0)
        proposer = OpportunityProposer(cfg)
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        opp = _make_opp(fee_edge=1.0)  # below threshold
        result = proposer.evaluate(pair, opp, ledger, pending_keys=set())
        assert result is None

    def test_above_threshold_with_zero_stability_proposes(self):
        cfg = AutomationConfig(edge_threshold_cents=1.5, stability_seconds=0.0)
        proposer = OpportunityProposer(cfg)
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        opp = _make_opp(fee_edge=2.0)
        result = proposer.evaluate(pair, opp, ledger, pending_keys=set())
        assert result is not None
        assert result.bid.no_a == 48
        assert result.bid.no_b == 50


class TestPositionGate:
    def test_resting_on_both_sides_no_proposal(self):
        cfg = AutomationConfig(edge_threshold_cents=1.5, stability_seconds=0.0)
        proposer = OpportunityProposer(cfg)
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        ledger.record_resting(Side.A, order_id="o1", count=10, price=48)
        ledger.record_resting(Side.B, order_id="o2", count=10, price=50)
        opp = _make_opp(fee_edge=2.0)
        result = proposer.evaluate(pair, opp, ledger, pending_keys=set())
        assert result is None


class TestStabilityFilter:
    def test_first_sight_starts_timer_no_proposal(self):
        cfg = AutomationConfig(edge_threshold_cents=1.5, stability_seconds=5.0)
        proposer = OpportunityProposer(cfg)
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        opp = _make_opp(fee_edge=2.0)
        t1 = datetime.now(UTC)
        result = proposer.evaluate(pair, opp, ledger, pending_keys=set(), now=t1)
        assert result is None  # stability not met yet

    def test_stable_long_enough_proposes(self):
        cfg = AutomationConfig(edge_threshold_cents=1.5, stability_seconds=5.0)
        proposer = OpportunityProposer(cfg)
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        opp = _make_opp(fee_edge=2.0)
        t1 = datetime.now(UTC)
        proposer.evaluate(pair, opp, ledger, pending_keys=set(), now=t1)
        t2 = t1 + timedelta(seconds=6)
        result = proposer.evaluate(pair, opp, ledger, pending_keys=set(), now=t2)
        assert result is not None

    def test_edge_drops_resets_timer(self):
        cfg = AutomationConfig(edge_threshold_cents=1.5, stability_seconds=5.0)
        proposer = OpportunityProposer(cfg)
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        t1 = datetime.now(UTC)
        # First: edge above threshold
        proposer.evaluate(pair, _make_opp(fee_edge=2.0), ledger, pending_keys=set(), now=t1)
        # Second: edge drops below
        t2 = t1 + timedelta(seconds=3)
        proposer.evaluate(pair, _make_opp(fee_edge=1.0), ledger, pending_keys=set(), now=t2)
        # Third: edge returns, but timer should have reset
        t3 = t2 + timedelta(seconds=3)
        result = proposer.evaluate(pair, _make_opp(fee_edge=2.0), ledger, pending_keys=set(), now=t3)
        assert result is None  # only 3s since reset, need 5s


class TestDuplicatePrevention:
    def test_no_proposal_when_pending_exists(self):
        cfg = AutomationConfig(edge_threshold_cents=1.5, stability_seconds=0.0)
        proposer = OpportunityProposer(cfg)
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        opp = _make_opp(fee_edge=2.0)
        pending = {ProposalKey(event_ticker="EVT-1", side="", kind="bid")}
        result = proposer.evaluate(pair, opp, ledger, pending_keys=pending)
        assert result is None


class TestCooldown:
    def test_cooldown_after_rejection(self):
        cfg = AutomationConfig(
            edge_threshold_cents=1.5,
            stability_seconds=0.0,
            rejection_cooldown_seconds=30.0,
        )
        proposer = OpportunityProposer(cfg)
        pair = ArbPair(event_ticker="EVT-1", ticker_a="TK-A", ticker_b="TK-B")
        ledger = PositionLedger(event_ticker="EVT-1", unit_size=10)
        opp = _make_opp(fee_edge=2.0)
        t1 = datetime.now(UTC)
        proposer.record_rejection("EVT-1", t1)
        # Within cooldown — no proposal
        t2 = t1 + timedelta(seconds=10)
        result = proposer.evaluate(pair, opp, ledger, pending_keys=set(), now=t2)
        assert result is None
        # Past cooldown — should propose
        t3 = t1 + timedelta(seconds=31)
        result = proposer.evaluate(pair, opp, ledger, pending_keys=set(), now=t3)
        assert result is not None
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_opportunity_proposer.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# src/talos/opportunity_proposer.py
"""OpportunityProposer — pure decision logic for initial bid proposals.

Watches scanner output + ledger state, applies edge threshold + stability
filter, emits bid proposals. Pure state machine — no I/O, no async.
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
    """Proposes initial bids when conditions are met.

    All gates must pass: edge threshold, position gate, stability filter,
    no pending proposal, cooldown after rejection.
    """

    def __init__(self, config: AutomationConfig) -> None:
        self._config = config
        # Stability tracking: event_ticker → first_seen_at
        self._stable_since: dict[str, datetime] = {}
        # Rejection cooldown: event_ticker → rejected_at
        self._rejected_at: dict[str, datetime] = {}

    def evaluate(
        self,
        pair: ArbPair,
        opportunity: Opportunity,
        ledger: PositionLedger,
        pending_keys: set[ProposalKey],
        now: datetime | None = None,
    ) -> Proposal | None:
        """Evaluate whether to propose a bid on this opportunity.

        Returns a Proposal envelope if all gates pass, None otherwise.
        """
        now = now or datetime.now(UTC)
        et = pair.event_ticker

        # Gate 1: Edge threshold
        if opportunity.fee_edge < self._config.edge_threshold_cents:
            self._stable_since.pop(et, None)
            return None

        # Gate 2: Position gate — don't propose if already have a unit resting on both sides
        has_a = ledger.resting_count(Side.A) > 0 or ledger.filled_count(Side.A) >= ledger.unit_size
        has_b = ledger.resting_count(Side.B) > 0 or ledger.filled_count(Side.B) >= ledger.unit_size
        if has_a and has_b:
            return None

        # Gate 3: No pending bid proposal for this event
        bid_key = ProposalKey(event_ticker=et, side="", kind="bid")
        if bid_key in pending_keys:
            return None

        # Gate 4: Cooldown after rejection
        rejected = self._rejected_at.get(et)
        if rejected is not None:
            elapsed = (now - rejected).total_seconds()
            if elapsed < self._config.rejection_cooldown_seconds:
                return None

        # Gate 5: Stability filter
        if self._config.stability_seconds > 0:
            first_seen = self._stable_since.get(et)
            if first_seen is None:
                self._stable_since[et] = now
                return None
            stable_for = (now - first_seen).total_seconds()
            if stable_for < self._config.stability_seconds:
                return None
        else:
            stable_for = 0.0

        # All gates passed — build proposal
        bid = ProposedBid(
            event_ticker=et,
            ticker_a=pair.ticker_a,
            ticker_b=pair.ticker_b,
            no_a=opportunity.no_a,
            no_b=opportunity.no_b,
            qty=self._config.unit_size,
            edge_cents=opportunity.fee_edge,
            stable_for_seconds=stable_for,
            reason=(
                f"edge {opportunity.fee_edge:.1f}c stable {stable_for:.1f}s, "
                f"no position"
            ),
        )
        key = ProposalKey(event_ticker=et, side="", kind="bid")
        return Proposal(
            key=key,
            kind="bid",
            summary=f"BID {et} edge {opportunity.fee_edge:.1f}c",
            detail=bid.reason,
            created_at=now,
            bid=bid,
        )

    def record_rejection(self, event_ticker: str, now: datetime | None = None) -> None:
        """Record that a bid proposal was rejected (starts cooldown)."""
        self._rejected_at[event_ticker] = now or datetime.now(UTC)
        self._stable_since.pop(event_ticker, None)
```

**Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_opportunity_proposer.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/talos/opportunity_proposer.py tests/test_opportunity_proposer.py
git commit -m "feat: add OpportunityProposer with edge/stability/position gates"
```

---

### Task 8: Wire OpportunityProposer into Engine

**Files:**
- Modify: `src/talos/engine.py`
- Modify: `tests/test_engine.py`

**Step 1: Write the failing test**

```python
# Add to tests/test_engine.py:

def test_opportunity_proposer_adds_bid_to_queue(engine_with_automation):
    """When a stable profitable opportunity exists, a bid proposal enters the queue."""
    engine = engine_with_automation
    # Simulate scanner having an opportunity with sufficient edge
    # and enough time passing for stability
    # Then call engine.evaluate_opportunities()
    # Assert queue has a bid proposal
    ...
```

The exact test depends on how you set up the engine fixture with scanner state. The key assertion: after calling `engine.evaluate_opportunities()` (new method), a bid `Proposal` appears in the queue.

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py::test_opportunity_proposer_adds_bid_to_queue -v`
Expected: FAIL

**Step 3: Modify engine.py**

Add to `TradingEngine.__init__`:
```python
from talos.opportunity_proposer import OpportunityProposer
from talos.automation_config import AutomationConfig

# New parameter:
automation_config: AutomationConfig | None = None,

# Store:
self._auto_config = automation_config or AutomationConfig()
self._proposer = OpportunityProposer(self._auto_config)
```

Add new method:
```python
def evaluate_opportunities(self) -> None:
    """Run OpportunityProposer against all scanner pairs."""
    if not self._auto_config.enabled:
        return
    pending_keys = {p.key for p in self._proposal_queue.pending()}
    for pair in self._scanner.pairs:
        opp = self._scanner.get_opportunity(pair.event_ticker)
        if opp is None:
            continue
        try:
            ledger = self._adjuster.get_ledger(pair.event_ticker)
        except KeyError:
            continue
        proposal = self._proposer.evaluate(pair, opp, ledger, pending_keys)
        if proposal is not None:
            self._proposal_queue.add(proposal)
            pending_keys.add(proposal.key)
```

Call `evaluate_opportunities()` at the end of `refresh_account()` (after positions are updated).

Add bid approval in `approve_adjustment` (rename to `approve_proposal`):
```python
async def approve_proposal(self, key: ProposalKey) -> None:
    try:
        envelope = self._proposal_queue.approve(key)
    except KeyError:
        self._notify("No pending proposal", "warning")
        return
    if envelope.kind == "adjustment" and envelope.adjustment:
        try:
            await self._adjuster.execute(envelope.adjustment, self._rest)
            self._notify(f"Adjusted: {envelope.adjustment.event_ticker} → {envelope.adjustment.new_price}c")
        except Exception as e:
            self._notify(f"Adjustment FAILED: {e}", "error")
    elif envelope.kind == "bid" and envelope.bid:
        bid = envelope.bid
        from talos.models.strategy import BidConfirmation
        confirmation = BidConfirmation(
            ticker_a=bid.ticker_a,
            ticker_b=bid.ticker_b,
            no_a=bid.no_a,
            no_b=bid.no_b,
            qty=bid.qty,
        )
        await self.place_bids(confirmation)
```

Wire rejection cooldown:
```python
def reject_proposal(self, key: ProposalKey) -> None:
    self._proposal_queue.reject(key)
    if key.kind == "bid":
        self._proposer.record_rejection(key.event_ticker)
    self._notify(f"Rejected: {key.event_ticker} {key.kind}")
```

**Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/talos/engine.py tests/test_engine.py
git commit -m "feat: wire OpportunityProposer into engine polling cycle"
```

---

## Phase 5: Cleanup and Integration

### Task 9: Update TalosApp for Bid Proposals

**Files:**
- Modify: `src/talos/ui/app.py`

Wire the `ProposalPanel.Approved` and `ProposalPanel.Rejected` messages to handle both adjustment and bid proposals (already partially done in Task 6). Ensure:

1. `on_proposal_panel_approved` calls `engine.approve_proposal(key)` (which routes by kind)
2. `on_proposal_panel_rejected` calls `engine.reject_proposal(key)` (which records cooldown for bids)
3. Remove the old `approve_adjustment(event_ticker, side_value)` and `reject_adjustment(event_ticker, side_value)` methods from TalosApp (dead code after queue migration)
4. Remove `_on_adjustment_proposed` toast callback (replaced by panel)

**Commit:**

```bash
git add src/talos/ui/app.py
git commit -m "refactor: route all proposal approval/rejection through ProposalPanel"
```

---

### Task 10: Remove Dead Code from Engine

**Files:**
- Modify: `src/talos/engine.py`

1. Remove `self.on_proposal` callback (declared line 79, never used by engine)
2. Remove old `approve_adjustment(event_ticker, side_value)` signature if a compatibility shim was kept
3. Remove old `reject_adjustment(event_ticker, side_value)` signature
4. Ensure `adjuster.on_proposal` callback is removed from engine wiring (proposals now go through queue)

**Commit:**

```bash
git add src/talos/engine.py
git commit -m "refactor: remove dead proposal callback and legacy approval methods"
```

---

### Task 11: Run Full Test Suite + Lint

Run all tests, ruff, and pyright to ensure nothing is broken:

```bash
.venv/Scripts/python -m pytest -v
.venv/Scripts/python -m ruff check src/ tests/
.venv/Scripts/python -m pyright
```

Fix any issues, then commit:

```bash
git commit -m "chore: fix lint/type issues from supervised automation"
```

---

### Task 12: Update Brain Vault

**Files:**
- Modify: `brain/architecture.md` — update Layer 6 (Automation) description
- Modify: `brain/principles.md` — update Principle 2 to reflect "supervised" stage
- Modify: `brain/index.md` — add link to new plan

**Commit:**

```bash
git add brain/
git commit -m "docs: update brain vault for supervised automation"
```
