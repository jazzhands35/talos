# Auto-Accept Mode Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a TUI-toggled auto-accept mode that automatically approves all pending proposals for a timed duration, with JSONL session logging for post-analysis.

**Architecture:** A 1-second timer in `app.py` drains the proposal queue by calling the same `_execute_approval()` path the Y key uses. A new `AutoAcceptLogger` writes JSONL snapshots capturing full state on each action. Zero changes to engine, queue, or safety gates.

**Tech Stack:** Python 3.12, Textual (TUI), structlog, Pydantic v2, JSONL output

---

### Task 1: AutoAcceptState dataclass

**Files:**
- Create: `src/talos/auto_accept.py`
- Test: `tests/test_auto_accept.py`

**Step 1: Write the failing test**

```python
# tests/test_auto_accept.py
"""Tests for auto-accept state management."""

from datetime import UTC, datetime, timedelta

from talos.auto_accept import AutoAcceptState


def test_initial_state_inactive():
    state = AutoAcceptState()
    assert state.active is False
    assert state.started_at is None
    assert state.duration is None
    assert state.accepted_count == 0


def test_start_sets_active():
    state = AutoAcceptState()
    state.start(hours=2.0)
    assert state.active is True
    assert state.started_at is not None
    assert state.duration == timedelta(hours=2)
    assert state.accepted_count == 0


def test_stop_clears_active():
    state = AutoAcceptState()
    state.start(hours=1.0)
    state.stop()
    assert state.active is False


def test_is_expired_false_within_duration():
    state = AutoAcceptState()
    state.start(hours=2.0)
    assert state.is_expired() is False


def test_is_expired_true_after_duration():
    state = AutoAcceptState()
    state.start(hours=1.0)
    # Backdate start time
    state.started_at = datetime.now(UTC) - timedelta(hours=1, minutes=1)
    assert state.is_expired() is True


def test_remaining_seconds():
    state = AutoAcceptState()
    state.start(hours=1.0)
    remaining = state.remaining_seconds()
    assert 3590 < remaining <= 3600


def test_remaining_seconds_inactive_returns_zero():
    state = AutoAcceptState()
    assert state.remaining_seconds() == 0.0


def test_elapsed_str_format():
    state = AutoAcceptState()
    state.start(hours=1.0)
    # Backdate 35 minutes
    state.started_at = datetime.now(UTC) - timedelta(minutes=35)
    elapsed = state.elapsed_str()
    assert elapsed.startswith("0:35:")


def test_remaining_str_format():
    state = AutoAcceptState()
    state.start(hours=1.0)
    remaining = state.remaining_str()
    # Should be ~"0:59:xx" format
    assert ":" in remaining
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_auto_accept.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'talos.auto_accept'"

**Step 3: Write minimal implementation**

```python
# src/talos/auto_accept.py
"""Auto-accept mode state management."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


@dataclass
class AutoAcceptState:
    """Tracks whether auto-accept is active, for how long, and how many accepted."""

    active: bool = False
    started_at: datetime | None = None
    duration: timedelta | None = None
    accepted_count: int = 0

    def start(self, hours: float) -> None:
        """Activate auto-accept for the given duration."""
        self.active = True
        self.started_at = datetime.now(UTC)
        self.duration = timedelta(hours=hours)
        self.accepted_count = 0

    def stop(self) -> None:
        """Deactivate auto-accept."""
        self.active = False

    def is_expired(self) -> bool:
        """True if the duration has elapsed."""
        if not self.active or self.started_at is None or self.duration is None:
            return False
        return datetime.now(UTC) >= self.started_at + self.duration

    def remaining_seconds(self) -> float:
        """Seconds remaining, or 0.0 if inactive/expired."""
        if not self.active or self.started_at is None or self.duration is None:
            return 0.0
        end = self.started_at + self.duration
        remaining = (end - datetime.now(UTC)).total_seconds()
        return max(0.0, remaining)

    def remaining_str(self) -> str:
        """Human-readable remaining time, e.g. '1:23:45'."""
        secs = int(self.remaining_seconds())
        h, remainder = divmod(secs, 3600)
        m, s = divmod(remainder, 60)
        return f"{h}:{m:02d}:{s:02d}"

    def elapsed_str(self) -> str:
        """Human-readable elapsed time since start."""
        if self.started_at is None:
            return "0:00:00"
        elapsed = (datetime.now(UTC) - self.started_at).total_seconds()
        secs = int(max(0, elapsed))
        h, remainder = divmod(secs, 3600)
        m, s = divmod(remainder, 60)
        return f"{h}:{m:02d}:{s:02d}"
```

**Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_auto_accept.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/talos/auto_accept.py tests/test_auto_accept.py
git commit -m "feat: add AutoAcceptState dataclass for timed auto-accept mode"
```

---

### Task 2: AutoAcceptLogger (JSONL session logging)

**Files:**
- Create: `src/talos/auto_accept_log.py`
- Test: `tests/test_auto_accept_log.py`

**Step 1: Write the failing test**

```python
# tests/test_auto_accept_log.py
"""Tests for JSONL auto-accept session logger."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from talos.auto_accept import AutoAcceptState
from talos.auto_accept_log import AutoAcceptLogger
from talos.models.proposal import Proposal, ProposalKey, ProposedBid


def _make_proposal() -> Proposal:
    return Proposal(
        key=ProposalKey(event_ticker="TENN-A", side="", kind="bid"),
        kind="bid",
        summary="Bid TENN-A @ 45/55 NO",
        detail="2.5c edge, qty 10",
        created_at=datetime.now(UTC),
        bid=ProposedBid(
            event_ticker="TENN-A",
            ticker_a="TENN-A-T1",
            ticker_b="TENN-A-T2",
            no_a=45,
            no_b=55,
            qty=10,
            edge_cents=2.5,
            stable_for_seconds=5.0,
            reason="edge above threshold",
        ),
    )


def test_session_start_writes_jsonl(tmp_path: Path):
    log_dir = tmp_path / "sessions"
    logger = AutoAcceptLogger(log_dir)
    state = AutoAcceptState()
    state.start(hours=2.0)
    config = {"edge_threshold_cents": 1.0, "unit_size": 10}

    logger.log_session_start(state, config)

    files = list(log_dir.glob("*.jsonl"))
    assert len(files) == 1
    line = json.loads(files[0].read_text().strip())
    assert line["event"] == "session_start"
    assert line["config"]["unit_size"] == 10
    assert "duration_hours" in line


def test_log_accepted_writes_state_snapshot(tmp_path: Path):
    log_dir = tmp_path / "sessions"
    logger = AutoAcceptLogger(log_dir)
    state = AutoAcceptState()
    state.start(hours=1.0)
    logger.log_session_start(state, {})

    proposal = _make_proposal()
    snapshot = {
        "positions": {},
        "balance_cents": 50000,
        "resting_orders": [],
        "top_of_market": {},
    }

    logger.log_accepted(proposal, snapshot, state)

    files = list(log_dir.glob("*.jsonl"))
    lines = files[0].read_text().strip().split("\n")
    assert len(lines) == 2  # session_start + accepted
    entry = json.loads(lines[1])
    assert entry["event"] == "auto_accepted"
    assert entry["proposal"]["kind"] == "bid"
    assert entry["state_snapshot"]["balance_cents"] == 50000
    assert "session" in entry


def test_log_session_end(tmp_path: Path):
    log_dir = tmp_path / "sessions"
    logger = AutoAcceptLogger(log_dir)
    state = AutoAcceptState()
    state.start(hours=1.0)
    state.accepted_count = 5
    logger.log_session_start(state, {})

    logger.log_session_end(state, final_positions={})

    files = list(log_dir.glob("*.jsonl"))
    lines = files[0].read_text().strip().split("\n")
    last = json.loads(lines[-1])
    assert last["event"] == "session_end"
    assert last["total_accepted"] == 5


def test_log_error(tmp_path: Path):
    log_dir = tmp_path / "sessions"
    logger = AutoAcceptLogger(log_dir)
    state = AutoAcceptState()
    state.start(hours=1.0)
    logger.log_session_start(state, {})

    proposal = _make_proposal()
    logger.log_error(proposal, "API timeout", {"balance_cents": 50000}, state)

    files = list(log_dir.glob("*.jsonl"))
    lines = files[0].read_text().strip().split("\n")
    last = json.loads(lines[-1])
    assert last["event"] == "auto_accept_error"
    assert last["error"] == "API timeout"
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_auto_accept_log.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'talos.auto_accept_log'"

**Step 3: Write minimal implementation**

```python
# src/talos/auto_accept_log.py
"""JSONL session logger for auto-accept mode."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from talos.auto_accept import AutoAcceptState
    from talos.models.proposal import Proposal


class AutoAcceptLogger:
    """Writes JSONL logs — one file per auto-accept session."""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._current_file: Path | None = None

    def log_session_start(
        self, state: AutoAcceptState, config: dict[str, Any]
    ) -> None:
        """Create session file and write the start event."""
        self._log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC)
        filename = ts.strftime("%Y-%m-%d_%H%M%S") + ".jsonl"
        self._current_file = self._log_dir / filename
        duration_hours = (
            state.duration.total_seconds() / 3600 if state.duration else 0
        )
        self._write(
            {
                "timestamp": ts.isoformat(),
                "event": "session_start",
                "config": config,
                "duration_hours": duration_hours,
            }
        )

    def log_accepted(
        self,
        proposal: Proposal,
        state_snapshot: dict[str, Any],
        state: AutoAcceptState,
    ) -> None:
        """Log an auto-accepted proposal with full state snapshot."""
        self._write(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": "auto_accepted",
                "proposal": {
                    "kind": proposal.kind,
                    "event_ticker": proposal.key.event_ticker,
                    "side": proposal.key.side,
                    "summary": proposal.summary,
                    "detail": proposal.detail,
                },
                "state_snapshot": state_snapshot,
                "session": {
                    "started_at": (
                        state.started_at.isoformat() if state.started_at else None
                    ),
                    "elapsed": state.elapsed_str(),
                    "accepted_count": state.accepted_count,
                },
            }
        )

    def log_error(
        self,
        proposal: Proposal,
        error: str,
        state_snapshot: dict[str, Any],
        state: AutoAcceptState,
    ) -> None:
        """Log an auto-accept failure."""
        self._write(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": "auto_accept_error",
                "proposal": {
                    "kind": proposal.kind,
                    "event_ticker": proposal.key.event_ticker,
                    "summary": proposal.summary,
                },
                "error": error,
                "state_snapshot": state_snapshot,
                "session": {
                    "elapsed": state.elapsed_str(),
                    "accepted_count": state.accepted_count,
                },
            }
        )

    def log_session_end(
        self, state: AutoAcceptState, final_positions: dict[str, Any]
    ) -> None:
        """Write the session end summary."""
        self._write(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": "session_end",
                "total_accepted": state.accepted_count,
                "elapsed": state.elapsed_str(),
                "final_positions": final_positions,
            }
        )

    def _write(self, data: dict[str, Any]) -> None:
        if self._current_file is None:
            return
        with open(self._current_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, default=str) + "\n")
```

**Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_auto_accept_log.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/talos/auto_accept_log.py tests/test_auto_accept_log.py
git commit -m "feat: add AutoAcceptLogger for JSONL session logging"
```

---

### Task 3: Duration input screen

**Files:**
- Modify: `src/talos/ui/screens.py`
- Test: `tests/test_auto_accept.py` (add to existing)

**Step 1: Write the failing test**

Append to `tests/test_auto_accept.py`:

```python
# -- Screen tests (import separately) --
from talos.ui.screens import AutoAcceptScreen


def test_auto_accept_screen_exists():
    """Verify the screen class is importable."""
    assert AutoAcceptScreen is not None
```

**Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_auto_accept.py::test_auto_accept_screen_exists -v`
Expected: FAIL with "ImportError: cannot import name 'AutoAcceptScreen'"

**Step 3: Write minimal implementation**

Add to `src/talos/ui/screens.py` (after the existing `UnitSizeScreen` class, around line 83):

```python
class AutoAcceptScreen(ModalScreen[float | None]):
    """Modal for entering auto-accept duration in hours."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("Auto-Accept Mode", classes="modal-title")
            yield Label("How many hours to auto-accept proposals?")
            yield Input(
                value="2.0",
                id="hours-input",
                type="number",
            )
            yield Label("", id="modal-error", classes="modal-error")
            with Vertical(id="modal-buttons"):
                yield Button("Cancel", id="cancel-btn", variant="default")
                yield Button("Start", id="start-btn", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
        elif event.button.id == "start-btn":
            hours_input = self.query_one("#hours-input", Input)
            try:
                hours = float(hours_input.value)
            except ValueError:
                self.query_one("#modal-error", Label).update("Enter a valid number")
                return
            if hours <= 0 or hours > 24:
                self.query_one("#modal-error", Label).update(
                    "Duration must be between 0 and 24 hours"
                )
                return
            self.dismiss(hours)
```

**Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_auto_accept.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/talos/ui/screens.py tests/test_auto_accept.py
git commit -m "feat: add AutoAcceptScreen modal for duration input"
```

---

### Task 4: Wire auto-accept into the TUI app

**Files:**
- Modify: `src/talos/ui/app.py`

This is the integration task — wiring the state, timer, keybinding, and logger into the app. No new test file; this is TUI integration tested by running the app.

**Step 1: Add imports and state to `app.py`**

Add to the imports at top of `src/talos/ui/app.py` (after existing imports):

```python
from talos.auto_accept import AutoAcceptState
from talos.auto_accept_log import AutoAcceptLogger
from talos.ui.screens import AutoAcceptScreen
```

Note: `AutoAcceptScreen` needs to be added to the existing import from `talos.ui.screens` — merge it:

```python
from talos.ui.screens import AddGamesScreen, AutoAcceptScreen, BidScreen, UnitSizeScreen
```

**Step 2: Add state and binding to `TalosApp`**

In the `BINDINGS` list (line 36-44), add before the quit binding:

```python
("f", "toggle_auto_accept", "Auto-Accept"),
```

In `__init__` (line 47-56), add after `self._scanner`:

```python
self._auto_accept = AutoAcceptState()
self._auto_accept_logger: AutoAcceptLogger | None = None
```

**Step 3: Add timer to `on_mount`**

In `on_mount` (after line 78 `self.set_interval(1.0, self._refresh_proposals)`), add:

```python
self.set_interval(1.0, self._auto_accept_tick)
```

**Step 4: Add the auto-accept tick method**

After the `_refresh_proposals` method (after line 91):

```python
@work(thread=False)
async def _auto_accept_tick(self) -> None:
    """Each second: if auto-accept is active, approve the oldest pending proposal."""
    if not self._auto_accept.active or self._engine is None:
        return

    # Check expiry
    if self._auto_accept.is_expired():
        self._stop_auto_accept()
        return

    pending = self._engine.proposal_queue.pending()
    if not pending:
        return

    proposal = pending[0]
    try:
        snapshot = self._capture_state_snapshot()
        await self._engine.approve_proposal(proposal.key)
        self._auto_accept.accepted_count += 1
        if self._auto_accept_logger:
            self._auto_accept_logger.log_accepted(
                proposal, snapshot, self._auto_accept
            )
    except Exception as e:
        logger.exception("auto_accept_error", proposal_key=str(proposal.key))
        if self._auto_accept_logger:
            snapshot = self._capture_state_snapshot()
            self._auto_accept_logger.log_error(
                proposal, str(e), snapshot, self._auto_accept
            )

    self.query_one(ProposalPanel).refresh_proposals()
```

**Step 5: Add state snapshot capture**

After the tick method:

```python
def _capture_state_snapshot(self) -> dict[str, object]:
    """Capture full trading state for JSONL logging."""
    if self._engine is None:
        return {}

    positions: dict[str, dict[str, object]] = {}
    for summary in self._engine.position_summaries:
        positions[summary.event_ticker] = {
            "status": summary.status,
            "side_a_filled": summary.side_a_filled,
            "side_a_resting": summary.side_a_resting,
            "side_b_filled": summary.side_b_filled,
            "side_b_resting": summary.side_b_resting,
        }

    resting_orders = [
        {
            "ticker": o.ticker,
            "price": o.no_price,
            "remaining": o.remaining_count,
            "side": o.side,
            "status": o.status,
        }
        for o in self._engine.orders
        if o.status == "resting"
    ]

    top_of_market: dict[str, int | None] = {}
    if self._engine.tracker:
        for ticker in self._engine.tracker.all_tickers():
            top_of_market[ticker] = self._engine.tracker.top_price(ticker)

    opportunities = []
    if self._scanner:
        for opp in self._scanner.opportunities:
            opportunities.append(
                {
                    "event_ticker": opp.event_ticker,
                    "edge": opp.fee_edge,
                    "no_a": opp.no_a,
                    "no_b": opp.no_b,
                }
            )

    return {
        "positions": positions,
        "balance_cents": self._engine.balance,
        "portfolio_value_cents": self._engine.portfolio_value,
        "resting_orders": resting_orders,
        "top_of_market": top_of_market,
        "scanner_opportunities": opportunities,
    }
```

**Step 6: Add the toggle action and helpers**

```python
def action_toggle_auto_accept(self) -> None:
    if self._engine is None:
        return
    if self._auto_accept.active:
        self._stop_auto_accept()
    else:
        self._open_auto_accept()

@work(thread=False, exclusive=True, group="auto_accept")
async def _open_auto_accept(self) -> None:
    hours = await self.push_screen_wait(AutoAcceptScreen())
    if hours is not None and self._engine is not None:
        self._start_auto_accept(hours)

def _start_auto_accept(self, hours: float) -> None:
    """Activate auto-accept for the given duration."""
    from pathlib import Path

    self._auto_accept.start(hours=hours)

    log_dir = Path(__file__).resolve().parents[3] / "auto_accept_sessions"
    self._auto_accept_logger = AutoAcceptLogger(log_dir)

    config = {
        "edge_threshold_cents": self._engine.automation_config.edge_threshold_cents,
        "stability_seconds": self._engine.automation_config.stability_seconds,
        "unit_size": self._engine.unit_size,
    }
    self._auto_accept_logger.log_session_start(self._auto_accept, config)

    self.notify(
        f"Auto-accept ON — {hours:.1f}h",
        severity="warning",
        markup=False,
    )
    logger.info("auto_accept_started", hours=hours)

def _stop_auto_accept(self) -> None:
    """Deactivate auto-accept and log session end."""
    count = self._auto_accept.accepted_count
    elapsed = self._auto_accept.elapsed_str()

    if self._auto_accept_logger and self._engine:
        final_positions: dict[str, object] = {}
        for s in self._engine.position_summaries:
            final_positions[s.event_ticker] = {
                "status": s.status,
                "side_a_filled": s.side_a_filled,
                "side_b_filled": s.side_b_filled,
            }
        self._auto_accept_logger.log_session_end(
            self._auto_accept, final_positions
        )

    self._auto_accept.stop()
    self._auto_accept_logger = None

    self.notify(
        f"Auto-accept OFF — {count} accepted in {elapsed}",
        severity="information",
        markup=False,
    )
    logger.info("auto_accept_stopped", accepted_count=count, elapsed=elapsed)
```

**Step 7: Update footer to show auto-accept status**

In the `_refresh_proposals` method (line 89-91), add a subtitle update:

```python
def _refresh_proposals(self) -> None:
    """Update the proposal panel from queue state."""
    self.query_one(ProposalPanel).refresh_proposals()
    # Update subtitle with auto-accept status
    if self._auto_accept.active:
        self.sub_title = (
            f"AUTO-ACCEPT {self._auto_accept.remaining_str()} remaining "
            f"({self._auto_accept.accepted_count} accepted)"
        )
    else:
        self.sub_title = ""
```

**Step 8: Run the full test suite**

Run: `.venv/Scripts/python -m pytest -x`
Expected: All PASS (no existing tests broken)

**Step 9: Commit**

```bash
git add src/talos/ui/app.py
git commit -m "feat: wire auto-accept mode into TUI with F key toggle and JSONL logging"
```

---

### Task 5: Add auto_accept_sessions to .gitignore

**Files:**
- Modify: `.gitignore`

**Step 1: Add the directory**

Append to `.gitignore`:

```
# Auto-accept session logs (JSONL)
auto_accept_sessions/
```

**Step 2: Commit**

```bash
git add .gitignore
git commit -m "chore: ignore auto-accept session logs"
```

---

### Task 6: Verify state snapshot data access

The `_capture_state_snapshot` method references several engine properties. This task verifies those properties exist and return the right shapes.

**Files:**
- Modify: `tests/test_auto_accept.py` (add snapshot shape tests)

**Step 1: Check engine properties**

Verify these are accessible (read-only, already confirmed in exploration):
- `engine.position_summaries` → `list[EventPositionSummary]` — has `.event_ticker`, `.status`, `.side_a_filled`, `.side_a_resting`, `.side_b_filled`, `.side_b_resting`
- `engine.orders` → `list[Order]` — has `.ticker`, `.no_price`, `.remaining_count`, `.side`, `.status`
- `engine.balance` → `int`
- `engine.portfolio_value` → `int`
- `engine.tracker.all_tickers()` and `.top_price(ticker)` — verify these exist

Run: `.venv/Scripts/python -m pytest tests/test_auto_accept.py tests/test_auto_accept_log.py -v`
Expected: All PASS

**Step 2: Verify tracker API**

Check `src/talos/top_of_market.py` for `all_tickers()` and `top_price()`. If `all_tickers()` doesn't exist, add a helper in `_capture_state_snapshot` that iterates `tracker._prices` keys or uses available API.

**Step 3: Commit any fixes**

```bash
git add -A
git commit -m "fix: ensure state snapshot fields align with engine API"
```

---

### Task 7: End-to-end smoke test

**Files:** None — manual runtime verification

**Step 1: Start Talos in demo mode**

Run: `.venv/Scripts/python -m talos`

**Step 2: Verify keybinding**

- Press **F** → AutoAcceptScreen modal should appear
- Enter **0.1** (6 minutes) → Press Start
- Subtitle should show "AUTO-ACCEPT 0:05:xx remaining"
- Press **F** again → should stop, show summary toast

**Step 3: Verify JSONL output**

Check `auto_accept_sessions/` directory for `.jsonl` file:

```bash
cat auto_accept_sessions/*.jsonl | python -m json.tool
```

Should contain `session_start` and `session_end` events.

**Step 4: Verify with live proposals**

- Add a game, enable suggestions (S), wait for proposal
- Start auto-accept (F) → proposal should auto-approve within 1s
- JSONL should contain `auto_accepted` entry with full state snapshot

**Step 5: Final commit**

```bash
git add -A
git commit -m "feat: auto-accept mode — timed auto-approval with JSONL session logging"
```

---

## Summary

| Task | What | Files |
|------|------|-------|
| 1 | AutoAcceptState dataclass | `auto_accept.py`, `test_auto_accept.py` |
| 2 | AutoAcceptLogger JSONL | `auto_accept_log.py`, `test_auto_accept_log.py` |
| 3 | Duration input screen | `screens.py`, `test_auto_accept.py` |
| 4 | Wire into TUI app | `app.py` |
| 5 | .gitignore | `.gitignore` |
| 6 | Verify snapshot API | tests + possible fixes |
| 7 | End-to-end smoke test | manual |
