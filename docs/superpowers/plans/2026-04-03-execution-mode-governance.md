# Execution Mode Governance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the implicit 168h auto-accept startup with an explicit, configurable execution mode system that separates startup policy, runtime state, and data health in the TUI status bar.

**Architecture:** Two execution modes (automatic/manual) with optional auto-stop timer on automatic. Settings.json defines boot policy (startup defaults, never rewritten at runtime). Status bar shows scan mode, execution mode, and data health as orthogonal always-visible dimensions.

**Tech Stack:** Python 3.12, Textual TUI, Pydantic, pytest

**Spec:** `docs/superpowers/specs/2026-04-03-execution-mode-governance.md`

**Design decision — staleness thresholds:** The UI data health indicator uses 60s (warn operator early). The orderbook recovery system uses 120s (`orderbook.py:18`, `_STALE_THRESHOLD`). These are intentionally different — the display warns before recovery acts. They should NOT share one constant.

---

### Task 1: ExecutionMode state machine

Replace `AutoAcceptState` internals in `auto_accept.py` with the new `ExecutionMode` model. Keep the file name.

**Files:**
- Modify: `src/talos/auto_accept.py` (full rewrite of class internals)
- Modify: `tests/test_auto_accept.py` (rewrite tests for new API)
- Delete content of: `tests/test_auto_accept_duration.py` (merge into main test file)

- [ ] **Step 1: Write failing tests for ExecutionMode**

Replace `tests/test_auto_accept.py` with:

```python
"""Tests for ExecutionMode state machine."""

from datetime import UTC, datetime, timedelta

from talos.auto_accept import ExecutionMode, Mode


def test_default_is_automatic():
    em = ExecutionMode()
    assert em.mode is Mode.AUTOMATIC
    assert em.is_automatic is True
    assert em.auto_stop_at is None
    assert em.accepted_count == 0


def test_enter_automatic_indefinite():
    em = ExecutionMode()
    em.enter_manual()  # start manual first
    em.enter_automatic()
    assert em.is_automatic is True
    assert em.auto_stop_at is None
    assert em.accepted_count == 0
    assert em.started_at is not None


def test_enter_automatic_with_timer():
    em = ExecutionMode()
    em.enter_automatic(hours=2.0)
    assert em.is_automatic is True
    assert em.auto_stop_at is not None
    assert em.accepted_count == 0


def test_enter_automatic_resets_accepted_count():
    em = ExecutionMode()
    em.enter_automatic()
    em.accepted_count = 15
    em.enter_automatic(hours=1.0)
    assert em.accepted_count == 0


def test_enter_manual():
    em = ExecutionMode()
    em.enter_automatic(hours=1.0)
    em.enter_manual()
    assert em.mode is Mode.MANUAL
    assert em.is_automatic is False
    assert em.auto_stop_at is None


def test_is_expired_false_when_indefinite():
    em = ExecutionMode()
    em.enter_automatic()
    assert em.is_expired() is False


def test_is_expired_false_within_duration():
    em = ExecutionMode()
    em.enter_automatic(hours=2.0)
    assert em.is_expired() is False


def test_is_expired_true_after_duration():
    em = ExecutionMode()
    em.enter_automatic(hours=1.0)
    em.started_at = datetime.now(UTC) - timedelta(hours=1, minutes=1)
    em.auto_stop_at = em.started_at + timedelta(hours=1)
    assert em.is_expired() is True


def test_is_expired_false_in_manual_mode():
    em = ExecutionMode()
    em.enter_manual()
    assert em.is_expired() is False


def test_remaining_str_with_timer():
    em = ExecutionMode()
    em.enter_automatic(hours=1.0)
    remaining = em.remaining_str()
    assert ":" in remaining
    assert remaining != ""


def test_remaining_str_indefinite_returns_empty():
    em = ExecutionMode()
    em.enter_automatic()
    assert em.remaining_str() == ""


def test_remaining_str_manual_returns_empty():
    em = ExecutionMode()
    em.enter_manual()
    assert em.remaining_str() == ""


def test_elapsed_str():
    em = ExecutionMode()
    em.enter_automatic()
    em.started_at = datetime.now(UTC) - timedelta(minutes=35)
    elapsed = em.elapsed_str()
    assert elapsed.startswith("0:35:")


def test_remaining_seconds_indefinite_returns_zero():
    em = ExecutionMode()
    em.enter_automatic()
    assert em.remaining_seconds() == 0.0


def test_remaining_seconds_with_timer():
    em = ExecutionMode()
    em.enter_automatic(hours=1.0)
    remaining = em.remaining_seconds()
    assert 3590 < remaining <= 3600
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_auto_accept.py -v`
Expected: ImportError — `ExecutionMode` and `Mode` don't exist yet.

- [ ] **Step 3: Implement ExecutionMode**

Replace `src/talos/auto_accept.py` with:

```python
"""Execution mode state management.

Two modes: Automatic (proposals auto-approve) and Manual (operator approves).
Optional auto_stop_at on automatic mode for timed sessions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum


class Mode(Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


@dataclass
class ExecutionMode:
    """Tracks current execution mode and optional auto-stop timer.

    This is runtime state, not persisted. Startup defaults come from
    settings.json — see persistence.py.
    """

    mode: Mode = Mode.AUTOMATIC
    auto_stop_at: datetime | None = None
    accepted_count: int = 0
    started_at: datetime | None = None

    def enter_automatic(self, hours: float | None = None) -> None:
        """Enter automatic mode. hours=None means indefinite."""
        self.mode = Mode.AUTOMATIC
        self.started_at = datetime.now(UTC)
        self.accepted_count = 0
        if hours is not None:
            self.auto_stop_at = self.started_at + timedelta(hours=hours)
        else:
            self.auto_stop_at = None

    def enter_manual(self) -> None:
        """Enter manual mode."""
        self.mode = Mode.MANUAL
        self.auto_stop_at = None

    @property
    def is_automatic(self) -> bool:
        return self.mode is Mode.AUTOMATIC

    def is_expired(self) -> bool:
        """True if auto_stop_at has passed. Always False if indefinite or manual."""
        if self.auto_stop_at is None:
            return False
        return datetime.now(UTC) >= self.auto_stop_at

    def remaining_seconds(self) -> float:
        """Seconds until auto_stop_at, or 0.0 if indefinite/manual/expired."""
        if self.auto_stop_at is None:
            return 0.0
        remaining = (self.auto_stop_at - datetime.now(UTC)).total_seconds()
        return max(0.0, remaining)

    def remaining_str(self) -> str:
        """Human-readable remaining time. Empty string if indefinite/manual."""
        if self.auto_stop_at is None:
            return ""
        secs = int(self.remaining_seconds())
        if secs <= 0:
            return ""
        h, remainder = divmod(secs, 3600)
        m, s = divmod(remainder, 60)
        return f"{h}:{m:02d}:{s:02d}"

    def elapsed_str(self) -> str:
        """Human-readable elapsed time since entering automatic mode."""
        if self.started_at is None:
            return "0:00:00"
        elapsed = (datetime.now(UTC) - self.started_at).total_seconds()
        secs = int(max(0, elapsed))
        h, remainder = divmod(secs, 3600)
        m, s = divmod(remainder, 60)
        return f"{h}:{m:02d}:{s:02d}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_auto_accept.py -v`
Expected: All pass.

- [ ] **Step 5: Delete test_auto_accept_duration.py (now covered by main tests)**

Delete the file `tests/test_auto_accept_duration.py` — the 168h tests and indefinite behavior are covered by the new test file.

- [ ] **Step 6: Verify full test suite still passes**

Run: `.venv/Scripts/python -m pytest tests/test_auto_accept.py tests/test_auto_accept_log.py -v`
Expected: `test_auto_accept_log.py` will FAIL because it imports `AutoAcceptState`. That's expected — we fix it in Task 2.

- [ ] **Step 7: Commit**

```bash
git add src/talos/auto_accept.py tests/test_auto_accept.py
git rm tests/test_auto_accept_duration.py
git commit -m "refactor: replace AutoAcceptState with ExecutionMode state machine

Two modes (automatic/manual) with optional auto-stop timer.
Session-local accepted_count resets on enter_automatic()."
```

---

### Task 2: Update AutoAcceptLogger for ExecutionMode

The logger imports `AutoAcceptState` — update it to work with `ExecutionMode`. Minimal changes: same JSONL format, same method signatures adapted for the new type.

**Files:**
- Modify: `src/talos/auto_accept_log.py`
- Modify: `tests/test_auto_accept_log.py`

- [ ] **Step 1: Update logger type hints**

In `src/talos/auto_accept_log.py`, change the import and type hints:

```python
# Change this import:
if TYPE_CHECKING:
    from talos.auto_accept import AutoAcceptState
    from talos.models.proposal import Proposal

# To this:
if TYPE_CHECKING:
    from talos.auto_accept import ExecutionMode
    from talos.models.proposal import Proposal
```

Then replace every `state: AutoAcceptState` parameter with `state: ExecutionMode` in:
- `log_session_start` (line 22)
- `log_accepted` (line 40)
- `log_error` (line 67)
- `log_session_end` (line 91)

In `log_session_start`, update the duration calculation:

```python
    def log_session_start(self, state: ExecutionMode, config: dict[str, Any]) -> None:
        """Create session file and write the start event."""
        self._log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC)
        filename = ts.strftime("%Y-%m-%d_%H%M%S") + ".jsonl"
        self._current_file = self._log_dir / filename
        if state.auto_stop_at and state.started_at:
            duration_hours = (state.auto_stop_at - state.started_at).total_seconds() / 3600
        else:
            duration_hours = None  # indefinite
        self._write(
            {
                "timestamp": ts.isoformat(),
                "event": "session_start",
                "config": config,
                "duration_hours": duration_hours,
                "mode": state.mode.value,
            }
        )
```

No other method bodies need changes — they access `.started_at`, `.elapsed_str()`, `.accepted_count` which exist on both old and new types.

- [ ] **Step 2: Update test imports**

In `tests/test_auto_accept_log.py`, change:

```python
# From:
from talos.auto_accept import AutoAcceptState

# To:
from talos.auto_accept import ExecutionMode
```

Then replace every `AutoAcceptState()` with `ExecutionMode()` and every `.start(hours=X)` with `.enter_automatic(hours=X)`:

- Line 39: `state = ExecutionMode()` then `state.enter_automatic(hours=2.0)`
- Line 56-57: `state = ExecutionMode()` then `state.enter_automatic(hours=1.0)`
- Line 83-84: `state = ExecutionMode()` then `state.enter_automatic(hours=1.0)`
- Line 99-100: `state = ExecutionMode()` then `state.enter_automatic(hours=1.0)`

- [ ] **Step 3: Run logger tests**

Run: `.venv/Scripts/python -m pytest tests/test_auto_accept_log.py -v`
Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add src/talos/auto_accept_log.py tests/test_auto_accept_log.py
git commit -m "refactor: update AutoAcceptLogger for ExecutionMode type"
```

---

### Task 3: Add data staleness query to OrderBookManager and TradingEngine

Add a method to query the most recent book update timestamp, so the UI can derive `DATA: LIVE` vs `DATA: STALE`.

**Files:**
- Modify: `src/talos/orderbook.py`
- Modify: `src/talos/engine.py`
- Modify: `tests/test_orderbook.py`
- Create: `tests/test_data_staleness.py`

- [ ] **Step 1: Write failing test for OrderBookManager.most_recent_update()**

Add to `tests/test_orderbook.py`:

```python
def test_most_recent_update_no_books():
    mgr = OrderBookManager()
    assert mgr.most_recent_update() == 0.0


def test_most_recent_update_tracks_delta(fake_snapshot):
    mgr = OrderBookManager()
    mgr.apply_snapshot("TICK-A", fake_snapshot)
    ts = mgr.most_recent_update()
    assert ts > 0.0
```

Where `fake_snapshot` is whatever fixture already exists in that test file (check the existing test patterns).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_orderbook.py::test_most_recent_update_no_books -v`
Expected: FAIL — `most_recent_update` doesn't exist.

- [ ] **Step 3: Implement most_recent_update() on OrderBookManager**

Add to the `OrderBookManager` class in `src/talos/orderbook.py`:

```python
    def most_recent_update(self) -> float:
        """Epoch timestamp of the most recently updated book, or 0.0 if no books."""
        if not self._books:
            return 0.0
        return max(book.last_update for book in self._books.values())
```

- [ ] **Step 4: Run orderbook tests**

Run: `.venv/Scripts/python -m pytest tests/test_orderbook.py -v`
Expected: All pass.

- [ ] **Step 5: Write test for engine staleness query**

Create `tests/test_data_staleness.py`:

```python
"""Tests for data staleness query on TradingEngine."""

import time
from unittest.mock import MagicMock

from talos.orderbook import OrderBookManager


def test_seconds_since_last_book_update_no_books():
    mgr = OrderBookManager()
    # No books → should report a large number (stale)
    assert mgr.most_recent_update() == 0.0


def test_seconds_since_last_book_update_fresh():
    mgr = OrderBookManager()
    # Simulate a book that was just updated
    from talos.models.ws import OrderBookSnapshot
    snap = OrderBookSnapshot(
        market_ticker="TEST-TICK",
        yes=[[50, 100]],
        no=[[50, 100]],
    )
    mgr.apply_snapshot("TEST-TICK", snap)
    ts = mgr.most_recent_update()
    assert time.time() - ts < 2.0  # updated within last 2 seconds
```

- [ ] **Step 6: Add seconds_since_last_book_update() to TradingEngine**

Add to `src/talos/engine.py` in the `TradingEngine` class, near the existing `ws_connected` property:

```python
    def seconds_since_last_book_update(self) -> float:
        """Seconds since any orderbook received a delta. Used for UI data health."""
        last = self._books.most_recent_update()
        if last <= 0.0:
            return float("inf")
        return time.time() - last
```

Add `import time` at the top of engine.py if not already present.

- [ ] **Step 7: Run staleness tests**

Run: `.venv/Scripts/python -m pytest tests/test_data_staleness.py -v`
Expected: All pass.

- [ ] **Step 8: Commit**

```bash
git add src/talos/orderbook.py src/talos/engine.py tests/test_orderbook.py tests/test_data_staleness.py
git commit -m "feat: add data staleness query for UI health indicator

OrderBookManager.most_recent_update() returns latest book timestamp.
TradingEngine.seconds_since_last_book_update() computes elapsed seconds.
UI threshold (60s) is intentionally lower than recovery threshold (120s)."
```

---

### Task 4: Update TalosApp for ExecutionMode + structured status bar

Wire the new `ExecutionMode` into the app: startup from settings, structured sub_title, `F` key toggle, shared session-end path.

**Files:**
- Modify: `src/talos/ui/app.py`
- Modify: `src/talos/ui/screens.py` (AutoAcceptScreen modal)

- [ ] **Step 1: Update imports in app.py**

Change:
```python
from talos.auto_accept import AutoAcceptState
```
to:
```python
from talos.auto_accept import ExecutionMode, Mode
```

- [ ] **Step 2: Update __init__ state**

In `TalosApp.__init__`, change:
```python
        self._auto_accept = AutoAcceptState()
```
to:
```python
        self._execution_mode = ExecutionMode()
```

- [ ] **Step 3: Update on_mount — replace hardcoded auto-accept with settings-driven startup**

In `on_mount`, remove:
```python
            # Auto-accept on by default (24h), press F to toggle off
            self._start_auto_accept(168.0)
```

Replace with:
```python
            # Boot into configured execution mode (startup defaults from settings.json)
            startup_mode = getattr(self._engine, '_startup_execution_mode', 'automatic')
            startup_hours = getattr(self._engine, '_startup_auto_stop_hours', None)
            if startup_mode == 'automatic':
                self._enter_automatic_mode(hours=startup_hours)
            else:
                self._execution_mode.enter_manual()
```

Note: `_startup_execution_mode` and `_startup_auto_stop_hours` are set on the engine in `__main__.py` (Task 5). Using `getattr` with defaults so tests that don't set these still work.

- [ ] **Step 4: Replace _start_auto_accept with _enter_automatic_mode**

Replace the `_start_auto_accept` method (lines 1015-1041) with:

```python
    def _enter_automatic_mode(self, hours: float | None = None) -> None:
        """Enter automatic execution mode. hours=None means indefinite."""
        if self._engine is None:
            return

        from talos.persistence import get_data_dir

        self._execution_mode.enter_automatic(hours=hours)

        log_dir = get_data_dir() / "auto_accept_sessions"
        aa_logger = AutoAcceptLogger(log_dir)
        self._auto_accept_logger = aa_logger

        cfg = self._engine.automation_config
        config: dict[str, object] = {
            "edge_threshold_cents": cfg.edge_threshold_cents,
            "stability_seconds": cfg.stability_seconds,
            "unit_size": self._engine.unit_size,
        }
        aa_logger.log_session_start(self._execution_mode, config)

        label = f"Automatic mode ON" + (f" — {hours:.1f}h" if hours else " — indefinite")
        self.notify(label, severity="warning", markup=False)
        logger.info("execution_mode_automatic", hours=hours)
```

- [ ] **Step 5: Replace _stop_auto_accept with _end_automatic_session**

Replace the `_stop_auto_accept` method (lines 1043-1066) with:

```python
    def _end_automatic_session(self) -> None:
        """End automatic session: log final state, switch to manual."""
        count = self._execution_mode.accepted_count
        elapsed = self._execution_mode.elapsed_str()

        if self._auto_accept_logger and self._engine:
            final_positions: dict[str, object] = {}
            for s in self._engine.position_summaries:
                final_positions[s.event_ticker] = {
                    "status": s.status,
                    "leg_a_filled": s.leg_a.filled_count,
                    "leg_b_filled": s.leg_b.filled_count,
                }
            self._auto_accept_logger.log_session_end(self._execution_mode, final_positions)

        self._execution_mode.enter_manual()
        self._auto_accept_logger = None

        self.notify(
            f"Manual mode — {count} accepted in {elapsed}",
            severity="information",
            markup=False,
        )
        logger.info("execution_mode_manual", accepted_count=count, elapsed=elapsed)
```

- [ ] **Step 6: Update action_toggle_auto_accept**

Replace:
```python
    def action_toggle_auto_accept(self) -> None:
        if self._engine is None:
            return
        if self._auto_accept.active:
            self._stop_auto_accept()
        else:
            self._open_auto_accept()
```

With:
```python
    def action_toggle_auto_accept(self) -> None:
        if self._engine is None:
            return
        if self._execution_mode.is_automatic:
            self._end_automatic_session()
        else:
            self._open_auto_accept()
```

- [ ] **Step 7: Update _open_auto_accept**

Replace:
```python
    @work(thread=False, exclusive=True, group="auto_accept")
    async def _open_auto_accept(self) -> None:
        hours = await self.push_screen_wait(AutoAcceptScreen())
        if hours is not None and self._engine is not None:
            self._start_auto_accept(hours)
```

With:
```python
    @work(thread=False, exclusive=True, group="auto_accept")
    async def _open_auto_accept(self) -> None:
        hours = await self.push_screen_wait(AutoAcceptScreen())
        if hours is not None and self._engine is not None:
            self._enter_automatic_mode(hours=hours if hours > 0 else None)
```

Note: `hours=0` (or blank) from modal means indefinite → pass `None`.

- [ ] **Step 8: Update _auto_accept_tick**

Replace all `self._auto_accept` references with `self._execution_mode`:

```python
    @work(thread=False)
    async def _auto_accept_tick(self) -> None:
        """Each second: if automatic mode, approve the oldest pending proposal."""
        if not self._execution_mode.is_automatic or self._engine is None:
            return

        if self._execution_mode.is_expired():
            self._end_automatic_session()
            return

        # Rate limit backoff — skip ticks until cooldown expires
        if self._rate_limit_until is not None:
            if datetime.now(UTC) < self._rate_limit_until:
                return
            self._rate_limit_until = None

        pending = self._engine.proposal_queue.pending()
        if not pending:
            return

        actionable = [p for p in pending if p.kind != "hold"]
        if not actionable:
            return

        proposal = actionable[0]
        snapshot = self._capture_state_snapshot()
        try:
            await self._engine.approve_proposal(proposal.key)
            self._execution_mode.accepted_count += 1
            if self._auto_accept_logger:
                self._auto_accept_logger.log_accepted(proposal, snapshot, self._execution_mode)
        except KalshiRateLimitError as e:
            backoff = max(e.retry_after or 2.0, 2.0)
            self._rate_limit_until = datetime.now(UTC) + timedelta(seconds=backoff)
            logger.info("auto_accept_rate_limited", backoff_s=backoff)
        except Exception as e:
            logger.exception("auto_accept_error", proposal_key=str(proposal.key))
            if self._auto_accept_logger:
                self._auto_accept_logger.log_error(proposal, str(e), snapshot, self._execution_mode)
```

- [ ] **Step 9: Update _refresh_proposals — structured status bar**

Replace the current `_refresh_proposals` sub_title logic with:

```python
    def _refresh_proposals(self) -> None:
        """Update subtitle with structured status bar and refresh proposal panel."""
        # WS disconnect banner (visual alarm, separate from status bar)
        banner = self.query_one("#ws-disconnect-banner", Static)
        ws_dead = self._engine is not None and not self._engine.ws_connected
        if ws_dead:
            banner.add_class("visible")
        else:
            banner.remove_class("visible")

        # Structured status bar: SCAN_MODE | MODE: X | DATA: X | count
        mode_tag = "SPORTS" if self._scan_mode == "sports" else "NON-SPORTS"
        parts: list[str] = [mode_tag]

        # Execution mode
        if self._execution_mode.is_automatic:
            mode_str = "MODE: AUTO"
            remaining = self._execution_mode.remaining_str()
            if remaining:
                mode_str += f" {remaining} left"
            parts.append(mode_str)
        else:
            parts.append("MODE: MANUAL")

        # Data health
        parts.append("DATA: STALE" if self._is_data_stale() else "DATA: LIVE")

        # Accepted count (automatic mode only)
        if self._execution_mode.is_automatic:
            parts.append(f"{self._execution_mode.accepted_count} accepted")

        self.sub_title = " | ".join(parts)

        # Refresh the proposal panel if it's visible
        try:
            panel = self.query_one("#proposal-panel", ProposalPanel)
            if panel.display:
                panel.refresh_proposals()
        except Exception:
            pass

    def _is_data_stale(self) -> bool:
        """True if orderbook data is not fresh. 60s threshold — warns before
        the 120s recovery threshold in orderbook.py kicks in."""
        if self._engine is None:
            return True
        if not self._engine.ws_connected:
            return True
        return self._engine.seconds_since_last_book_update() > 60.0
```

- [ ] **Step 10: Update AutoAcceptScreen modal**

In `src/talos/ui/screens.py`, update `AutoAcceptScreen` to support indefinite mode:

```python
class AutoAcceptScreen(ModalScreen[float | None]):
    """Modal for entering automatic mode duration. 0 or blank = indefinite."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("Automatic Mode", classes="modal-title")
            yield Label("Hours until auto-stop (blank = indefinite):")
            yield Input(
                value="",
                placeholder="indefinite",
                id="hours-input",
                type="text",
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
            raw = hours_input.value.strip()
            if raw == "":
                self.dismiss(0.0)  # 0 = indefinite
                return
            try:
                hours = float(raw)
            except ValueError:
                self.query_one("#modal-error", Label).update("Enter a number or leave blank")
                return
            if hours < 0:
                self.query_one("#modal-error", Label).update("Duration cannot be negative")
                return
            self.dismiss(hours)
```

Note: returns `0.0` for indefinite, positive float for timed. The caller (`_open_auto_accept`) maps `0` → `None` (indefinite).

- [ ] **Step 11: Grep for any remaining AutoAcceptState / _auto_accept references in app.py**

Run: `grep -n "AutoAcceptState\|_auto_accept\b" src/talos/ui/app.py`

Fix any remaining references:
- `self._auto_accept` → `self._execution_mode` (wherever not already changed)
- `self._auto_accept.active` → `self._execution_mode.is_automatic`
- `self._auto_accept.accepted_count` → `self._execution_mode.accepted_count`
- `self._auto_accept.remaining_str()` → `self._execution_mode.remaining_str()`
- Keep `self._auto_accept_logger` as-is (logger name doesn't change per spec)

- [ ] **Step 12: Commit**

```bash
git add src/talos/ui/app.py src/talos/ui/screens.py
git commit -m "feat: wire ExecutionMode into TUI with structured status bar

- Startup from settings instead of hardcoded 168h auto-accept
- Status bar: SCAN_MODE | MODE: AUTO/MANUAL | DATA: LIVE/STALE | count
- Shared _end_automatic_session() for F-key stop and timer expiry
- AutoAcceptScreen supports indefinite (blank input)"
```

---

### Task 5: Wire startup defaults from settings.json

Read `execution_mode` and `auto_stop_hours` from settings and pass them to the app via the engine.

**Files:**
- Modify: `src/talos/__main__.py`

- [ ] **Step 1: Add startup default passthrough**

In `src/talos/__main__.py`, after `settings = load_settings()` (line 226) and before engine creation, read the new keys:

```python
    startup_execution_mode = str(settings.get("execution_mode", "automatic"))
    startup_auto_stop_hours = settings.get("auto_stop_hours", None)
    if startup_auto_stop_hours is not None:
        startup_auto_stop_hours = float(startup_auto_stop_hours)
```

After engine creation (after line 333), attach the startup defaults to the engine for the app to read:

```python
    engine._startup_execution_mode = startup_execution_mode  # type: ignore[attr-defined]
    engine._startup_auto_stop_hours = startup_auto_stop_hours  # type: ignore[attr-defined]
```

This uses ad-hoc attributes rather than adding formal parameters to TradingEngine — execution mode is a UI concern, not an engine concern. The `type: ignore` comments acknowledge this is intentional passthrough, not a formal API.

- [ ] **Step 2: Verify no runtime errors**

Run: `.venv/Scripts/python -m pytest tests/test_engine.py -x -q`
Expected: Pass (engine tests don't depend on these attributes).

- [ ] **Step 3: Commit**

```bash
git add src/talos/__main__.py
git commit -m "feat: read execution mode startup defaults from settings.json

Reads execution_mode and auto_stop_hours from settings.json and
passes to TalosApp via engine attributes. Factory default: automatic."
```

---

### Task 6: Update UI tests

Update existing tests that reference `AutoAcceptState` or `_auto_accept`.

**Files:**
- Modify: `tests/test_ui.py` (if it references auto_accept)
- Modify: any other test files found by grep

- [ ] **Step 1: Find all test references**

Run: `grep -rn "AutoAcceptState\|_auto_accept\|auto_accept\.active" tests/`

- [ ] **Step 2: Update each reference**

For every file found:
- `AutoAcceptState` → `ExecutionMode`
- `from talos.auto_accept import AutoAcceptState` → `from talos.auto_accept import ExecutionMode, Mode`
- `.active` → `.is_automatic`
- `.start(hours=X)` → `.enter_automatic(hours=X)`
- `.stop()` → `.enter_manual()`

- [ ] **Step 3: Run full test suite**

Run: `.venv/Scripts/python -m pytest -x -q`
Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test: update all tests for ExecutionMode API"
```

---

### Task 7: Final verification

- [ ] **Step 1: Run full test suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: All pass.

- [ ] **Step 2: Run lint + type check**

Run: `.venv/Scripts/python -m ruff check src/ tests/`
Run: `.venv/Scripts/python -m pyright`
Expected: Clean (or only pre-existing pyright noise from talos.* imports).

- [ ] **Step 3: Grep for any remaining AutoAcceptState references in src/**

Run: `grep -rn "AutoAcceptState" src/`
Expected: Zero hits.

- [ ] **Step 4: Grep for hardcoded 168**

Run: `grep -rn "168" src/talos/`
Expected: Zero hits related to auto-accept duration.

- [ ] **Step 5: Verify status bar format manually (if running TUI)**

If you can launch the TUI, check:
- Status bar shows `MODE: AUTO` or `MODE: MANUAL`
- Status bar shows `DATA: LIVE` or `DATA: STALE`
- Press `F` to toggle between modes
- WS disconnect banner and status bar coexist

- [ ] **Step 6: Commit any cleanup**

```bash
git add -A
git commit -m "chore: final cleanup for execution mode governance"
```
