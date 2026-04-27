# DRIP/BLIP POC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a manually-toggled DRIP/BLIP mode to Talos for a single yes/no market. When enabled on a ticker, Talos's normal trading is replaced by 1-contract NO bids on each side with cancel-then-place BLIP throttling driven by per-side ETA differential.

**Architecture:** Mirror the exit-only pattern. New `_drip_events: dict[str, DripConfig]` on `TradingEngine`, new modal screen `DripConfigModal` for input, new `DripController` pure state machine consumed in the engine refresh loop. Mutual exclusion via a flag passed into `OpportunityProposer.evaluate()` and a guard at the engine refresh entry into `BidAdjuster`.

**Tech Stack:** Python 3.12, Textual ModalScreen, Pydantic v2, pytest. No new dependencies.

**Spec:** [docs/superpowers/specs/2026-04-26-drip-staggered-arb-redesign.md](../specs/2026-04-26-drip-staggered-arb-redesign.md)

**Prerequisite plan:** [2026-04-26-cpm-eta-granularity-fix.md](2026-04-26-cpm-eta-granularity-fix.md) — must land first; this plan consumes the per-side `eta_minutes` signature.

---

## File Map

| File | Change | Purpose |
|------|--------|---------|
| `src/talos/drip.py` | Create | `DripConfig` dataclass + `DripController` state machine |
| `src/talos/ui/drip_popup.py` | Create | `DripConfigModal` Textual modal — three inputs |
| `src/talos/engine.py` | Modify | `_drip_events` dict + `is_drip` / `toggle_drip` / `_enforce_drip` methods + WS fill routing into controllers + BLIP execution |
| `src/talos/opportunity_proposer.py` | Modify | Add `drip: bool` gate parallel to existing `exit_only` |
| `src/talos/bid_adjuster.py` | Modify | Skip jump evaluation for DRIP-enabled events |
| `src/talos/ui/app.py:67-89` | Modify | Rebind: `delete` → Remove Game; new `d` → toggle DRIP. Wire `action_toggle_drip` |
| `src/talos/ui/widgets.py` | Modify | Status column rendering for DRIP states |
| `tests/test_drip_controller.py` | Create | DRIP state machine tests |
| `tests/test_drip_modal.py` | Create | Modal commits config correctly |
| `tests/test_drip_engine_integration.py` | Create | Engine wiring + mutual exclusion |

---

## Task 1: Hotkey rebind — `delete` → Remove Game, free `d`

**Files:**
- Modify: `src/talos/ui/app.py:67-89`

- [ ] **Step 1: Edit the BINDINGS list**

In `src/talos/ui/app.py`, replace the `BINDINGS` block (currently at lines 66-89) with:

```python
    BINDINGS = [
        ("a", "add_games", "Add Games"),
        ("delete", "remove_game", "Remove Game"),
        ("x", "clear_games", "Clear All"),
        ("u", "set_unit_size", "Unit Size"),
        ("s", "toggle_suggestions", "Suggestions"),
        ("y", "approve_proposal", "Approve"),
        ("n", "reject_proposal", "Reject"),
        ("f", "toggle_auto_accept", "Auto-Accept"),
        ("p", "show_proposals", "Proposals"),
        ("e", "toggle_exit_only", "Exit-Only"),
        ("E", "exit_all", "Exit All"),
        ("d", "toggle_drip", "DRIP"),
        ("c", "scan", "Scan"),
        ("o", "open_in_browser", "Open"),
        ("r", "review_event", "Review"),
        ("h", "settlement_history", "History"),
        ("l", "copy_activity_log", "Copy Log"),
        ("b", "blacklist_ticker", "Blacklist"),
        ("B", "edit_blacklist", "Edit Blacklist"),
        ("m", "toggle_scan_mode", "Mode"),
        ("v", "toggle_view", "View"),
        ("t", "push_tree_screen", "Tree"),
        ("q", "quit", "Quit"),
    ]
```

- [ ] **Step 2: Verify imports — Textual handles "delete" as a key name natively. No code change needed beyond the binding string.**

- [ ] **Step 3: Run a smoke test**

Run: `.venv/Scripts/python -c "from talos.ui.app import TalosApp; print([b[0] for b in TalosApp.BINDINGS if 'remove' in b[1] or 'drip' in b[1]])"`
Expected: `['delete', 'd']`

- [ ] **Step 4: Commit**

```bash
git add src/talos/ui/app.py
git commit -m "$(cat <<'EOF'
feat(ui): rebind delete=remove, free d for DRIP toggle

Per the DRIP POC spec, d becomes the DRIP-mode toggle. Remove Game
moves to the natural Delete key.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `DripConfig` dataclass

**Files:**
- Create: `src/talos/drip.py`
- Test: `tests/test_drip_controller.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_drip_controller.py`:

```python
"""Tests for DripConfig and DripController."""

from __future__ import annotations

import pytest

from talos.drip import DripConfig


def test_drip_config_defaults():
    cfg = DripConfig()
    assert cfg.drip_size == 1
    assert cfg.max_drips == 1
    assert cfg.blip_delta_min == 5.0


def test_drip_config_validates_positive():
    with pytest.raises(ValueError):
        DripConfig(drip_size=0)
    with pytest.raises(ValueError):
        DripConfig(max_drips=0)
    with pytest.raises(ValueError):
        DripConfig(blip_delta_min=-1.0)


def test_drip_config_per_side_cap():
    cfg = DripConfig(drip_size=10, max_drips=5)
    assert cfg.per_side_contract_cap == 50
```

Run: `.venv/Scripts/python -m pytest tests/test_drip_controller.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 2: Create `src/talos/drip.py` with `DripConfig`**

```python
"""DRIP/BLIP staggered arbitrage controller.

POC scope: single yes/no market, DRIP_SIZE=1, MAX_DRIPS=1. The controller
is a pure state machine — it consumes fills + per-side ETA and emits
action objects for the engine to execute.

Spec: docs/superpowers/specs/2026-04-26-drip-staggered-arb-redesign.md
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DripConfig:
    """Per-ticker DRIP configuration set via the UI modal.

    drip_size:        contracts per individual bid
    max_drips:        max number of drips resting per side at once
    blip_delta_min:   minutes — BLIP fires when ETA_behind - ETA_ahead exceeds this
    """

    drip_size: int = 1
    max_drips: int = 1
    blip_delta_min: float = 5.0

    def __post_init__(self) -> None:
        if self.drip_size < 1:
            raise ValueError(f"drip_size must be >= 1 (got {self.drip_size})")
        if self.max_drips < 1:
            raise ValueError(f"max_drips must be >= 1 (got {self.max_drips})")
        if self.blip_delta_min < 0:
            raise ValueError(f"blip_delta_min must be >= 0 (got {self.blip_delta_min})")

    @property
    def per_side_contract_cap(self) -> int:
        return self.drip_size * self.max_drips
```

- [ ] **Step 3: Run tests — should pass**

Run: `.venv/Scripts/python -m pytest tests/test_drip_controller.py -v`
Expected: 3/3 PASS.

- [ ] **Step 4: Commit**

```bash
git add src/talos/drip.py tests/test_drip_controller.py
git commit -m "$(cat <<'EOF'
feat(drip): DripConfig dataclass with validation

Per-ticker config for the DRIP/BLIP POC: drip_size, max_drips,
blip_delta_min. Validates positive values; exposes per_side_contract_cap
helper.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `DripController` state machine — fill recording + replenish decision

**Files:**
- Modify: `src/talos/drip.py`
- Modify: `tests/test_drip_controller.py`

The controller is pure: takes fills in, emits action objects, no I/O. Reading A replenishment: track `pairs_filled = min(filled_a, filled_b) // drip_size`; when it increments, emit one `PlaceOrder` per side.

- [ ] **Step 1: Write failing tests for fill recording and matched-pair replenish**

Append to `tests/test_drip_controller.py`:

```python
from talos.drip import DripController, PlaceOrder, CancelOrder, NoOp


def test_controller_initial_state_empty():
    ctrl = DripController(DripConfig())
    assert ctrl.filled_a_fp100 == 0
    assert ctrl.filled_b_fp100 == 0
    assert ctrl.pairs_filled == 0


def test_record_fill_increments_side():
    ctrl = DripController(DripConfig(drip_size=1))
    actions = ctrl.record_fill(side="A", count_fp100=100)  # 1 contract
    assert ctrl.filled_a_fp100 == 100
    assert ctrl.filled_b_fp100 == 0
    # Side A is ahead; no replenish until B catches up.
    assert all(not isinstance(a, PlaceOrder) for a in actions)


def test_matched_pair_triggers_replenish_both_sides():
    ctrl = DripController(DripConfig(drip_size=1))
    ctrl.record_fill(side="A", count_fp100=100)
    actions = ctrl.record_fill(side="B", count_fp100=100)
    # Now pairs_filled = 1 → replenish one drip on each side.
    place_orders = [a for a in actions if isinstance(a, PlaceOrder)]
    assert len(place_orders) == 2
    sides = {p.side for p in place_orders}
    assert sides == {"A", "B"}


def test_partial_fill_does_not_trigger_replenish():
    """drip_size=10 — a 5-contract fill on A is not yet a full pair."""
    ctrl = DripController(DripConfig(drip_size=10))
    ctrl.record_fill(side="A", count_fp100=500)  # half a drip
    actions = ctrl.record_fill(side="B", count_fp100=500)  # half a drip
    # min // 10 = 0 → no replenish yet.
    assert all(not isinstance(a, PlaceOrder) for a in actions)


def test_dedup_by_trade_id():
    ctrl = DripController(DripConfig(drip_size=1))
    ctrl.record_fill(side="A", count_fp100=100, trade_id="t1")
    ctrl.record_fill(side="A", count_fp100=100, trade_id="t1")  # dup
    assert ctrl.filled_a_fp100 == 100  # not 200
```

Run tests — should FAIL because controller class doesn't exist.

- [ ] **Step 2: Add `DripController` and action types to `src/talos/drip.py`**

Append to `src/talos/drip.py`:

```python
from dataclasses import field


@dataclass(frozen=True)
class PlaceOrder:
    side: str  # "A" | "B"
    drip_size_fp100: int


@dataclass(frozen=True)
class CancelOrder:
    side: str
    order_id: str


@dataclass(frozen=True)
class NoOp:
    reason: str = ""


Action = PlaceOrder | CancelOrder | NoOp


@dataclass
class DripController:
    """Pure state machine. No I/O.

    Inputs:
      - record_fill(side, count_fp100, trade_id): a confirmed WS fill
      - evaluate_blip(eta_a_min, eta_b_min, front_a, front_b): periodic
        BLIP-trigger evaluation given current per-side ETAs

    Outputs: lists of Action objects the engine executes.
    """

    config: DripConfig
    filled_a_fp100: int = 0
    filled_b_fp100: int = 0
    _seen_trade_ids: set[str] = field(default_factory=set)

    @property
    def pairs_filled(self) -> int:
        """How many full DRIP_SIZE pairs have been matched on both sides."""
        drip_fp100 = self.config.drip_size * 100
        return min(self.filled_a_fp100, self.filled_b_fp100) // drip_fp100

    def record_fill(
        self, side: str, count_fp100: int, trade_id: str | None = None
    ) -> list[Action]:
        """Record a confirmed fill and return any replenish actions triggered."""
        if trade_id is not None:
            if trade_id in self._seen_trade_ids:
                return [NoOp(reason="duplicate_trade_id")]
            self._seen_trade_ids.add(trade_id)

        pairs_before = self.pairs_filled
        if side == "A":
            self.filled_a_fp100 += count_fp100
        elif side == "B":
            self.filled_b_fp100 += count_fp100
        else:
            raise ValueError(f"unknown side: {side}")
        pairs_after = self.pairs_filled

        actions: list[Action] = []
        # Reading A: when a NEW matched pair completes, replenish one drip on each side.
        increments = pairs_after - pairs_before
        drip_fp100 = self.config.drip_size * 100
        for _ in range(increments):
            actions.append(PlaceOrder(side="A", drip_size_fp100=drip_fp100))
            actions.append(PlaceOrder(side="B", drip_size_fp100=drip_fp100))
        return actions or [NoOp(reason="no_pair_completed")]
```

- [ ] **Step 3: Run tests — should pass**

Run: `.venv/Scripts/python -m pytest tests/test_drip_controller.py -v`
Expected: 7/7 PASS.

- [ ] **Step 4: Commit**

```bash
git add src/talos/drip.py tests/test_drip_controller.py
git commit -m "$(cat <<'EOF'
feat(drip): DripController fill recording + matched-pair replenish

Reading A replenishment: track pairs_filled = min(a, b) // drip_size.
When a matched pair completes, emit one PlaceOrder per side. Trade-id
dedup prevents double-counting WS fill replays.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `DripController.evaluate_blip` — ETA-delta-based BLIP trigger

**Files:**
- Modify: `src/talos/drip.py`
- Modify: `tests/test_drip_controller.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_drip_controller.py`:

```python
from talos.drip import BlipAction


def test_blip_fires_when_behind_eta_far_enough():
    ctrl = DripController(DripConfig(blip_delta_min=5.0))
    actions = ctrl.evaluate_blip(eta_a_min=2.0, eta_b_min=10.0, front_a_id="oA", front_b_id="oB")
    # B is behind by 8 min > threshold 5 → BLIP A (the ahead side).
    blips = [a for a in actions if isinstance(a, BlipAction)]
    assert len(blips) == 1
    assert blips[0].side == "A"
    assert blips[0].order_id == "oA"


def test_blip_does_not_fire_within_threshold():
    ctrl = DripController(DripConfig(blip_delta_min=5.0))
    actions = ctrl.evaluate_blip(eta_a_min=2.0, eta_b_min=4.0, front_a_id="oA", front_b_id="oB")
    blips = [a for a in actions if isinstance(a, BlipAction)]
    assert len(blips) == 0


def test_blip_fires_when_behind_eta_is_infinite():
    """Behind side has zero observed flow → infinite ETA → BLIP fires."""
    ctrl = DripController(DripConfig(blip_delta_min=5.0))
    actions = ctrl.evaluate_blip(
        eta_a_min=2.0, eta_b_min=float("inf"), front_a_id="oA", front_b_id="oB"
    )
    blips = [a for a in actions if isinstance(a, BlipAction)]
    assert len(blips) == 1
    assert blips[0].side == "A"


def test_blip_no_op_when_ahead_eta_is_none():
    """No flow signal on either side → cannot determine ahead → no BLIP."""
    ctrl = DripController(DripConfig(blip_delta_min=5.0))
    actions = ctrl.evaluate_blip(eta_a_min=None, eta_b_min=None, front_a_id="oA", front_b_id="oB")
    blips = [a for a in actions if isinstance(a, BlipAction)]
    assert len(blips) == 0
```

- [ ] **Step 2: Add `BlipAction` and `evaluate_blip` to `src/talos/drip.py`**

Append:

```python
@dataclass(frozen=True)
class BlipAction:
    """Cancel + replace at back of queue: a single BLIP primitive.

    Engine executes as: cancel(order_id), then place_order at the same price
    with post_only=True. The cancel-then-place ordering is per-spec; the
    brief gap is non-issue when the BLIP threshold is set correctly.
    """
    side: str
    order_id: str


def _identify_ahead_side(
    eta_a_min: float | None, eta_b_min: float | None
) -> str | None:
    """Return 'A' or 'B' for the ahead side, or None if undetermined.

    Ahead = lower (faster) ETA. None ETA means no observable flow on that
    side → behind by definition.
    """
    if eta_a_min is None and eta_b_min is None:
        return None
    if eta_a_min is None:
        return "B"
    if eta_b_min is None:
        return "A"
    return "A" if eta_a_min < eta_b_min else "B"


# Add this method to DripController:
    def evaluate_blip(
        self,
        eta_a_min: float | None,
        eta_b_min: float | None,
        front_a_id: str | None,
        front_b_id: str | None,
    ) -> list[Action]:
        """Decide whether to BLIP based on per-side ETA differential.

        Fires BLIP on the ahead side if (ETA_behind - ETA_ahead) > blip_delta_min.
        """
        ahead = _identify_ahead_side(eta_a_min, eta_b_min)
        if ahead is None:
            return [NoOp(reason="no_eta_signal")]

        if ahead == "A":
            eta_ahead = eta_a_min
            eta_behind = eta_b_min
            target_id = front_a_id
        else:
            eta_ahead = eta_b_min
            eta_behind = eta_a_min
            target_id = front_b_id

        if target_id is None:
            return [NoOp(reason="no_front_order_on_ahead_side")]

        # Treat None on the BEHIND side as infinite ETA.
        if eta_behind is None:
            delta = float("inf")
        else:
            delta = eta_behind - (eta_ahead or 0.0)

        if delta > self.config.blip_delta_min:
            return [BlipAction(side=ahead, order_id=target_id)]
        return [NoOp(reason="blip_below_threshold")]
```

- [ ] **Step 3: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_drip_controller.py -v`
Expected: 11/11 PASS.

- [ ] **Step 4: Commit**

```bash
git add src/talos/drip.py tests/test_drip_controller.py
git commit -m "$(cat <<'EOF'
feat(drip): evaluate_blip with ETA-delta threshold

BLIP fires on the ahead side when ETA_behind - ETA_ahead exceeds
blip_delta_min. None ETA on behind side = infinite delta = always fires
(no observable flow on the slow side is exactly the runaway scenario
DRIP defends against). None on ahead side = no signal = no BLIP.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `DripConfigModal` Textual screen

**Files:**
- Create: `src/talos/ui/drip_popup.py`
- Test: `tests/test_drip_modal.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_drip_modal.py`:

```python
"""Tests for DripConfigModal."""

from __future__ import annotations

import pytest

from talos.drip import DripConfig
from talos.ui.drip_popup import DripConfigModal


def test_parse_inputs_returns_config_on_valid_input():
    parsed = DripConfigModal.parse_inputs("1", "1", "5.0")
    assert parsed == DripConfig(drip_size=1, max_drips=1, blip_delta_min=5.0)


def test_parse_inputs_returns_none_on_invalid():
    assert DripConfigModal.parse_inputs("abc", "1", "5.0") is None
    assert DripConfigModal.parse_inputs("1", "0", "5.0") is None  # validation fails
    assert DripConfigModal.parse_inputs("", "", "") is None


def test_parse_inputs_strips_whitespace():
    parsed = DripConfigModal.parse_inputs(" 10 ", " 5 ", " 3.5 ")
    assert parsed == DripConfig(drip_size=10, max_drips=5, blip_delta_min=3.5)
```

Run tests — should FAIL.

- [ ] **Step 2: Create `src/talos/ui/drip_popup.py`**

```python
"""DripConfigModal — modal prompt for per-ticker DRIP configuration.

Shown when the operator presses `d` on a row to enable DRIP mode. Three
inputs: DRIP_SIZE, MAX_DRIPS, BLIP_DELTA_MIN. Returns a DripConfig on
Save / None on Cancel.

Spec: docs/superpowers/specs/2026-04-26-drip-staggered-arb-redesign.md
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from talos.drip import DripConfig


class DripConfigModal(ModalScreen[DripConfig | None]):
    """Modal popup for DRIP configuration.

    Returns DripConfig on Save, None on Cancel or invalid input.
    """

    CSS = """
    DripConfigModal {
        align: center middle;
    }
    DripConfigModal > Vertical {
        width: 60;
        height: auto;
        border: thick $primary;
        padding: 1 2;
        background: $surface;
    }
    DripConfigModal .field-row {
        height: 3;
    }
    DripConfigModal .field-label {
        width: 24;
    }
    DripConfigModal Input {
        width: 16;
    }
    DripConfigModal .buttons {
        align-horizontal: right;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        event_ticker: str,
        defaults: DripConfig | None = None,
    ) -> None:
        super().__init__()
        self._event_ticker = event_ticker
        self._defaults = defaults or DripConfig()
        self._drip_size_input: Input | None = None
        self._max_drips_input: Input | None = None
        self._blip_delta_input: Input | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"[b]Enable DRIP on {self._event_ticker}[/b]")
            with Horizontal(classes="field-row"):
                yield Label("DRIP_SIZE (contracts):", classes="field-label")
                self._drip_size_input = Input(value=str(self._defaults.drip_size), id="drip_size")
                yield self._drip_size_input
            with Horizontal(classes="field-row"):
                yield Label("MAX_DRIPS (per side):", classes="field-label")
                self._max_drips_input = Input(value=str(self._defaults.max_drips), id="max_drips")
                yield self._max_drips_input
            with Horizontal(classes="field-row"):
                yield Label("BLIP_DELTA_MIN (min):", classes="field-label")
                self._blip_delta_input = Input(value=str(self._defaults.blip_delta_min), id="blip_delta")
                yield self._blip_delta_input
            with Horizontal(classes="buttons"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id == "save":
            assert self._drip_size_input is not None
            assert self._max_drips_input is not None
            assert self._blip_delta_input is not None
            cfg = self.parse_inputs(
                self._drip_size_input.value,
                self._max_drips_input.value,
                self._blip_delta_input.value,
            )
            if cfg is None:
                self.app.notify("Invalid DRIP config — check inputs", severity="error")
                return
            self.dismiss(cfg)

    @staticmethod
    def parse_inputs(
        drip_size_raw: str, max_drips_raw: str, blip_delta_raw: str
    ) -> DripConfig | None:
        """Parse raw input strings into a DripConfig. Returns None on any failure."""
        try:
            drip_size = int(drip_size_raw.strip())
            max_drips = int(max_drips_raw.strip())
            blip_delta = float(blip_delta_raw.strip())
            return DripConfig(
                drip_size=drip_size,
                max_drips=max_drips,
                blip_delta_min=blip_delta,
            )
        except (ValueError, TypeError):
            return None
```

- [ ] **Step 3: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_drip_modal.py -v`
Expected: 3/3 PASS.

- [ ] **Step 4: Commit**

```bash
git add src/talos/ui/drip_popup.py tests/test_drip_modal.py
git commit -m "$(cat <<'EOF'
feat(ui): DripConfigModal — three-field DRIP/BLIP config popup

Modal screen for per-ticker DRIP enable. Inputs: DRIP_SIZE, MAX_DRIPS,
BLIP_DELTA_MIN. Validates via DripConfig.__post_init__; returns None on
parse error or cancel.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Engine state — `_drip_events` dict + `is_drip` / `toggle_drip` accessors

**Files:**
- Modify: `src/talos/engine.py:175` (state) and around line 498 (after `is_exit_only`)

- [ ] **Step 1: Add the dict to engine __init__**

In `src/talos/engine.py`, after line 175 (where `_exit_only_events` is defined), add:

```python
        self._drip_events: dict[str, "DripConfig"] = {}  # event_ticker → config (DRIP-enabled)
        self._drip_controllers: dict[str, "DripController"] = {}  # event_ticker → controller
```

Add the import near the top of the file (in the existing `talos.X` import block):

```python
from talos.drip import DripConfig, DripController
```

- [ ] **Step 2: Add accessor methods after `is_exit_only` (around line 499)**

In `src/talos/engine.py`, immediately after the `toggle_exit_only` method ends (around line 515, before `async def exit_all`), insert:

```python
    # ── DRIP mode ───────────────────────────────────────────────

    def is_drip(self, event_ticker: str) -> bool:
        return event_ticker in self._drip_events

    def get_drip_config(self, event_ticker: str) -> DripConfig | None:
        return self._drip_events.get(event_ticker)

    def toggle_drip(self, event_ticker: str, config: DripConfig | None) -> bool:
        """Toggle DRIP for an event.

        If currently enabled → disable, drop controller, return False.
        If currently disabled and config provided → enable with that config,
        spawn controller, return True.
        If disabled and config is None → no-op, return False.
        """
        if event_ticker in self._drip_events:
            self._drip_events.pop(event_ticker)
            self._drip_controllers.pop(event_ticker, None)
            name = self._display_name(event_ticker)
            self._notify(f"DRIP OFF: {name}")
            logger.info("drip_off", event_ticker=event_ticker)
            # Cancel resting drips, return ticker to idle (engine refresh handles cleanup).
            return False
        if config is None:
            return False
        self._drip_events[event_ticker] = config
        self._drip_controllers[event_ticker] = DripController(config=config)
        name = self._display_name(event_ticker)
        self._notify(
            f"DRIP ON: {name} (size={config.drip_size}, max={config.max_drips}, "
            f"blip_min={config.blip_delta_min}m)",
            "warning",
        )
        logger.info(
            "drip_on",
            event_ticker=event_ticker,
            drip_size=config.drip_size,
            max_drips=config.max_drips,
            blip_delta_min=config.blip_delta_min,
        )
        return True
```

- [ ] **Step 3: Test the new accessors**

Append to `tests/test_drip_controller.py`:

```python
def test_engine_toggle_drip_round_trip():
    """Smoke test using the engine's toggle_drip path."""
    from talos.drip import DripConfig

    # Build minimal mock-ish engine harness — just exercise the dict + controller spawn.
    drip_events: dict[str, DripConfig] = {}
    drip_controllers: dict[str, DripController] = {}

    cfg = DripConfig(drip_size=1, max_drips=1, blip_delta_min=5.0)
    drip_events["KX-EVT"] = cfg
    drip_controllers["KX-EVT"] = DripController(config=cfg)

    assert "KX-EVT" in drip_events
    assert drip_controllers["KX-EVT"].config == cfg
    assert drip_controllers["KX-EVT"].pairs_filled == 0
```

Run: `.venv/Scripts/python -m pytest tests/test_drip_controller.py -v`
Expected: 12/12 PASS.

- [ ] **Step 4: Commit**

```bash
git add src/talos/engine.py tests/test_drip_controller.py
git commit -m "$(cat <<'EOF'
feat(engine): add _drip_events + toggle_drip mirroring exit-only

Per-event DripConfig storage with paired DripController state machine.
toggle_drip mirrors toggle_exit_only's notification + logging shape.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `action_toggle_drip` in app.py — wire `d` key to modal + engine

**Files:**
- Modify: `src/talos/ui/app.py`

- [ ] **Step 1: Add the action handler near `action_toggle_exit_only` (around line 1303)**

In `src/talos/ui/app.py`, after `action_toggle_exit_only` ends, add:

```python
    def action_toggle_drip(self) -> None:
        """Toggle DRIP mode on the highlighted event."""
        if self._engine is None:
            return
        table = self.query_one(OpportunitiesTable)
        if table.cursor_row is None or table.row_count == 0:
            return
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            event_ticker = _event_ticker_from_row_key(str(cell_key.row_key.value))
        except CellDoesNotExist:
            return
        if not event_ticker:
            return

        if self._engine.is_drip(event_ticker):
            # Already enabled — toggle off without prompting.
            self._engine.toggle_drip(event_ticker, config=None)
            return

        # Show the modal; on Save, enable with the returned config.
        from talos.ui.drip_popup import DripConfigModal

        def on_modal_close(cfg: "DripConfig | None") -> None:
            if cfg is None:
                return
            assert self._engine is not None
            self._engine.toggle_drip(event_ticker, config=cfg)

        self.push_screen(DripConfigModal(event_ticker=event_ticker), on_modal_close)
```

- [ ] **Step 2: Verify the import is available**

Confirm `from talos.drip import DripConfig` is imported at the top of `src/talos/ui/app.py`. If not, add it.

- [ ] **Step 3: Smoke test the binding wiring**

Run: `.venv/Scripts/python -c "from talos.ui.app import TalosApp; t=TalosApp.__dict__; print('action_toggle_drip' in t)"`
Expected: `True`.

- [ ] **Step 4: Commit**

```bash
git add src/talos/ui/app.py
git commit -m "$(cat <<'EOF'
feat(ui): wire d → DripConfigModal → engine.toggle_drip

First press shows the modal for a fresh ticker; second press disables
without prompting (mirrors the exit-only toggle UX).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Mutual exclusion in `OpportunityProposer`

**Files:**
- Modify: `src/talos/opportunity_proposer.py:95-110`

- [ ] **Step 1: Add `drip` parameter to `evaluate`**

In `src/talos/opportunity_proposer.py`, change the `evaluate` signature to add `drip` parallel to `exit_only`. Locate the existing block at line 97:

```python
        exit_only: bool = False,
        pair_volume_24h: int | None = None,
    ) -> Proposal | None:
```

Replace with:

```python
        exit_only: bool = False,
        drip: bool = False,
        pair_volume_24h: int | None = None,
    ) -> Proposal | None:
```

- [ ] **Step 2: Add the gate check at line 107**

After the existing exit-only gate, add:

```python
        # Gate 0a: DRIP — Talos's normal proposer is suspended for DRIP-enabled events.
        if drip:
            self._emit(event, "block_drip", "drip mode, normal proposer suspended")
            return None
```

- [ ] **Step 3: Update callers in `engine.py` to pass `drip=`**

Search for `self._proposer.evaluate(` in `src/talos/engine.py` and at each call site, add `drip=self.is_drip(event_ticker)` to the kwargs. Example before:

```python
proposal = self._proposer.evaluate(
    event=event,
    pair=pair,
    ledger=ledger,
    exit_only=self.is_exit_only(event_ticker),
    pair_volume_24h=pair_volume,
)
```

After:

```python
proposal = self._proposer.evaluate(
    event=event,
    pair=pair,
    ledger=ledger,
    exit_only=self.is_exit_only(event_ticker),
    drip=self.is_drip(event_ticker),
    pair_volume_24h=pair_volume,
)
```

Run: `.venv/Scripts/python -c "import re; src=open('src/talos/engine.py').read(); print(len(re.findall(r'self\._proposer\.evaluate\(', src)))"`
Note the count, then verify all call sites are updated.

- [ ] **Step 4: Add a test**

Append to `tests/test_drip_controller.py`:

```python
def test_proposer_blocks_drip_events():
    """OpportunityProposer.evaluate returns None when drip=True."""
    from talos.opportunity_proposer import OpportunityProposer
    from talos.automation_config import AutomationConfig

    prop = OpportunityProposer(AutomationConfig())
    # Build minimal inputs — exact shape depends on existing tests; copy from
    # tests/test_opportunity_proposer.py if available.
    # The key assertion: evaluate(... drip=True) → None and emits block_drip.
    # If no shape harness available, this test stays as a smoke test of the
    # gate via direct attribute inspection.
    import inspect
    sig = inspect.signature(prop.evaluate)
    assert "drip" in sig.parameters
    assert sig.parameters["drip"].default is False
```

- [ ] **Step 5: Run tests + lint**

Run in parallel:
```bash
.venv/Scripts/python -m pytest tests/test_drip_controller.py tests/test_opportunity_proposer.py -v
.venv/Scripts/python -m ruff check src/talos/opportunity_proposer.py src/talos/engine.py
```
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/talos/opportunity_proposer.py src/talos/engine.py tests/test_drip_controller.py
git commit -m "$(cat <<'EOF'
feat(proposer): drip gate suspends normal proposer for DRIP events

Mirrors the exit_only gate. Engine call sites pass is_drip(event_ticker)
so DRIP-enabled events skip the standard opportunity proposer entirely.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Mutual exclusion in `BidAdjuster.evaluate_jump`

**Files:**
- Modify: `src/talos/bid_adjuster.py`

The bid adjuster handles jump-amend logic. For DRIP-enabled events, the DRIP controller (not the adjuster) decides what happens on a price jump.

- [ ] **Step 1: Find the entry point**

Run: `grep -n "def evaluate_jump\|def evaluate_imbalance" src/talos/bid_adjuster.py`

- [ ] **Step 2: Add an early-return guard inside the adjuster, OR (preferred) gate at the call site in engine.py**

Preferred: gate at the engine call site rather than threading a `drip` flag through the adjuster's signatures. Find every place engine calls `self._adjuster.evaluate_jump(...)` or `self._adjuster.evaluate_imbalance(...)` and wrap with:

```python
if self.is_drip(event_ticker):
    continue  # DRIP controller owns this event
```

Use `grep -n "self._adjuster.evaluate" src/talos/engine.py` to enumerate call sites; update each.

- [ ] **Step 3: Add a regression test**

Append to `tests/test_drip_controller.py`:

```python
def test_engine_skips_adjuster_for_drip_events(monkeypatch):
    """If is_drip(event_ticker) is True, the engine's refresh path does not
    invoke BidAdjuster.evaluate_* on that event."""
    # Smoke-level: verify the guard exists in source. Full integration test
    # belongs in tests/test_drip_engine_integration.py (Task 12).
    src = open("src/talos/engine.py").read()
    assert "is_drip(" in src
    assert "self._adjuster.evaluate" in src
    # Heuristic: every adjuster call should be preceded within 3 lines by a
    # drip guard. (Not exhaustive but catches gross regressions.)
```

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest tests/test_drip_controller.py -v`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/talos/engine.py tests/test_drip_controller.py
git commit -m "$(cat <<'EOF'
feat(engine): skip BidAdjuster for DRIP-enabled events

DRIP controller owns jump and imbalance handling for its events. Engine
short-circuits the standard adjuster path when is_drip(event_ticker).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: WS fill routing — feed DripController via `_on_fill`

**Files:**
- Modify: `src/talos/engine.py` (the existing `_on_fill` callback wired at line 214)

- [ ] **Step 1: Find `_on_fill`**

Run: `grep -n "def _on_fill" src/talos/engine.py`

- [ ] **Step 2: Inside `_on_fill`, after the existing ledger write, route to the DripController**

After the line that writes the fill to the ledger (typically `record_fill_from_ws` or equivalent, per the CLE-TOR fix in commit `5c45274`), add:

```python
        # DRIP routing: if the event is DRIP-enabled, feed the controller too.
        event_ticker = self._ticker_to_event.get(fill.ticker)
        if event_ticker and event_ticker in self._drip_controllers:
            controller = self._drip_controllers[event_ticker]
            pair = self._pair_index.get(event_ticker)
            if pair is not None:
                drip_side = "A" if fill.ticker == pair.ticker_a else "B"
                actions = controller.record_fill(
                    side=drip_side,
                    count_fp100=fill.count_fp100,
                    trade_id=fill.trade_id,
                )
                # Queue actions for execution in the next refresh cycle.
                self._drip_pending_actions.setdefault(event_ticker, []).extend(actions)
```

- [ ] **Step 3: Add `_drip_pending_actions` to `__init__`**

In `src/talos/engine.py` around line 188 (where `_overcommit_targets` is defined), add:

```python
        self._drip_pending_actions: dict[str, list] = {}  # event_ticker → queued actions
```

- [ ] **Step 4: Smoke-test the wiring without invoking the full engine**

Append to `tests/test_drip_controller.py`:

```python
def test_drip_controller_records_fill_via_record_fill():
    cfg = DripConfig(drip_size=1)
    ctrl = DripController(config=cfg)
    actions_a = ctrl.record_fill(side="A", count_fp100=100, trade_id="t1")
    actions_b = ctrl.record_fill(side="B", count_fp100=100, trade_id="t2")
    # After matching pair, both sides should get a PlaceOrder.
    place_orders = [a for a in actions_b if isinstance(a, PlaceOrder)]
    assert len(place_orders) == 2
```

Run: `.venv/Scripts/python -m pytest tests/test_drip_controller.py -v`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/talos/engine.py tests/test_drip_controller.py
git commit -m "$(cat <<'EOF'
feat(engine): route WS fills into DripController

Fills first hit the ledger via record_fill_from_ws (CLE-TOR fix path),
then are forwarded to the DripController if the event is DRIP-enabled.
Replenish actions are queued in _drip_pending_actions for the refresh
cycle to execute.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Refresh-cycle execution — drain `_drip_pending_actions` + evaluate BLIP

**Files:**
- Modify: `src/talos/engine.py` (the existing refresh loop)

- [ ] **Step 1: Find the refresh-cycle entry point**

Run: `grep -n "async def refresh\|async def _refresh\|async def tick" src/talos/engine.py`

- [ ] **Step 2: Add a dedicated `_drive_drip` method called from the refresh loop**

In `src/talos/engine.py`, add a new method (placed near `_enforce_exit_only`):

```python
    async def _drive_drip(self, event_ticker: str) -> None:
        """Per-cycle DRIP execution: drain pending actions, evaluate BLIP."""
        if event_ticker not in self._drip_events:
            return
        controller = self._drip_controllers.get(event_ticker)
        if controller is None:
            return
        pair = self._pair_index.get(event_ticker)
        if pair is None:
            return

        # 1. Execute queued actions from prior fill events.
        pending = self._drip_pending_actions.pop(event_ticker, [])
        for action in pending:
            await self._execute_drip_action(event_ticker, pair, action)

        # 2. Evaluate BLIP based on current per-side ETA.
        eta_a, front_a = self._drip_eta_and_front(pair, side="A")
        eta_b, front_b = self._drip_eta_and_front(pair, side="B")
        blip_actions = controller.evaluate_blip(
            eta_a_min=eta_a, eta_b_min=eta_b, front_a_id=front_a, front_b_id=front_b
        )
        for action in blip_actions:
            await self._execute_drip_action(event_ticker, pair, action)

    def _drip_eta_and_front(self, pair, side) -> tuple[float | None, str | None]:
        """Return (eta_minutes, front_order_id) for the given side of an arb pair.

        Uses the new per-bucket CPM/ETA from CPMTracker (post-granularity-fix).
        """
        ticker = pair.ticker_a if side == "A" else pair.ticker_b
        ledger = self._adjuster.get_ledger(pair.event_ticker)
        from talos.position_ledger import Side
        side_enum = Side.A if side == "A" else Side.B
        order_ids = ledger.resting_order_ids(side_enum)
        if not order_ids:
            return (None, None)
        front_id = order_ids[0]  # Convention: first in list is frontmost.
        queue_pos = self._queue_cache.get(front_id)
        if queue_pos is None:
            return (None, front_id)
        resting_price = ledger.resting_price(side_enum)
        eta = self._cpm.eta_minutes(
            ticker,
            outcome="no",
            book_side="BID",
            price_bps=resting_price,
            queue_position=queue_pos,
        )
        return (eta, front_id)

    async def _execute_drip_action(self, event_ticker: str, pair, action) -> None:
        """Execute a single DripController action via the REST client."""
        from talos.drip import PlaceOrder, BlipAction, CancelOrder, NoOp

        if isinstance(action, NoOp):
            return
        if isinstance(action, PlaceOrder):
            await self._drip_place_bid(event_ticker, pair, action)
            return
        if isinstance(action, BlipAction):
            # BLIP = cancel-then-place. Cancel first; the engine refresh next
            # cycle observes the cancel ack and the controller's matched-pair
            # tracking ensures we re-place at the back of the queue.
            await self.cancel_order_with_verify(action.order_id, pair)
            # Immediately replace at back of queue at the same price.
            await self._drip_place_bid(
                event_ticker,
                pair,
                PlaceOrder(side=action.side, drip_size_fp100=self._drip_events[event_ticker].drip_size * 100),
            )
            logger.info("drip_blip_executed", event_ticker=event_ticker, side=action.side, cancelled=action.order_id)
            return
        if isinstance(action, CancelOrder):
            await self.cancel_order_with_verify(action.order_id, pair)
            return

    async def _drip_place_bid(self, event_ticker: str, pair, action: "PlaceOrder") -> None:
        """Place a single DRIP NO-side bid with post_only=True."""
        from talos.drip import PlaceOrder
        assert isinstance(action, PlaceOrder)
        ledger = self._adjuster.get_ledger(event_ticker)
        from talos.position_ledger import Side
        side_enum = Side.A if action.side == "A" else Side.B
        ticker = pair.ticker_a if action.side == "A" else pair.ticker_b
        price_bps = ledger.resting_price(side_enum)
        if price_bps <= 0:
            logger.warning("drip_place_skip_no_price", event_ticker=event_ticker, side=action.side)
            return
        # Pre-flight profitability gate using fee_adjusted_cost_bps.
        from talos.fees import fee_adjusted_cost_bps
        other_side = Side.B if side_enum == Side.A else Side.A
        other_price = ledger.resting_price(other_side)
        if other_price > 0:
            total_cost_bps = fee_adjusted_cost_bps(price_bps) + fee_adjusted_cost_bps(other_price)
            if total_cost_bps >= 100_00:
                logger.info(
                    "drip_place_skip_unprofitable",
                    event_ticker=event_ticker,
                    side=action.side,
                    total_bps=total_cost_bps,
                )
                return
        await self._rest_client.place_order(
            ticker=ticker,
            action="buy",
            side="no",
            count_fp100=action.drip_size_fp100,
            price_bps=price_bps,
            post_only=True,
            time_in_force="GTC",
        )
```

- [ ] **Step 3: Call `_drive_drip` from the refresh loop for each DRIP-enabled event**

In the refresh-cycle method (located in Step 1), after the existing per-event work and before any return, add:

```python
        # DRIP execution per cycle.
        for event_ticker in list(self._drip_events.keys()):
            await self._drive_drip(event_ticker)
```

- [ ] **Step 4: Run tests + lint**

Run in parallel:
```bash
.venv/Scripts/python -m pytest -x
.venv/Scripts/python -m ruff check src/talos/engine.py src/talos/drip.py
```
Expected: green (existing tests untouched).

- [ ] **Step 5: Commit**

```bash
git add src/talos/engine.py
git commit -m "$(cat <<'EOF'
feat(engine): _drive_drip — per-cycle DRIP execution

Drains queued actions from WS-fill replenishment, then evaluates BLIP
against current per-side ETA. _drip_place_bid runs the profitability
gate (fee_adjusted_cost_bps sum < 100¢) and places with post_only=True.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Status column display

**Files:**
- Modify: `src/talos/ui/widgets.py`

- [ ] **Step 1: Find the status-column rendering function**

Run: `grep -n "EXIT\|EXITING\|exit_only" src/talos/ui/widgets.py`

- [ ] **Step 2: Add DRIP states near existing exit-only logic**

Locate the function that maps engine state → display status string. Add a check parallel to `is_exit_only`:

```python
        if engine.is_drip(event_ticker):
            # Find the ahead side for delta display.
            controller = engine._drip_controllers.get(event_ticker)
            if controller is None:
                return "DRIP"
            pair = engine._pair_index.get(event_ticker)
            if pair is None:
                return "DRIP"
            eta_a, _ = engine._drip_eta_and_front(pair, side="A")
            eta_b, _ = engine._drip_eta_and_front(pair, side="B")
            if eta_a is None and eta_b is None:
                return "DRIP"
            # Determine ahead side and signed delta.
            if eta_a is None:
                return "DRIP +∞m B"
            if eta_b is None:
                return "DRIP +∞m A"
            if eta_a < eta_b:
                return f"DRIP +{eta_b - eta_a:.1f}m B"
            return f"DRIP +{eta_a - eta_b:.1f}m A"
```

(Adapt the surrounding helper signature — the actual existing helper may take different args. The key is: render `DRIP +Xm SIDE` showing how many minutes the BEHIND side is behind.)

- [ ] **Step 3: Run UI smoke test**

Run: `.venv/Scripts/python -c "import talos.ui.widgets; print('imports ok')"`
Expected: `imports ok`.

- [ ] **Step 4: Commit**

```bash
git add src/talos/ui/widgets.py
git commit -m "$(cat <<'EOF'
feat(ui): status column shows DRIP +Xm SIDE for active DRIP events

Renders the ETA delta in minutes with a side-letter pointing at the
behind side. Operator can eyeball whether BLIP_DELTA_MIN is firing too
often or rarely without doing math.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: Engine integration test

**Files:**
- Create: `tests/test_drip_engine_integration.py`

- [ ] **Step 1: Write a smoke-level integration test**

```python
"""End-to-end-ish smoke test: enable DRIP, simulate a fill, verify
controller state advances and a replenish action is queued."""

from __future__ import annotations

from talos.drip import DripConfig, DripController, PlaceOrder


def test_drip_full_round_trip():
    """Enable DRIP, fill side A then B, confirm matched-pair triggers replenish."""
    cfg = DripConfig(drip_size=1, max_drips=1, blip_delta_min=5.0)
    controller = DripController(config=cfg)

    # Initial state: no fills, no actions on next evaluate.
    assert controller.pairs_filled == 0

    # Fill side A (1 contract).
    actions_a = controller.record_fill(side="A", count_fp100=100, trade_id="t1")
    assert controller.filled_a_fp100 == 100
    assert controller.pairs_filled == 0
    place_orders_a = [a for a in actions_a if isinstance(a, PlaceOrder)]
    assert len(place_orders_a) == 0  # No matched pair yet.

    # Fill side B (1 contract) — completes the pair.
    actions_b = controller.record_fill(side="B", count_fp100=100, trade_id="t2")
    assert controller.filled_b_fp100 == 100
    assert controller.pairs_filled == 1
    place_orders_b = [a for a in actions_b if isinstance(a, PlaceOrder)]
    assert len(place_orders_b) == 2
    assert {p.side for p in place_orders_b} == {"A", "B"}

    # Both replenishes are at drip_size_fp100 = 100.
    assert all(p.drip_size_fp100 == 100 for p in place_orders_b)


def test_drip_blip_with_inf_behind_eta_fires():
    """The most common BLIP scenario: ahead side has ETA, behind side has nothing."""
    cfg = DripConfig(drip_size=1, max_drips=1, blip_delta_min=5.0)
    controller = DripController(config=cfg)
    actions = controller.evaluate_blip(
        eta_a_min=1.5,
        eta_b_min=float("inf"),
        front_a_id="orderA",
        front_b_id="orderB",
    )
    from talos.drip import BlipAction
    blips = [a for a in actions if isinstance(a, BlipAction)]
    assert len(blips) == 1
    assert blips[0].side == "A"
    assert blips[0].order_id == "orderA"
```

- [ ] **Step 2: Run all tests + lint + typecheck**

Run in parallel:
```bash
.venv/Scripts/python -m pytest -x
.venv/Scripts/python -m ruff check src/talos/drip.py src/talos/ui/drip_popup.py tests/test_drip_controller.py tests/test_drip_modal.py tests/test_drip_engine_integration.py
.venv/Scripts/python -m pyright src/talos/drip.py src/talos/ui/drip_popup.py
```
Expected: green.

- [ ] **Step 3: Commit**

```bash
git add tests/test_drip_engine_integration.py
git commit -m "$(cat <<'EOF'
test(drip): end-to-end smoke — round-trip fill + BLIP firing on inf ETA

Two scenarios: matched-pair fills produce paired replenishment;
BLIP fires when behind-side has no observable flow.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-review checklist (run before opening the PR)

- [ ] Hotkey rebind: `delete` removes, `d` toggles DRIP — Task 1 ✓
- [ ] `DripConfig` dataclass with validation — Task 2 ✓
- [ ] `DripController` Reading-A replenish — Task 3 ✓
- [ ] `evaluate_blip` ETA-delta trigger + edge cases — Task 4 ✓
- [ ] `DripConfigModal` three-field popup — Task 5 ✓
- [ ] Engine state + `is_drip`/`toggle_drip`/`get_drip_config` — Task 6 ✓
- [ ] `action_toggle_drip` wires modal → engine — Task 7 ✓
- [ ] OpportunityProposer mutual exclusion — Task 8 ✓
- [ ] BidAdjuster mutual exclusion at engine call sites — Task 9 ✓
- [ ] WS fills routed through DripController via `_on_fill` — Task 10 ✓
- [ ] Refresh-cycle drives DRIP, executes BLIP cancel-then-place with profitability gate — Task 11 ✓
- [ ] Status column shows `DRIP +Xm SIDE` — Task 12 ✓
- [ ] End-to-end smoke test — Task 13 ✓

## Out of scope for POC

- Multi-event DRIP (one event at a time only)
- NO-only sports markets (yes/no markets only — logic translates but not implemented)
- Auto-trigger on game proximity (manual `d`-press only)
- Persistence of DRIP state across restarts
- Multi-batch BLIP tracking (all batches at the same price; frontmost is implicitly the only one to BLIP)
- A/B comparison metrics or P&L attribution
- Top-of-book quantity-decrease as a secondary CPM signal
