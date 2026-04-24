# Scanner Tree Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `GameManager.scan_events` auto-discovery and the broken `expiration − 3h` scheduling estimator with a tree-UI-driven selection model backed by Kalshi's `/milestones` API, so the SURVIVOR-class adverse-selection failure becomes structurally impossible.

**Architecture:** Four new components (`DiscoveryService`, `MilestoneResolver`, `TreeMetadataStore`, `TreeScreen`) feed into two new `Engine` entry points (`add_pairs_from_selection`, `remove_pairs_from_selection`) that mirror today's add/remove orchestration. Persistence adds two optional fields to `games_full.json` (`source`, `engine_state`) plus a new sidecar `tree_metadata.json` for event-level metadata. All behavior is gated behind `automation_config.tree_mode: bool = False` for the initial rollout; Phase 1 lands the scaffold with the flag off.

**Tech Stack:** Python 3.12+, httpx (async), websockets, Textual (TUI), Pydantic v2, structlog, pytest + pytest-asyncio, ruff, pyright. All changes in `src/talos/` and `tests/`.

**Spec:** [docs/superpowers/specs/2026-04-16-scanner-tree-redesign-design.md](../specs/2026-04-16-scanner-tree-redesign-design.md)

---

## File Map

**Create:**
- `src/talos/discovery.py` — `DiscoveryService` + cache model classes
- `src/talos/milestones.py` — `MilestoneResolver` + `Milestone` model
- `src/talos/tree_metadata.py` — `TreeMetadataStore`
- `src/talos/models/tree.py` — `ArbPairRecord`, `RemoveOutcome`, `StagedChanges`, discovery-cache Pydantic models
- `src/talos/ui/tree_screen.py` — `TreeScreen` Textual screen
- `tests/test_discovery.py`
- `tests/test_milestones.py`
- `tests/test_tree_metadata.py`
- `tests/test_tree_commit_flow.py`
- `tests/test_legacy_writer_roundtrip.py` — Codex P1 regression gate
- `tests/test_resolver_cascade.py`
- `tests/test_tree_screen.py`

**Modify:**
- `src/talos/models/strategy.py` — `ArbPair` gains optional `source` + `engine_state` fields
- `src/talos/models/market.py` — (read-only) may need exposed fields already on `Market`/`Event`
- `src/talos/automation_config.py` — new settings (§6.1 of spec)
- `src/talos/persistence.py` — add `load_tree_metadata` / `save_tree_metadata` functions
- `src/talos/game_manager.py` — add `suppress_on_change` context manager
- `src/talos/engine.py` — add `add_pairs_from_selection` / `remove_pairs_from_selection` / resolver cascade / `ready_for_trading` gate / `engine_state` restore branching
- `src/talos/__main__.py` — update `_persist_games` to serialize `source` + `engine_state`; wire `TreeMetadataStore` + `DiscoveryService` + `MilestoneResolver` into the engine at boot
- `src/talos/ui/app.py` — keybinding to push `TreeScreen`; gate startup wiring on `ready_for_trading`
- `brain/principles.md` — add Principle "Safety over speed"

**Deferred to Phase 5 (NOT this plan):**
- Delete `GameManager.scan_events`, `DEFAULT_NONSPORTS_CATEGORIES`, `_nonsports_max_days`, hardcoded `volume_24h > 0` checks, `_expiration_fallback`. These stay in place behind the flag until Phase 2 dogfooding + Phase 3 dual-run + Phase 4 default-on validate the new paths.

---

## Development Commands (for convenience during implementation)

```bash
# Activate venv first (Windows): source .venv/Scripts/activate

# Run a single test file
.venv/Scripts/python -m pytest tests/test_discovery.py -v

# Run one test
.venv/Scripts/python -m pytest tests/test_milestones.py::TestResolverCascade::test_manual_override_wins -v

# Full test suite
.venv/Scripts/python -m pytest

# Lint + format
.venv/Scripts/python -m ruff check --fix src/ tests/
.venv/Scripts/python -m ruff format src/ tests/

# Type check
.venv/Scripts/python -m pyright
```

Run `test-runner` and `lint-check` agents in parallel before every commit per CLAUDE.md.

---

## Task 1: Extend `ArbPair` model with `source` and `engine_state`

**Files:**
- Modify: `src/talos/models/strategy.py`
- Test: `tests/test_models_strategy.py` (new or existing)

Foundation: every downstream persistence/restore path reads these fields. Without them defined on the model, later tasks can't attach them.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_models_strategy.py` (create file if absent):

```python
"""Tests for ArbPair model additions."""
from talos.models.strategy import ArbPair


def test_arbpair_has_source_field_default_none():
    pair = ArbPair(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
    )
    assert pair.source is None


def test_arbpair_source_accepts_tree_value():
    pair = ArbPair(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
        source="tree",
    )
    assert pair.source == "tree"


def test_arbpair_engine_state_defaults_to_active():
    pair = ArbPair(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
    )
    assert pair.engine_state == "active"


def test_arbpair_engine_state_accepts_winding_down():
    pair = ArbPair(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
        engine_state="winding_down",
    )
    assert pair.engine_state == "winding_down"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/Scripts/python -m pytest tests/test_models_strategy.py -v
```

Expected: FAIL with `ValidationError` or `AttributeError` on `source` / `engine_state`.

- [ ] **Step 3: Add fields to `ArbPair`**

In `src/talos/models/strategy.py`, locate the `ArbPair` class definition and add two optional fields (preserving all existing fields):

```python
# In class ArbPair(BaseModel):
    source: str | None = None           # "tree" | "manual_url" | "restore" | "migration"
    engine_state: str = "active"        # "active" | "winding_down" | "exit_only"
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/Scripts/python -m pytest tests/test_models_strategy.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Run full suite to catch regressions**

```bash
.venv/Scripts/python -m pytest
```

Expected: all existing tests still pass (new fields are optional / have defaults).

- [ ] **Step 6: Commit**

```bash
git add src/talos/models/strategy.py tests/test_models_strategy.py
git commit -m "feat(models): add source and engine_state to ArbPair"
```

---

## Task 2: Update `_persist_games` to serialize new fields

**Files:**
- Modify: `src/talos/__main__.py` around line 345
- Test: `tests/test_legacy_writer_roundtrip.py` (new)

This is Codex P1 — the legacy writer must preserve `source` and `engine_state` round-trip so flag-off sessions don't strip them. Depends on Task 1.

- [ ] **Step 1: Write the failing regression test**

Create `tests/test_legacy_writer_roundtrip.py`:

```python
"""Regression: _persist_games must preserve source + engine_state round-trip
so flag-off sessions don't strip durability fields."""

from pathlib import Path
import json

from talos.models.strategy import ArbPair
from talos.persistence import save_games_full, load_saved_games_full


def test_games_full_preserves_source_field(tmp_path: Path):
    record = {
        "event_ticker": "KXFEDMENTION-26APR-YIEL",
        "ticker_a": "KXFEDMENTION-26APR-YIEL",
        "ticker_b": "KXFEDMENTION-26APR-YIEL",
        "side_a": "yes",
        "side_b": "no",
        "fee_type": "quadratic_with_maker_fees",
        "fee_rate": 0.0175,
        "source": "tree",
        "engine_state": "winding_down",
    }
    save_games_full([record], path=tmp_path / "games_full.json")
    loaded = load_saved_games_full(path=tmp_path / "games_full.json")
    assert loaded is not None
    assert loaded[0]["source"] == "tree"
    assert loaded[0]["engine_state"] == "winding_down"


def test_persist_games_roundtrips_source_and_engine_state(tmp_path: Path, monkeypatch):
    """Simulate what happens when __main__._persist_games writes a pair with
    source + engine_state and we re-read it — the fields must survive."""
    from talos import persistence
    monkeypatch.setattr(persistence, "_data_dir", tmp_path)

    pair = ArbPair(
        event_ticker="KXSURVIVORMENTION-26APR23",
        ticker_a="KXSURVIVORMENTION-26APR23-MRBE",
        ticker_b="KXSURVIVORMENTION-26APR23-MRBE",
        side_a="yes",
        side_b="no",
        source="tree",
        engine_state="winding_down",
    )
    # Simulate _persist_games' inner loop: build entry dict
    entry = {
        "event_ticker": pair.event_ticker,
        "ticker_a": pair.ticker_a,
        "ticker_b": pair.ticker_b,
        "side_a": pair.side_a,
        "side_b": pair.side_b,
        "fee_type": pair.fee_type,
        "fee_rate": pair.fee_rate,
    }
    # Phase 1 requirement: writer adds these
    if pair.source is not None:
        entry["source"] = pair.source
    entry["engine_state"] = pair.engine_state
    save_games_full([entry])

    reloaded = load_saved_games_full()
    assert reloaded is not None
    assert reloaded[0]["source"] == "tree"
    assert reloaded[0]["engine_state"] == "winding_down"
```

- [ ] **Step 2: Run test — the first should pass already (models support it), the second tests the writer pattern**

```bash
.venv/Scripts/python -m pytest tests/test_legacy_writer_roundtrip.py -v
```

Expected: both pass if `save_games_full`/`load_saved_games_full` are faithful JSON round-trip (they are — they `json.dumps`/`json.loads` dicts). If they fail, read `persistence.py` and fix the round-trip. Most likely they pass; the real value of the test lands when we update `__main__._persist_games` next.

- [ ] **Step 3: Update `_persist_games` in `__main__.py`**

Locate `_persist_games` function (around line 345 of `src/talos/__main__.py`). In the entry-dict build loop, after the existing fields, add:

```python
            # Phase 1: persist tree-mode durability fields.
            # - source is observability only; write only when set.
            # - engine_state is safety-critical; always write (default "active").
            if p.source is not None:
                entry["source"] = p.source
            entry["engine_state"] = p.engine_state
```

Place this after the existing `talos_id` entry and before the `vol_a = game_mgr.volumes_24h.get(...)` line.

- [ ] **Step 4: Write a writer integration test**

Add to `tests/test_legacy_writer_roundtrip.py`:

```python
def test_persist_games_on_change_preserves_fields_flag_off(tmp_path: Path, monkeypatch):
    """Phase 3 dual-run proof: if a tree-mode session wrote a pair with
    source='tree' and engine_state='winding_down', a later legacy session
    that triggers _persist_games (via on_change) must preserve them."""
    from talos import persistence
    monkeypatch.setattr(persistence, "_data_dir", tmp_path)

    # Seed the file as if a tree-mode session had just written it
    original = [{
        "event_ticker": "KXFEDMENTION-26APR-YIEL",
        "ticker_a": "KXFEDMENTION-26APR-YIEL",
        "ticker_b": "KXFEDMENTION-26APR-YIEL",
        "side_a": "yes",
        "side_b": "no",
        "fee_type": "quadratic_with_maker_fees",
        "fee_rate": 0.0175,
        "close_time": "2026-04-30T14:00:00Z",
        "expected_expiration_time": "2026-04-29T14:00:00Z",
        "source": "tree",
        "engine_state": "winding_down",
    }]
    save_games_full(original)

    # Simulate legacy session: load via load_saved_games_full, restore into
    # ArbPair(s) (which now carry the new fields thanks to Task 1), and
    # re-persist via the updated _persist_games pattern.
    loaded = load_saved_games_full()
    assert loaded is not None
    pairs = [ArbPair(**r) for r in loaded]

    entries = []
    for p in pairs:
        entry = {
            "event_ticker": p.event_ticker,
            "ticker_a": p.ticker_a,
            "ticker_b": p.ticker_b,
            "side_a": p.side_a,
            "side_b": p.side_b,
            "fee_type": p.fee_type,
            "fee_rate": p.fee_rate,
        }
        if p.source is not None:
            entry["source"] = p.source
        entry["engine_state"] = p.engine_state
        entries.append(entry)
    save_games_full(entries)

    # Round-trip must preserve both fields
    reloaded = load_saved_games_full()
    assert reloaded[0]["source"] == "tree"
    assert reloaded[0]["engine_state"] == "winding_down"
```

- [ ] **Step 5: Run test**

```bash
.venv/Scripts/python -m pytest tests/test_legacy_writer_roundtrip.py -v
```

Expected: all 3 tests pass.

- [ ] **Step 6: Lint**

```bash
.venv/Scripts/python -m ruff check --fix src/ tests/
.venv/Scripts/python -m pyright src/talos/__main__.py
```

- [ ] **Step 7: Commit**

```bash
git add src/talos/__main__.py tests/test_legacy_writer_roundtrip.py
git commit -m "feat(persistence): _persist_games preserves source+engine_state"
```

---

## Task 3: Create tree/discovery Pydantic models

**Files:**
- Create: `src/talos/models/tree.py`
- Test: `tests/test_models_tree.py` (new)

These are the data-transfer objects passed between TreeScreen ↔ Engine and cached by DiscoveryService. Pure data classes — validates the schema without any behavior.

- [ ] **Step 1: Write the test first**

Create `tests/test_models_tree.py`:

```python
"""Tests for tree/discovery Pydantic models."""
from datetime import datetime, UTC

from talos.models.tree import (
    ArbPairRecord, RemoveOutcome, StagedChanges,
    Milestone, MarketNode, EventNode, SeriesNode, CategoryNode,
)


def test_arbpair_record_minimal_fields():
    r = ArbPairRecord(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
        kalshi_event_ticker="KXFEDMENTION-26APR",
        series_ticker="KXFEDMENTION",
        category="Mentions",
    )
    assert r.side_a == "yes"
    assert r.side_b == "no"
    assert r.source == "tree"
    assert r.markets is None  # null means "all active"
    assert r.volume_24h_a is None


def test_arbpair_record_carries_volume_data():
    r = ArbPairRecord(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
        kalshi_event_ticker="KXFEDMENTION-26APR",
        series_ticker="KXFEDMENTION",
        category="Mentions",
        volume_24h_a=1234,
        volume_24h_b=1234,
    )
    assert r.volume_24h_a == 1234
    assert r.volume_24h_b == 1234


def test_remove_outcome_statuses():
    o = RemoveOutcome(
        pair_ticker="K-1",
        kalshi_event_ticker="K",
        status="winding_down",
        reason="filled=5,3",
    )
    assert o.status == "winding_down"
    assert o.reason == "filled=5,3"


def test_staged_changes_empty():
    s = StagedChanges.empty()
    assert s.to_add == []
    assert s.to_remove == []
    assert s.is_empty()


def test_milestone_start_date_parses():
    m = Milestone(
        id="abc",
        category="mentions",
        type="one_off_milestone",
        start_date=datetime(2026, 4, 22, 20, 0, tzinfo=UTC),
        end_date=datetime(2026, 4, 22, 22, 0, tzinfo=UTC),
        title="Survivor Episode 9",
        related_event_tickers=["KXSURVIVORMENTION-26APR23"],
    )
    assert m.start_date.year == 2026


def test_category_node_series_count():
    cat = CategoryNode(name="Mentions", series_count=335, series={})
    assert cat.series_count == 335
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/Scripts/python -m pytest tests/test_models_tree.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'talos.models.tree'`.

- [ ] **Step 3: Create `src/talos/models/tree.py`**

```python
"""Tree-UI and discovery-layer data models.

Pure data containers. No behavior beyond Pydantic validation. Shared between
TreeScreen, SelectionStore, Engine, DiscoveryService, and MilestoneResolver.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ── Commit-path DTOs ────────────────────────────────────────────────────


class ArbPairRecord(BaseModel):
    """What TreeScreen stages and hands to Engine.add_pairs_from_selection.

    Field shape intentionally matches games_full.json record shape so the
    same dict can feed GameManager.restore_game() directly.
    """

    # Pair identity — matches ArbPair
    event_ticker: str
    ticker_a: str
    ticker_b: str
    side_a: str = "yes"
    side_b: str = "no"

    # Event grouping
    kalshi_event_ticker: str
    series_ticker: str
    category: str

    # Fee metadata (hydrated from DiscoveryService at commit time)
    fee_type: str = "quadratic_with_maker_fees"
    fee_rate: float = 0.0175

    # Timing hints
    close_time: str | None = None
    expected_expiration_time: str | None = None

    # Display
    sub_title: str = ""
    label: str = ""

    # Tree-specific
    source: str = "tree"
    selected_at: str | None = None

    # For non-sports multi-market events: if null, all active markets selected;
    # otherwise, list of specific market tickers.
    markets: list[str] | None = None

    # 24h volume seeded from discovery cache — avoids zero-volume problem
    # described in Codex round 5 P2.
    volume_24h_a: int | None = None
    volume_24h_b: int | None = None


class RemoveOutcome(BaseModel):
    """Per-pair outcome from Engine.remove_pairs_from_selection."""

    pair_ticker: str
    kalshi_event_ticker: str
    status: Literal["removed", "winding_down", "not_found", "failed"]
    reason: str | None = None


class StagedChanges(BaseModel):
    """In-memory staged tree edits held by TreeScreen until commit."""

    to_add: list[ArbPairRecord] = Field(default_factory=list)
    to_remove: list[str] = Field(default_factory=list)
    to_set_unticked: list[str] = Field(default_factory=list)
    to_clear_unticked: list[str] = Field(default_factory=list)
    to_set_manual_start: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def empty(cls) -> "StagedChanges":
        return cls()

    def is_empty(self) -> bool:
        return not (self.to_add or self.to_remove or self.to_set_unticked
                    or self.to_clear_unticked or self.to_set_manual_start)


# ── Milestones ──────────────────────────────────────────────────────────


class Milestone(BaseModel):
    """Kalshi milestone record from /milestones endpoint."""

    id: str
    category: str
    type: str                          # one_off_milestone, fomc_meeting, basketball_game, ...
    start_date: datetime
    end_date: datetime
    title: str
    related_event_tickers: list[str]
    notification_message: str = ""


# ── Discovery cache models ──────────────────────────────────────────────


class MarketNode(BaseModel):
    """A single Kalshi market (YES/NO instrument) — discovery cache entry."""

    ticker: str
    title: str
    yes_bid: int | None = None
    yes_ask: int | None = None
    volume_24h: int = 0
    open_interest: int = 0
    status: str = "active"
    close_time: datetime | None = None


class EventNode(BaseModel):
    """A single Kalshi event — contains one or more MarketNodes."""

    ticker: str
    series_ticker: str
    title: str
    sub_title: str = ""
    close_time: datetime | None = None
    milestone: Milestone | None = None
    markets: list[MarketNode] = Field(default_factory=list)
    fetched_at: datetime | None = None


class SeriesNode(BaseModel):
    """A Kalshi series — container for its events."""

    ticker: str
    title: str
    category: str
    tags: list[str] = Field(default_factory=list)
    frequency: str = "custom"
    fee_type: str = "quadratic_with_maker_fees"
    fee_multiplier: float = 1.0
    # events: None means "not fetched yet"; {} means "fetched and empty"
    events: dict[str, EventNode] | None = None
    events_loaded_at: datetime | None = None


class CategoryNode(BaseModel):
    """A Kalshi category — top of the discovery tree."""

    name: str
    series_count: int
    series: dict[str, SeriesNode] = Field(default_factory=dict)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/Scripts/python -m pytest tests/test_models_tree.py -v
```

Expected: 6 tests pass.

- [ ] **Step 5: Lint + type-check**

```bash
.venv/Scripts/python -m ruff check --fix src/ tests/
.venv/Scripts/python -m pyright src/talos/models/tree.py
```

- [ ] **Step 6: Commit**

```bash
git add src/talos/models/tree.py tests/test_models_tree.py
git commit -m "feat(models): add tree-UI and discovery-layer Pydantic models"
```

---

## Task 4: Extend `persistence.py` with `tree_metadata.json` I/O

**Files:**
- Modify: `src/talos/persistence.py`
- Test: `tests/test_persistence_tree_metadata.py` (new)

Pure I/O helpers. `TreeMetadataStore` (next task) will sit on top of these.

- [ ] **Step 1: Write the test**

Create `tests/test_persistence_tree_metadata.py`:

```python
from pathlib import Path
import json

from talos.persistence import load_tree_metadata, save_tree_metadata


def test_load_returns_empty_default_when_missing(tmp_path: Path):
    data = load_tree_metadata(path=tmp_path / "tree_metadata.json")
    assert data == {
        "version": 1,
        "event_first_seen": {},
        "event_reviewed_at": {},
        "manual_event_start": {},
        "deliberately_unticked": [],
        "deliberately_unticked_pending": [],
    }


def test_save_and_load_roundtrip(tmp_path: Path):
    original = {
        "version": 1,
        "event_first_seen": {"K-1": "2026-04-16T18:32:00Z"},
        "event_reviewed_at": {"K-1": "2026-04-16T19:00:00Z"},
        "manual_event_start": {"K-2": "2026-04-22T20:00:00-04:00"},
        "deliberately_unticked": ["K-3"],
        "deliberately_unticked_pending": ["K-4"],
    }
    save_tree_metadata(original, path=tmp_path / "tree_metadata.json")
    loaded = load_tree_metadata(path=tmp_path / "tree_metadata.json")
    assert loaded == original


def test_load_corrupt_file_returns_defaults(tmp_path: Path):
    f = tmp_path / "tree_metadata.json"
    f.write_text("{broken json")
    data = load_tree_metadata(path=f)
    assert data["version"] == 1
    assert data["deliberately_unticked"] == []


def test_load_partial_file_backfills_missing_keys(tmp_path: Path):
    """Forward-compat: older files missing a key must still load cleanly
    with defaults backfilled."""
    f = tmp_path / "tree_metadata.json"
    f.write_text(json.dumps({
        "version": 1,
        "event_first_seen": {"K-1": "2026-04-16T00:00:00Z"},
        # Missing: event_reviewed_at, manual_event_start, deliberately_unticked*
    }))
    data = load_tree_metadata(path=f)
    assert data["event_first_seen"] == {"K-1": "2026-04-16T00:00:00Z"}
    assert data["event_reviewed_at"] == {}
    assert data["manual_event_start"] == {}
    assert data["deliberately_unticked"] == []
    assert data["deliberately_unticked_pending"] == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/Scripts/python -m pytest tests/test_persistence_tree_metadata.py -v
```

Expected: FAIL — `ImportError: cannot import name 'load_tree_metadata'`.

- [ ] **Step 3: Add functions to `persistence.py`**

Open `src/talos/persistence.py` and add at the end (after existing functions):

```python
# ---------------------------------------------------------------------------
# Tree metadata persistence
# ---------------------------------------------------------------------------
def _tree_metadata_file(path: Path | None = None) -> Path:
    return path or (get_data_dir() / "tree_metadata.json")


_TREE_METADATA_DEFAULTS: dict[str, object] = {
    "version": 1,
    "event_first_seen": {},
    "event_reviewed_at": {},
    "manual_event_start": {},
    "deliberately_unticked": [],
    "deliberately_unticked_pending": [],
}


def load_tree_metadata(path: Path | None = None) -> dict[str, object]:
    """Load tree_metadata.json. Returns defaults if missing or corrupt.

    Forward-compatible: any missing keys from older versions are backfilled
    with their default value, so tests / callers can assume all keys exist.
    """
    f = _tree_metadata_file(path)
    if not f.is_file():
        return {k: _default_copy(v) for k, v in _TREE_METADATA_DEFAULTS.items()}
    try:
        data = json.loads(f.read_text())
        if not isinstance(data, dict):
            raise ValueError("tree_metadata must be a JSON object")
    except Exception:
        logger.warning("load_tree_metadata_failed", path=str(f))
        return {k: _default_copy(v) for k, v in _TREE_METADATA_DEFAULTS.items()}

    # Backfill missing keys
    for k, default in _TREE_METADATA_DEFAULTS.items():
        if k not in data:
            data[k] = _default_copy(default)
    return data


def save_tree_metadata(data: dict[str, object], path: Path | None = None) -> None:
    """Persist tree_metadata.json."""
    f = _tree_metadata_file(path)
    try:
        f.write_text(json.dumps(data, indent=2) + "\n")
    except Exception:
        logger.debug("save_tree_metadata_failed", path=str(f))


def _default_copy(v: object) -> object:
    """Copy mutable defaults (dict/list) to avoid shared-reference bugs."""
    if isinstance(v, dict):
        return {}
    if isinstance(v, list):
        return []
    return v
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/Scripts/python -m pytest tests/test_persistence_tree_metadata.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Lint + pyright**

```bash
.venv/Scripts/python -m ruff check --fix src/ tests/
.venv/Scripts/python -m pyright src/talos/persistence.py
```

- [ ] **Step 6: Commit**

```bash
git add src/talos/persistence.py tests/test_persistence_tree_metadata.py
git commit -m "feat(persistence): load/save tree_metadata.json with forward-compat defaults"
```

---

## Task 5: `TreeMetadataStore` — in-memory wrapper with event emission

**Files:**
- Create: `src/talos/tree_metadata.py`
- Test: `tests/test_tree_metadata.py` (new)

Wraps the JSON file with a typed API. Exposes `manual_event_start()` for the resolver cascade, tracks NEW flags, handles deferred `[·]` set.

- [ ] **Step 1: Write the test**

Create `tests/test_tree_metadata.py`:

```python
from datetime import datetime, UTC
from pathlib import Path

from talos.tree_metadata import TreeMetadataStore


def test_empty_store(tmp_path: Path):
    store = TreeMetadataStore(path=tmp_path / "tree_metadata.json")
    store.load()
    assert store.manual_event_start("KX-ANYTHING") is None
    assert not store.is_deliberately_unticked("KX-ANYTHING")
    assert not store.is_deliberately_unticked_pending("KX-ANYTHING")


def test_set_and_read_manual_event_start(tmp_path: Path):
    store = TreeMetadataStore(path=tmp_path / "tree_metadata.json")
    store.load()
    dt = datetime(2026, 4, 22, 20, 0, tzinfo=UTC)
    store.set_manual_event_start("KX-FOO", dt.isoformat())
    assert store.manual_event_start("KX-FOO") == dt


def test_manual_event_start_none_value_returns_opt_out(tmp_path: Path):
    store = TreeMetadataStore(path=tmp_path / "tree_metadata.json")
    store.load()
    store.set_manual_event_start("KX-FOO", "none")
    assert store.manual_event_start("KX-FOO") == "none"


def test_first_seen_and_reviewed(tmp_path: Path):
    store = TreeMetadataStore(path=tmp_path / "tree_metadata.json")
    store.load()
    assert store.is_new("KX-NEW")  # never seen → treated as "not new yet"
    store.mark_first_seen("KX-NEW")
    assert store.is_new("KX-NEW")  # seen but not reviewed → new
    store.mark_reviewed("KX-NEW")
    assert not store.is_new("KX-NEW")


def test_deliberately_unticked_lifecycle(tmp_path: Path):
    store = TreeMetadataStore(path=tmp_path / "tree_metadata.json")
    store.load()
    store.set_deliberately_unticked("KX-EVT")
    assert store.is_deliberately_unticked("KX-EVT")
    store.clear_deliberately_unticked("KX-EVT")
    assert not store.is_deliberately_unticked("KX-EVT")


def test_deferred_unticked_lifecycle(tmp_path: Path):
    store = TreeMetadataStore(path=tmp_path / "tree_metadata.json")
    store.load()
    store.set_deliberately_unticked_pending("KX-EVT")
    assert store.is_deliberately_unticked_pending("KX-EVT")
    # Promotion: when event fully removed, pending → applied
    store.promote_pending_to_applied("KX-EVT")
    assert not store.is_deliberately_unticked_pending("KX-EVT")
    assert store.is_deliberately_unticked("KX-EVT")


def test_persistence_roundtrip(tmp_path: Path):
    path = tmp_path / "tree_metadata.json"
    s1 = TreeMetadataStore(path=path)
    s1.load()
    s1.set_manual_event_start("KX-A", "2026-04-22T20:00:00-04:00")
    s1.mark_first_seen("KX-A")
    s1.set_deliberately_unticked_pending("KX-A")
    s1.save()

    s2 = TreeMetadataStore(path=path)
    s2.load()
    assert s2.manual_event_start("KX-A") == datetime.fromisoformat(
        "2026-04-22T20:00:00-04:00"
    )
    assert s2.is_deliberately_unticked_pending("KX-A")


def test_save_on_every_mutation_when_autosave(tmp_path: Path):
    path = tmp_path / "tree_metadata.json"
    s1 = TreeMetadataStore(path=path, autosave=True)
    s1.load()
    s1.set_manual_event_start("KX-A", "2026-04-22T20:00:00Z")
    # File should already be written
    s2 = TreeMetadataStore(path=path)
    s2.load()
    assert s2.manual_event_start("KX-A") is not None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/Scripts/python -m pytest tests/test_tree_metadata.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Create `src/talos/tree_metadata.py`**

```python
"""TreeMetadataStore — typed wrapper around tree_metadata.json.

Owns event-level state:
- Manual event-start overrides (resolver-cascade priority 1)
- NEW indicator bookkeeping (first_seen, reviewed_at)
- Deliberately-unticked flags (applied and pending)

All mutations go through the typed API. Persistence is automatic when
`autosave=True` (default for production); set `autosave=False` in tests
that want to batch mutations before asserting disk state.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

import structlog

from talos.persistence import load_tree_metadata, save_tree_metadata

logger = structlog.get_logger()


class TreeMetadataStore:
    """Read/write interface for tree_metadata.json."""

    def __init__(self, path: Path | None = None, *, autosave: bool = True) -> None:
        self._path = path
        self._autosave = autosave
        self._data: dict[str, object] = {}
        self._loaded = False

    # ── Lifecycle ────────────────────────────────────────────────────

    def load(self) -> None:
        self._data = load_tree_metadata(self._path)
        self._loaded = True

    def save(self) -> None:
        save_tree_metadata(self._data, self._path)

    def _touch(self) -> None:
        if self._autosave:
            self.save()

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("TreeMetadataStore.load() must be called before use")

    # ── Manual event-start overrides ─────────────────────────────────

    def manual_event_start(
        self, kalshi_event_ticker: str
    ) -> datetime | Literal["none"] | None:
        """Return the user's manual override for this event, or None.

        Return value:
          - datetime — explicit event-start set by user
          - "none"   — user explicitly opted out of exit-only for this event
          - None     — no override set; resolver cascade should fall through
        """
        self._require_loaded()
        raw = self._manual_dict().get(kalshi_event_ticker)
        if raw is None:
            return None
        if raw == "none":
            return "none"
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            logger.warning("manual_event_start_invalid",
                           event=kalshi_event_ticker, raw=raw)
            return None

    def set_manual_event_start(self, kalshi_event_ticker: str, value: str) -> None:
        """Set a manual override. `value` is ISO 8601 datetime or 'none'."""
        self._require_loaded()
        self._manual_dict()[kalshi_event_ticker] = value
        self._touch()

    def clear_manual_event_start(self, kalshi_event_ticker: str) -> None:
        self._require_loaded()
        self._manual_dict().pop(kalshi_event_ticker, None)
        self._touch()

    # ── NEW indicator ─────────────────────────────────────────────────

    def is_new(self, kalshi_event_ticker: str) -> bool:
        """An event is NEW iff it's been seen but not reviewed."""
        self._require_loaded()
        seen = kalshi_event_ticker in self._first_seen_dict()
        reviewed = kalshi_event_ticker in self._reviewed_dict()
        return seen and not reviewed

    def mark_first_seen(self, kalshi_event_ticker: str) -> None:
        """Idempotent: only sets first_seen if not already present."""
        self._require_loaded()
        d = self._first_seen_dict()
        if kalshi_event_ticker not in d:
            d[kalshi_event_ticker] = datetime.utcnow().isoformat() + "Z"
            self._touch()

    def mark_reviewed(self, kalshi_event_ticker: str) -> None:
        """Clear the NEW flag by marking as reviewed."""
        self._require_loaded()
        d = self._reviewed_dict()
        d[kalshi_event_ticker] = datetime.utcnow().isoformat() + "Z"
        self._touch()

    # ── Deliberately unticked ─────────────────────────────────────────

    def is_deliberately_unticked(self, kalshi_event_ticker: str) -> bool:
        self._require_loaded()
        return kalshi_event_ticker in self._unticked_applied()

    def set_deliberately_unticked(self, kalshi_event_ticker: str) -> None:
        self._require_loaded()
        lst = self._unticked_applied()
        if kalshi_event_ticker not in lst:
            lst.append(kalshi_event_ticker)
            self._touch()

    def clear_deliberately_unticked(self, kalshi_event_ticker: str) -> None:
        self._require_loaded()
        lst = self._unticked_applied()
        if kalshi_event_ticker in lst:
            lst.remove(kalshi_event_ticker)
            self._touch()

    # ── Deliberately unticked (pending) ───────────────────────────────

    def is_deliberately_unticked_pending(self, kalshi_event_ticker: str) -> bool:
        self._require_loaded()
        return kalshi_event_ticker in self._unticked_pending()

    def set_deliberately_unticked_pending(self, kalshi_event_ticker: str) -> None:
        self._require_loaded()
        lst = self._unticked_pending()
        if kalshi_event_ticker not in lst:
            lst.append(kalshi_event_ticker)
            self._touch()

    def clear_deliberately_unticked_pending(self, kalshi_event_ticker: str) -> None:
        self._require_loaded()
        lst = self._unticked_pending()
        if kalshi_event_ticker in lst:
            lst.remove(kalshi_event_ticker)
            self._touch()

    def promote_pending_to_applied(self, kalshi_event_ticker: str) -> None:
        """Called when engine emits event_fully_removed for a pending event."""
        self._require_loaded()
        self.clear_deliberately_unticked_pending(kalshi_event_ticker)
        self.set_deliberately_unticked(kalshi_event_ticker)

    # ── Internal accessors ────────────────────────────────────────────

    def _manual_dict(self) -> dict[str, str]:
        return self._data["manual_event_start"]  # type: ignore[return-value]

    def _first_seen_dict(self) -> dict[str, str]:
        return self._data["event_first_seen"]  # type: ignore[return-value]

    def _reviewed_dict(self) -> dict[str, str]:
        return self._data["event_reviewed_at"]  # type: ignore[return-value]

    def _unticked_applied(self) -> list[str]:
        return self._data["deliberately_unticked"]  # type: ignore[return-value]

    def _unticked_pending(self) -> list[str]:
        return self._data["deliberately_unticked_pending"]  # type: ignore[return-value]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/Scripts/python -m pytest tests/test_tree_metadata.py -v
```

Expected: 8 tests pass.

- [ ] **Step 5: Lint + pyright**

```bash
.venv/Scripts/python -m ruff check --fix src/ tests/
.venv/Scripts/python -m pyright src/talos/tree_metadata.py
```

- [ ] **Step 6: Commit**

```bash
git add src/talos/tree_metadata.py tests/test_tree_metadata.py
git commit -m "feat(tree): TreeMetadataStore wraps tree_metadata.json"
```

---

## Task 6: `MilestoneResolver` — `/milestones` API + in-memory index

**Files:**
- Create: `src/talos/milestones.py`
- Test: `tests/test_milestones.py` (new)

Paginated fetch of upcoming milestones, indexed by `event_ticker` for O(1) lookup. Atomic-swap refresh so cascade readers never see partial state.

- [ ] **Step 1: Write the test**

Create `tests/test_milestones.py`:

```python
from datetime import datetime, UTC
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from talos.milestones import MilestoneResolver


@pytest.fixture
def sample_milestone_response() -> dict:
    return {
        "milestones": [
            {
                "id": "c8bb4f46-eb47-4f84-9723-ad9b1961d2b5",
                "category": "mentions",
                "type": "one_off_milestone",
                "start_date": "2026-04-16T23:00:00Z",
                "end_date": "2026-04-17T01:00:00Z",
                "title": "Trump holds a roundtable on No Tax on Tips",
                "notification_message": "What will Trump say?",
                "related_event_tickers": ["KXTRUMPMENTION-26APR16"],
                "primary_event_tickers": ["KXTRUMPMENTION-26APR16"],
                "last_updated_ts": "2026-04-16T14:40:36.610301Z",
                "details": {},
                "product_details": {},
                "source_ids": {},
            },
        ],
        "cursor": "",
    }


@pytest.mark.asyncio
async def test_empty_resolver_returns_none():
    r = MilestoneResolver()
    assert r.event_start("KX-ANY") is None


@pytest.mark.asyncio
async def test_refresh_builds_index(sample_milestone_response: dict):
    r = MilestoneResolver()
    with patch.object(r, "_paginated_fetch",
                      new=AsyncMock(return_value=sample_milestone_response["milestones"])):
        await r.refresh()
    start = r.event_start("KXTRUMPMENTION-26APR16")
    assert start == datetime(2026, 4, 16, 23, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_refresh_replaces_index_atomically(sample_milestone_response: dict):
    r = MilestoneResolver()
    with patch.object(r, "_paginated_fetch",
                      new=AsyncMock(return_value=sample_milestone_response["milestones"])):
        await r.refresh()
    # Simulate a subsequent refresh with an empty list
    with patch.object(r, "_paginated_fetch", new=AsyncMock(return_value=[])):
        await r.refresh()
    assert r.event_start("KXTRUMPMENTION-26APR16") is None


@pytest.mark.asyncio
async def test_refresh_failure_keeps_old_index(sample_milestone_response: dict):
    r = MilestoneResolver()
    with patch.object(r, "_paginated_fetch",
                      new=AsyncMock(return_value=sample_milestone_response["milestones"])):
        await r.refresh()
    with patch.object(r, "_paginated_fetch",
                      new=AsyncMock(side_effect=httpx.HTTPError("boom"))):
        await r.refresh()  # must not raise
    # Old data still available
    assert r.event_start("KXTRUMPMENTION-26APR16") is not None


@pytest.mark.asyncio
async def test_multiple_events_in_one_milestone(sample_milestone_response: dict):
    ms = dict(sample_milestone_response["milestones"][0])
    ms["related_event_tickers"] = ["KXA-1", "KXA-2"]
    r = MilestoneResolver()
    with patch.object(r, "_paginated_fetch", new=AsyncMock(return_value=[ms])):
        await r.refresh()
    assert r.event_start("KXA-1") is not None
    assert r.event_start("KXA-2") is not None


@pytest.mark.asyncio
async def test_unknown_event_returns_none(sample_milestone_response: dict):
    r = MilestoneResolver()
    with patch.object(r, "_paginated_fetch",
                      new=AsyncMock(return_value=sample_milestone_response["milestones"])):
        await r.refresh()
    assert r.event_start("KXOTHERMENTION-99") is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/Scripts/python -m pytest tests/test_milestones.py -v
```

Expected: FAIL — module missing.

- [ ] **Step 3: Create `src/talos/milestones.py`**

```python
"""MilestoneResolver — Kalshi /milestones index.

Pulls upcoming milestones via paginated /milestones calls and maintains an
in-memory index keyed by event_ticker. Refresh is atomic-swap so readers
(Engine._check_exit_only) never see partial state.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from talos.models.tree import Milestone

logger = structlog.get_logger()

_KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class MilestoneResolver:
    """In-memory milestone index with scheduled refresh."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http
        self._owns_http = http is None
        self._by_event_ticker: dict[str, Milestone] = {}
        self._last_refresh: datetime | None = None

    # ── Public API ───────────────────────────────────────────────────

    def event_start(self, event_ticker: str) -> datetime | None:
        """O(1) lookup of the curated event-start for this event, if any."""
        ms = self._by_event_ticker.get(event_ticker)
        return ms.start_date if ms else None

    def get_milestone(self, event_ticker: str) -> Milestone | None:
        return self._by_event_ticker.get(event_ticker)

    @property
    def last_refresh(self) -> datetime | None:
        return self._last_refresh

    @property
    def count(self) -> int:
        return len(self._by_event_ticker)

    # ── Refresh ──────────────────────────────────────────────────────

    async def refresh(self) -> None:
        """Pull upcoming milestones from /milestones; atomic-swap the index.

        On failure: keep the existing index. Log a warning. Never raise.
        """
        try:
            items = await self._paginated_fetch()
        except Exception:
            logger.warning("milestone_refresh_failed", exc_info=True)
            return

        new_index: dict[str, Milestone] = {}
        for raw in items:
            try:
                ms = self._parse_milestone(raw)
            except Exception:
                logger.warning("milestone_parse_failed",
                               milestone_id=raw.get("id"), exc_info=True)
                continue
            for et in ms.related_event_tickers:
                new_index[et] = ms

        self._by_event_ticker = new_index  # atomic swap
        self._last_refresh = datetime.now(UTC)
        logger.info("milestone_refresh_ok",
                    milestone_count=len(items),
                    event_index_size=len(new_index))

    # ── Internals ────────────────────────────────────────────────────

    async def _paginated_fetch(self) -> list[dict[str, Any]]:
        """Paginate /milestones?minimum_start_date=<now>&limit=200."""
        now_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
        http = self._http or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        try:
            out: list[dict[str, Any]] = []
            cursor: str | None = None
            for _ in range(40):  # safety cap — 40 * 200 = 8000 milestones
                params: dict[str, str] = {
                    "limit": "200",
                    "minimum_start_date": now_iso,
                }
                if cursor:
                    params["cursor"] = cursor
                resp = await http.get(f"{_KALSHI_API_BASE}/milestones", params=params)
                resp.raise_for_status()
                data = resp.json()
                out.extend(data.get("milestones", []))
                cursor = data.get("cursor")
                if not cursor:
                    break
            return out
        finally:
            if self._owns_http:
                await http.aclose()

    def _parse_milestone(self, raw: dict[str, Any]) -> Milestone:
        return Milestone(
            id=raw["id"],
            category=raw.get("category", ""),
            type=raw.get("type", ""),
            start_date=datetime.fromisoformat(
                raw["start_date"].replace("Z", "+00:00")
            ),
            end_date=datetime.fromisoformat(
                raw["end_date"].replace("Z", "+00:00")
            ),
            title=raw.get("title", ""),
            notification_message=raw.get("notification_message", ""),
            related_event_tickers=raw.get("related_event_tickers", []),
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/Scripts/python -m pytest tests/test_milestones.py -v
```

Expected: 6 tests pass.

- [ ] **Step 5: Lint + pyright**

```bash
.venv/Scripts/python -m ruff check --fix src/ tests/
.venv/Scripts/python -m pyright src/talos/milestones.py
```

- [ ] **Step 6: Commit**

```bash
git add src/talos/milestones.py tests/test_milestones.py
git commit -m "feat(milestones): MilestoneResolver indexes /milestones by event"
```

---

## Task 7: Extend `automation_config.py` with new settings

**Files:**
- Modify: `src/talos/automation_config.py`
- Test: `tests/test_automation_config.py` (extend or create)

Per spec §6.1.

- [ ] **Step 1: Write the test**

Add to `tests/test_automation_config.py` (create if absent):

```python
from talos.automation_config import AutomationConfig


def test_defaults_include_tree_mode_settings():
    c = AutomationConfig()
    assert c.tree_mode is False
    assert c.startup_milestone_wait_seconds == 30.0
    assert c.schedule_conflict_threshold_minutes == 5.0
    assert c.discovery_concurrent_limit == 5
    assert c.milestone_refresh_seconds == 300.0


def test_exit_only_minutes_unchanged():
    """Regression: single exit_only_minutes setting retained per Q2."""
    c = AutomationConfig()
    assert c.exit_only_minutes == 30.0
```

- [ ] **Step 2: Run test — expect fail**

```bash
.venv/Scripts/python -m pytest tests/test_automation_config.py -v
```

- [ ] **Step 3: Add fields to `AutomationConfig`**

In `src/talos/automation_config.py`, add fields inside the `AutomationConfig` Pydantic class:

```python
    # Tree-mode feature flag — all new behavior gated on this.
    tree_mode: bool = False

    # Startup gate — max wait for milestones before engine begins tick loop.
    startup_milestone_wait_seconds: float = 30.0

    # Schedule conflict threshold — delta between manual override and Kalshi
    # milestone that triggers a user-resolved conflict prompt.
    schedule_conflict_threshold_minutes: float = 5.0

    # DiscoveryService semaphore — max concurrent discovery Kalshi calls.
    discovery_concurrent_limit: int = 5

    # Background milestone refresh interval.
    milestone_refresh_seconds: float = 300.0
```

- [ ] **Step 4: Run test — verify pass**

```bash
.venv/Scripts/python -m pytest tests/test_automation_config.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/talos/automation_config.py tests/test_automation_config.py
git commit -m "feat(config): add tree-mode settings to AutomationConfig"
```

---

## Task 8: `GameManager.suppress_on_change` context manager

**Files:**
- Modify: `src/talos/game_manager.py`
- Test: `tests/test_game_manager_suppress_on_change.py` (new)

Enables batch atomicity per Codex round 4 P2.

- [ ] **Step 1: Write the test**

Create `tests/test_game_manager_suppress_on_change.py`:

```python
from unittest.mock import MagicMock

from talos.game_manager import GameManager


def _make_game_manager() -> GameManager:
    # Minimal fixture — only need .on_change and suppress_on_change to work
    return GameManager.__new__(GameManager)


def test_suppress_on_change_pauses_callback():
    gm = _make_game_manager()
    cb = MagicMock()
    gm.on_change = cb

    with gm.suppress_on_change():
        # Simulate an internal mutation that would fire on_change
        if gm.on_change:
            gm.on_change()  # should be None here → no call

    cb.assert_not_called()


def test_suppress_on_change_restores_callback():
    gm = _make_game_manager()
    cb = MagicMock()
    gm.on_change = cb

    with gm.suppress_on_change():
        pass

    assert gm.on_change is cb

    # And firing after the block works
    if gm.on_change:
        gm.on_change()
    cb.assert_called_once()


def test_suppress_on_change_restores_on_exception():
    gm = _make_game_manager()
    cb = MagicMock()
    gm.on_change = cb

    try:
        with gm.suppress_on_change():
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    assert gm.on_change is cb
```

- [ ] **Step 2: Run test — expect fail**

```bash
.venv/Scripts/python -m pytest tests/test_game_manager_suppress_on_change.py -v
```

- [ ] **Step 3: Add the context manager to `GameManager`**

In `src/talos/game_manager.py`, add the `contextmanager` import at the top if absent:

```python
from contextlib import contextmanager
```

Then add this method inside `class GameManager`:

```python
    @contextmanager
    def suppress_on_change(self):
        """Pause on_change emission within a batch.

        Engine batch paths (add_pairs_from_selection, remove_pairs_from_selection)
        call this to prevent per-pair save_games_full writes during restore/
        remove loops. A single final persist runs in Engine._persist_active_games
        at batch end.

        Non-batch callers (URL-add via add_games, clear_all_games, UI
        re-renders) are unaffected — they keep firing on_change per-pair.
        """
        prev = self.on_change
        self.on_change = None
        try:
            yield
        finally:
            self.on_change = prev
```

- [ ] **Step 4: Run test — verify pass**

```bash
.venv/Scripts/python -m pytest tests/test_game_manager_suppress_on_change.py -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/talos/game_manager.py tests/test_game_manager_suppress_on_change.py
git commit -m "feat(game_manager): add suppress_on_change context manager"
```

---

## Task 9: `DiscoveryService` — bootstrap (series catalog)

**Files:**
- Create: `src/talos/discovery.py`
- Test: `tests/test_discovery_bootstrap.py` (new)

Initial series catalog pull. Lazy event fetch comes in Task 10.

- [ ] **Step 1: Write the test**

Create `tests/test_discovery_bootstrap.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from talos.discovery import DiscoveryService


_SERIES_SAMPLE = {
    "series": [
        {
            "ticker": "KXFEDMENTION",
            "title": "What will Powell say?",
            "category": "Mentions",
            "tags": ["Politicians"],
            "frequency": "one_off",
            "fee_type": "quadratic_with_maker_fees",
            "fee_multiplier": 1.0,
        },
        {
            "ticker": "KXNBAGAME",
            "title": "NBA game",
            "category": "Sports",
            "tags": ["Basketball"],
            "frequency": "daily",
            "fee_type": "quadratic_with_maker_fees",
            "fee_multiplier": 1.0,
        },
    ]
}


@pytest.mark.asyncio
async def test_bootstrap_populates_categories_and_series():
    ds = DiscoveryService()
    with patch.object(ds, "_fetch_all_series",
                      new=AsyncMock(return_value=_SERIES_SAMPLE["series"])):
        await ds.bootstrap()

    assert "Mentions" in ds.categories
    assert "Sports" in ds.categories
    assert ds.categories["Mentions"].series_count == 1
    assert "KXFEDMENTION" in ds.categories["Mentions"].series


@pytest.mark.asyncio
async def test_bootstrap_fills_series_metadata():
    ds = DiscoveryService()
    with patch.object(ds, "_fetch_all_series",
                      new=AsyncMock(return_value=_SERIES_SAMPLE["series"])):
        await ds.bootstrap()

    s = ds.categories["Mentions"].series["KXFEDMENTION"]
    assert s.title == "What will Powell say?"
    assert s.tags == ["Politicians"]
    assert s.frequency == "one_off"
    assert s.events is None  # not loaded yet — lazy


@pytest.mark.asyncio
async def test_bootstrap_failure_leaves_empty_cache():
    ds = DiscoveryService()
    with patch.object(ds, "_fetch_all_series",
                      new=AsyncMock(side_effect=RuntimeError("kaboom"))):
        await ds.bootstrap()
    assert ds.categories == {}
```

- [ ] **Step 2: Run test — expect fail**

```bash
.venv/Scripts/python -m pytest tests/test_discovery_bootstrap.py -v
```

- [ ] **Step 3: Create `src/talos/discovery.py`**

```python
"""DiscoveryService — Kalshi discovery cache.

Two-level cache:
- Categories + series list: eagerly loaded at bootstrap, manually refreshed.
- Events per series: lazily fetched on tree-expand, TTL 5 min.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from talos.models.tree import (
    CategoryNode,
    EventNode,
    MarketNode,
    Milestone,
    SeriesNode,
)

logger = structlog.get_logger()

_KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class DiscoveryService:
    """Discovery cache for categories, series, and events.

    Holds its own semaphore (default 5 slots) so discovery calls can't
    starve trading calls on the shared REST client pool.
    """

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        *,
        concurrent_limit: int = 5,
    ) -> None:
        self._http = http
        self._owns_http = http is None
        self._sem = asyncio.Semaphore(concurrent_limit)
        self.categories: dict[str, CategoryNode] = {}

    # ── Bootstrap ────────────────────────────────────────────────────

    async def bootstrap(self) -> None:
        """Pull full series catalog from /series and build the tree skeleton.

        On failure: log and leave the cache empty.
        """
        try:
            all_series = await self._fetch_all_series()
        except Exception:
            logger.warning("discovery_bootstrap_failed", exc_info=True)
            return

        categories: dict[str, CategoryNode] = {}
        for raw in all_series:
            cat_name = raw.get("category", "").strip() or "Uncategorized"
            series = SeriesNode(
                ticker=raw.get("ticker", ""),
                title=raw.get("title", ""),
                category=cat_name,
                tags=raw.get("tags") or [],
                frequency=raw.get("frequency", "custom"),
                fee_type=raw.get("fee_type", "quadratic_with_maker_fees"),
                fee_multiplier=float(raw.get("fee_multiplier", 1.0)),
            )
            node = categories.setdefault(
                cat_name, CategoryNode(name=cat_name, series_count=0, series={}),
            )
            node.series[series.ticker] = series

        # Set counts
        for cat in categories.values():
            cat.series_count = len(cat.series)

        self.categories = categories
        logger.info(
            "discovery_bootstrap_ok",
            category_count=len(categories),
            series_count=sum(c.series_count for c in categories.values()),
        )

    # ── Internals ────────────────────────────────────────────────────

    async def _fetch_all_series(self) -> list[dict[str, Any]]:
        async with self._sem:
            http = self._http or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
            try:
                resp = await http.get(f"{_KALSHI_API_BASE}/series")
                resp.raise_for_status()
                data = resp.json()
                return data.get("series", [])
            finally:
                if self._owns_http:
                    await http.aclose()
```

- [ ] **Step 4: Run test — verify pass**

```bash
.venv/Scripts/python -m pytest tests/test_discovery_bootstrap.py -v
```

Expected: 3 pass.

- [ ] **Step 5: Commit**

```bash
git add src/talos/discovery.py tests/test_discovery_bootstrap.py
git commit -m "feat(discovery): DiscoveryService.bootstrap loads series catalog"
```

---

## Task 10: `DiscoveryService` — lazy event fetch with TTL

**Files:**
- Modify: `src/talos/discovery.py`
- Test: `tests/test_discovery_events.py` (new)

- [ ] **Step 1: Write the test**

Create `tests/test_discovery_events.py`:

```python
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, patch

import pytest

from talos.discovery import DiscoveryService
from talos.models.tree import CategoryNode, SeriesNode


_EVENTS_SAMPLE = [
    {
        "event_ticker": "KXFEDMENTION-26APR",
        "series_ticker": "KXFEDMENTION",
        "title": "What will Powell say?",
        "sub_title": "On Apr 29, 2026",
        "category": "Mentions",
        "markets": [
            {
                "ticker": "KXFEDMENTION-26APR-YIEL",
                "title": "Will Powell say Yield Curve?",
                "status": "active",
                "volume_24h": 500,
                "open_interest_fp": "1200",
                "yes_bid_dollars": 0.20,
                "yes_ask_dollars": 0.25,
                "close_time": "2026-04-30T14:00:00Z",
            }
        ],
    },
]


def _preload_series(ds: DiscoveryService) -> None:
    s = SeriesNode(
        ticker="KXFEDMENTION", title="What will Powell say?",
        category="Mentions", tags=[], frequency="one_off",
    )
    ds.categories["Mentions"] = CategoryNode(
        name="Mentions", series_count=1, series={"KXFEDMENTION": s},
    )


@pytest.mark.asyncio
async def test_fetch_events_populates_series_and_markets():
    ds = DiscoveryService()
    _preload_series(ds)

    with patch.object(ds, "_fetch_events_for_series",
                      new=AsyncMock(return_value=_EVENTS_SAMPLE)):
        events = await ds.get_events_for_series("KXFEDMENTION")

    assert "KXFEDMENTION-26APR" in events
    ev = events["KXFEDMENTION-26APR"]
    assert ev.title == "What will Powell say?"
    assert ev.sub_title == "On Apr 29, 2026"
    assert len(ev.markets) == 1
    assert ev.markets[0].volume_24h == 500


@pytest.mark.asyncio
async def test_fetch_events_caches_within_ttl():
    ds = DiscoveryService()
    _preload_series(ds)
    fetch_mock = AsyncMock(return_value=_EVENTS_SAMPLE)

    with patch.object(ds, "_fetch_events_for_series", new=fetch_mock):
        await ds.get_events_for_series("KXFEDMENTION")
        await ds.get_events_for_series("KXFEDMENTION")  # should hit cache

    assert fetch_mock.await_count == 1


@pytest.mark.asyncio
async def test_fetch_events_refetches_after_ttl_expires():
    ds = DiscoveryService()
    _preload_series(ds)
    fetch_mock = AsyncMock(return_value=_EVENTS_SAMPLE)

    with patch.object(ds, "_fetch_events_for_series", new=fetch_mock):
        await ds.get_events_for_series("KXFEDMENTION")

        # Manually age the cache past TTL
        s = ds.categories["Mentions"].series["KXFEDMENTION"]
        s.events_loaded_at = datetime.now(UTC) - timedelta(minutes=6)

        await ds.get_events_for_series("KXFEDMENTION")

    assert fetch_mock.await_count == 2


@pytest.mark.asyncio
async def test_fetch_events_unknown_series_returns_empty():
    ds = DiscoveryService()
    events = await ds.get_events_for_series("KXNONEXISTENT")
    assert events == {}
```

- [ ] **Step 2: Run test — expect fail**

```bash
.venv/Scripts/python -m pytest tests/test_discovery_events.py -v
```

- [ ] **Step 3: Extend `DiscoveryService`**

Append to `src/talos/discovery.py`:

```python
    EVENTS_TTL_SECONDS = 300  # 5 min

    async def get_events_for_series(
        self, series_ticker: str
    ) -> dict[str, EventNode]:
        """Return events for a series, fetching lazily if not cached or stale.

        Returns {} for unknown series (not raised).
        """
        series = self._find_series(series_ticker)
        if series is None:
            return {}

        now = datetime.now(UTC)
        needs_fetch = (
            series.events is None
            or series.events_loaded_at is None
            or (now - series.events_loaded_at).total_seconds() > self.EVENTS_TTL_SECONDS
        )
        if not needs_fetch and series.events is not None:
            return series.events

        try:
            raw = await self._fetch_events_for_series(series_ticker)
        except Exception:
            logger.warning(
                "discovery_events_fetch_failed",
                series=series_ticker, exc_info=True,
            )
            # Keep previous cache (if any), just don't update timestamp
            return series.events or {}

        events: dict[str, EventNode] = {}
        for raw_ev in raw:
            try:
                events[raw_ev["event_ticker"]] = self._parse_event(raw_ev)
            except Exception:
                logger.warning(
                    "discovery_event_parse_failed",
                    event_ticker=raw_ev.get("event_ticker"),
                    exc_info=True,
                )

        series.events = events
        series.events_loaded_at = now
        return events

    # ── Internals ────────────────────────────────────────────────────

    def _find_series(self, series_ticker: str) -> SeriesNode | None:
        for cat in self.categories.values():
            if series_ticker in cat.series:
                return cat.series[series_ticker]
        return None

    async def _fetch_events_for_series(
        self, series_ticker: str
    ) -> list[dict[str, Any]]:
        async with self._sem:
            http = self._http or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
            try:
                resp = await http.get(
                    f"{_KALSHI_API_BASE}/events",
                    params={
                        "series_ticker": series_ticker,
                        "status": "open",
                        "with_nested_markets": "true",
                        "limit": "200",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("events", [])
            finally:
                if self._owns_http:
                    await http.aclose()

    def _parse_event(self, raw: dict[str, Any]) -> EventNode:
        markets = []
        for m in raw.get("markets", []):
            try:
                markets.append(self._parse_market(m))
            except Exception:
                logger.warning(
                    "market_parse_failed", ticker=m.get("ticker"), exc_info=True,
                )
        close = raw.get("close_time")
        close_dt = None
        if close:
            try:
                close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
            except ValueError:
                pass
        return EventNode(
            ticker=raw["event_ticker"],
            series_ticker=raw.get("series_ticker", ""),
            title=raw.get("title", ""),
            sub_title=raw.get("sub_title", ""),
            close_time=close_dt,
            markets=markets,
            fetched_at=datetime.now(UTC),
        )

    def _parse_market(self, raw: dict[str, Any]) -> MarketNode:
        close = raw.get("close_time")
        close_dt = None
        if close:
            try:
                close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
            except ValueError:
                pass
        # open_interest may arrive as string in some responses
        oi_raw = raw.get("open_interest_fp") or raw.get("open_interest") or 0
        try:
            oi = int(float(oi_raw))
        except (ValueError, TypeError):
            oi = 0
        return MarketNode(
            ticker=raw.get("ticker", ""),
            title=raw.get("title", ""),
            yes_bid=_to_cents(raw.get("yes_bid_dollars")),
            yes_ask=_to_cents(raw.get("yes_ask_dollars")),
            volume_24h=int(raw.get("volume_24h") or 0),
            open_interest=oi,
            status=raw.get("status", "active"),
            close_time=close_dt,
        )


def _to_cents(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(round(float(val) * 100))
    except (ValueError, TypeError):
        return None
```

- [ ] **Step 4: Run test — verify pass**

```bash
.venv/Scripts/python -m pytest tests/test_discovery_events.py -v
```

Expected: 4 pass.

- [ ] **Step 5: Commit**

```bash
git add src/talos/discovery.py tests/test_discovery_events.py
git commit -m "feat(discovery): lazy event fetch with 5-min TTL"
```

---

## Task 11: `DiscoveryService` — background milestone refresh loop

**Files:**
- Modify: `src/talos/discovery.py`
- Test: `tests/test_discovery_milestone_loop.py` (new)

Drives periodic milestone refresh per §3.3.

- [ ] **Step 1: Write the test**

Create `tests/test_discovery_milestone_loop.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.discovery import DiscoveryService
from talos.milestones import MilestoneResolver


@pytest.mark.asyncio
async def test_milestone_loop_calls_resolver_repeatedly():
    ds = DiscoveryService()
    resolver = MilestoneResolver()
    resolver.refresh = AsyncMock()  # type: ignore[method-assign]

    task = asyncio.create_task(
        ds.run_milestone_loop(resolver, interval_seconds=0.01),
    )
    await asyncio.sleep(0.05)
    ds.stop()
    await task

    assert resolver.refresh.await_count >= 3


@pytest.mark.asyncio
async def test_milestone_loop_survives_refresh_exception():
    ds = DiscoveryService()
    resolver = MilestoneResolver()
    resolver.refresh = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    task = asyncio.create_task(
        ds.run_milestone_loop(resolver, interval_seconds=0.01),
    )
    await asyncio.sleep(0.05)
    ds.stop()
    await task

    # Exceptions should not terminate the loop
    assert resolver.refresh.await_count >= 3
```

- [ ] **Step 2: Run test — expect fail**

- [ ] **Step 3: Add `run_milestone_loop` + `stop` to `DiscoveryService`**

In `src/talos/discovery.py`, add `self._stopped = False` to the existing `DiscoveryService.__init__` (alongside `self._sem`, `self.categories`, etc. — do not duplicate the method signature). Then append these two methods to the class:

```python
    def stop(self) -> None:
        """Signal background loops to exit after current iteration."""
        self._stopped = True

    async def run_milestone_loop(
        self,
        resolver: "MilestoneResolver",
        *,
        interval_seconds: float = 300.0,
    ) -> None:
        """Drive MilestoneResolver.refresh on a timer until stop() is called.

        Exceptions inside refresh are caught by the resolver itself (it logs
        and keeps old state); if something escapes, we still catch here so
        the loop never dies silently.
        """
        # Initial refresh ASAP
        await self._safe_refresh(resolver)
        while not self._stopped:
            await asyncio.sleep(interval_seconds)
            if self._stopped:
                break
            await self._safe_refresh(resolver)

    async def _safe_refresh(self, resolver: "MilestoneResolver") -> None:
        try:
            async with self._sem:
                await resolver.refresh()
        except Exception:
            logger.warning("milestone_loop_iteration_failed", exc_info=True)
```

Also add the forward-ref import inside `TYPE_CHECKING` block at top:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from talos.milestones import MilestoneResolver
```

Merge `self._stopped = False` into the existing `__init__` (don't duplicate the function signature above — read the existing code and add the attribute initialization alongside the others).

- [ ] **Step 4: Run test — verify pass**

```bash
.venv/Scripts/python -m pytest tests/test_discovery_milestone_loop.py -v
```

Expected: 2 pass.

- [ ] **Step 5: Commit**

```bash
git add src/talos/discovery.py tests/test_discovery_milestone_loop.py
git commit -m "feat(discovery): background milestone refresh loop"
```

---

## Task 12: `Engine._resolve_event_start` cascade

**Files:**
- Modify: `src/talos/engine.py`
- Test: `tests/test_resolver_cascade.py` (new)

Pure-logic cascade. Plumbing into `_check_exit_only` comes in Task 13.

- [ ] **Step 1: Write the test**

Create `tests/test_resolver_cascade.py`:

```python
from datetime import datetime, UTC
from unittest.mock import MagicMock

import pytest

from talos.engine import TradingEngine


# A thin engine factory that installs just the collaborators the cascade needs
def _make_engine_with_collaborators():
    engine = TradingEngine.__new__(TradingEngine)
    engine._tree_metadata_store = MagicMock()
    engine._milestone_resolver = MagicMock()
    engine._game_status_resolver = MagicMock()
    return engine


class _Pair:
    def __init__(self, event_ticker: str, kalshi_event_ticker: str = ""):
        self.event_ticker = event_ticker
        self.kalshi_event_ticker = kalshi_event_ticker or event_ticker


def test_manual_opt_out_wins():
    e = _make_engine_with_collaborators()
    e._tree_metadata_store.manual_event_start.return_value = "none"
    pair = _Pair("K-1", "K")

    start, source = e._resolve_event_start("K", pair)
    assert start is None
    assert source == "manual_opt_out"


def test_manual_override_wins_over_milestone():
    e = _make_engine_with_collaborators()
    manual_dt = datetime(2026, 4, 22, 20, 0, tzinfo=UTC)
    milestone_dt = datetime(2026, 4, 22, 20, 5, tzinfo=UTC)
    e._tree_metadata_store.manual_event_start.return_value = manual_dt
    e._milestone_resolver.event_start.return_value = milestone_dt
    pair = _Pair("K-1", "K")

    start, source = e._resolve_event_start("K", pair)
    assert start == manual_dt
    assert source == "manual"


def test_milestone_used_when_no_manual():
    e = _make_engine_with_collaborators()
    e._tree_metadata_store.manual_event_start.return_value = None
    ms = datetime(2026, 4, 22, 20, 0, tzinfo=UTC)
    e._milestone_resolver.event_start.return_value = ms
    pair = _Pair("K-1", "K")

    start, source = e._resolve_event_start("K", pair)
    assert start == ms
    assert source == "milestone"


def test_sports_gsr_used_as_third_fallback():
    e = _make_engine_with_collaborators()
    e._tree_metadata_store.manual_event_start.return_value = None
    e._milestone_resolver.event_start.return_value = None

    gsr_dt = datetime(2026, 4, 20, 18, 0, tzinfo=UTC)
    gs_stub = MagicMock()
    gs_stub.scheduled_start = gsr_dt
    e._game_status_resolver.get.return_value = gs_stub
    pair = _Pair("KXNBAGAME-26APR20BOSNYR")

    start, source = e._resolve_event_start("KXNBAGAME-26APR20BOSNYR", pair)
    assert start == gsr_dt
    assert source == "sports_gsr"


def test_no_source_available_returns_none_none():
    e = _make_engine_with_collaborators()
    e._tree_metadata_store.manual_event_start.return_value = None
    e._milestone_resolver.event_start.return_value = None
    e._game_status_resolver.get.return_value = None
    pair = _Pair("K-1", "K")

    start, source = e._resolve_event_start("K", pair)
    assert start is None
    assert source is None
```

- [ ] **Step 2: Run test — expect fail**

```bash
.venv/Scripts/python -m pytest tests/test_resolver_cascade.py -v
```

- [ ] **Step 3: Add `_resolve_event_start` to `Engine`**

In `src/talos/engine.py`, add this method (near `_check_exit_only`):

```python
    def _resolve_event_start(
        self, kalshi_event_ticker: str, pair: Any
    ) -> tuple[datetime | None, str | None]:
        """Resolver cascade per spec §5.2.

        Priority: manual override → Kalshi milestone → sports GSR → nothing.

        Returns (start_time, source) where source is one of:
          - "manual_opt_out": user explicitly disabled exit-only for this event
          - "manual":         user-set override; start_time is the datetime
          - "milestone":      Kalshi milestone start_date
          - "sports_gsr":     sports provider scheduled_start
          - None:             no schedule data available
        """
        # 1. Manual (user-owned)
        if self._tree_metadata_store is not None:
            manual = self._tree_metadata_store.manual_event_start(kalshi_event_ticker)
            if manual == "none":
                return (None, "manual_opt_out")
            if manual is not None:
                return (manual, "manual")

        # 2. Kalshi milestone
        if self._milestone_resolver is not None:
            ms = self._milestone_resolver.event_start(kalshi_event_ticker)
            if ms is not None:
                return (ms, "milestone")

        # 3. Sports GSR (keyed by pair.event_ticker — sports pairs have
        #    event_ticker == kalshi_event_ticker)
        if self._game_status_resolver is not None:
            gs = self._game_status_resolver.get(pair.event_ticker)
            if gs and getattr(gs, "scheduled_start", None):
                return (gs.scheduled_start, "sports_gsr")

        return (None, None)
```

Also add the new collaborators to `__init__`:

```python
        self._tree_metadata_store: TreeMetadataStore | None = None
        self._milestone_resolver: MilestoneResolver | None = None
```

…and corresponding setter method(s) or constructor param(s) so they can be wired from `__main__.py`. Choose the pattern that matches how other collaborators (e.g., `game_status_resolver`) are passed. Looking at existing code: they're passed as constructor kwargs. Match that.

- [ ] **Step 4: Run test — verify pass**

```bash
.venv/Scripts/python -m pytest tests/test_resolver_cascade.py -v
```

Expected: 5 pass.

- [ ] **Step 5: Commit**

```bash
git add src/talos/engine.py tests/test_resolver_cascade.py
git commit -m "feat(engine): add _resolve_event_start resolver cascade"
```

---

## Task 13: `Engine._check_exit_only` rewrite with cascade + per-event dedup

**Files:**
- Modify: `src/talos/engine.py`
- Test: `tests/test_check_exit_only_cascade.py` (new)

- [ ] **Step 1: Write the test**

Create `tests/test_check_exit_only_cascade.py`. Write 4 test cases:

1. When cascade returns `manual_opt_out`, pair is NOT flipped.
2. When milestone is within lead time, all pairs sharing `kalshi_event_ticker` flip together.
3. Sports GSR `state == "live"` triggers immediate flip.
4. When no schedule source found, `exit_only_no_schedule` is logged and no flip.

(For brevity: full test bodies follow the pattern in Task 12 — instantiate engine with mocked collaborators, populate `_scanner.pairs`, call `_check_exit_only`, assert on `_exit_only_events`.)

```python
from datetime import datetime, timedelta, UTC
from unittest.mock import MagicMock

from talos.engine import TradingEngine


def _engine_with_scanner(pairs):
    e = TradingEngine.__new__(TradingEngine)
    e._tree_metadata_store = MagicMock()
    e._milestone_resolver = MagicMock()
    e._game_status_resolver = MagicMock()
    e._exit_only_events = set()
    e._stale_candidates = set()
    e._game_started_events = set()
    e._log_once_keys = set()
    scanner = MagicMock()
    scanner.pairs = pairs
    e._scanner = scanner
    e._auto_config = MagicMock(exit_only_minutes=30.0)
    # _flip_exit_only_for_key is implemented on the engine; stub it
    e._flip_exit_only_for_key = MagicMock(
        side_effect=lambda key, **kw: e._exit_only_events.add(key),
    )
    e._log_once = MagicMock()
    return e


class _Pair:
    def __init__(self, event_ticker, kalshi_event_ticker=""):
        self.event_ticker = event_ticker
        self.kalshi_event_ticker = kalshi_event_ticker or event_ticker


def test_manual_opt_out_prevents_flip():
    p = _Pair("K-1", "K")
    e = _engine_with_scanner([p])
    e._tree_metadata_store.manual_event_start.return_value = "none"
    e._check_exit_only()
    assert "K" not in e._exit_only_events


def test_milestone_within_lead_flips_all_sibling_pairs():
    p1 = _Pair("K-1", "K")
    p2 = _Pair("K-2", "K")
    e = _engine_with_scanner([p1, p2])
    e._tree_metadata_store.manual_event_start.return_value = None
    # Start time is now + 20 min → inside the 30-min lead window
    start = datetime.now(UTC) + timedelta(minutes=20)
    e._milestone_resolver.event_start.return_value = start
    e._check_exit_only()
    # _flip_exit_only_for_key should have been called once with key="K"
    e._flip_exit_only_for_key.assert_called_once()
    assert "K" in e._exit_only_events


def test_sports_gsr_live_state_flips_immediately():
    p = _Pair("KXNBAGAME-26APR20BOSNYR")
    e = _engine_with_scanner([p])
    e._tree_metadata_store.manual_event_start.return_value = None
    e._milestone_resolver.event_start.return_value = None
    gs = MagicMock()
    gs.state = "live"
    gs.scheduled_start = datetime.now(UTC) - timedelta(minutes=5)
    e._game_status_resolver.get.return_value = gs
    e._check_exit_only()
    e._flip_exit_only_for_key.assert_called_once()


def test_no_schedule_logs_once_and_skips_flip():
    p = _Pair("K-1", "K")
    e = _engine_with_scanner([p])
    e._tree_metadata_store.manual_event_start.return_value = None
    e._milestone_resolver.event_start.return_value = None
    e._game_status_resolver.get.return_value = None
    e._check_exit_only()
    e._log_once.assert_called_once()
    e._flip_exit_only_for_key.assert_not_called()
```

- [ ] **Step 2: Run test — expect fail**

- [ ] **Step 3: Replace `_check_exit_only` body**

Locate the existing `_check_exit_only` method in `engine.py` (line ~599). Replace its body with:

```python
    def _check_exit_only(self) -> None:
        """Resolver-cascade driven auto-trigger per spec §5.2."""
        now = datetime.now(UTC)
        seen_events: set[str] = set()  # dedupe: one decision per kalshi_event_ticker

        for pair in self._scanner.pairs:
            key = pair.kalshi_event_ticker or pair.event_ticker
            if key in seen_events:
                continue
            seen_events.add(key)

            if pair.event_ticker in self._exit_only_events:
                continue

            start_time, source = self._resolve_event_start(key, pair)

            if source == "manual_opt_out":
                continue

            if source is None:
                self._log_once("exit_only_no_schedule", key)
                continue

            # Sports GSR additionally supplies live/post state — flip immediately
            if source == "sports_gsr":
                gs = self._game_status_resolver.get(pair.event_ticker)
                if gs and gs.state in ("live", "post"):
                    self._flip_exit_only_for_key(
                        key, reason=f"sports_{gs.state}",
                    )
                    continue

            lead_min = self._auto_config.exit_only_minutes
            if (start_time - now).total_seconds() < lead_min * 60:
                self._flip_exit_only_for_key(
                    key,
                    reason=source,
                    scheduled_start=start_time,
                )
```

And add these helper methods:

```python
    def _flip_exit_only_for_key(
        self,
        kalshi_event_ticker: str,
        *,
        reason: str,
        scheduled_start: datetime | None = None,
    ) -> None:
        """Flip all pairs sharing kalshi_event_ticker into exit-only together.

        For a Fed presser with 46 market-pairs, this ensures all 46 gate
        simultaneously rather than one per tick.
        """
        self._exit_only_events.add(kalshi_event_ticker)
        self._game_started_events.add(kalshi_event_ticker)
        name = self._display_name(kalshi_event_ticker)
        self._notify(
            f"EXIT-ONLY: {name} — {reason}", "warning", toast=True,
        )
        logger.info(
            "exit_only_auto_trigger",
            kalshi_event_ticker=kalshi_event_ticker,
            reason=reason,
            scheduled_start=(
                scheduled_start.isoformat() if scheduled_start else None
            ),
        )

    def _log_once(self, event_key: str, event_ticker: str) -> None:
        """Emit a structured log at most once per event_ticker per process."""
        key = (event_key, event_ticker)
        if key in self._log_once_keys:
            return
        self._log_once_keys.add(key)
        logger.info(event_key, event_ticker=event_ticker)
```

In `__init__`, add:
```python
        self._log_once_keys: set[tuple[str, str]] = set()
```

**Note:** the old `_check_exit_only` had 3 branches for pre/live/post + a preemptive branch using `_expiration_fallback`. The new version collapses all of this via the cascade. The `_expiration_fallback` path is retained as dead code (behind `tree_mode = False`) — we'll delete it in Phase 5. For Phase 1 with `tree_mode = False`, we need the OLD behavior to remain available. So:

Gate the new `_check_exit_only` behind `self._auto_config.tree_mode`:

```python
    def _check_exit_only(self) -> None:
        if self._auto_config.tree_mode:
            self._check_exit_only_tree_mode()
        else:
            self._check_exit_only_legacy()
```

Rename the existing implementation to `_check_exit_only_legacy` and place the new cascade logic inside `_check_exit_only_tree_mode`.

- [ ] **Step 4: Run test — verify pass**

```bash
.venv/Scripts/python -m pytest tests/test_check_exit_only_cascade.py -v
```

Expected: 4 pass.

- [ ] **Step 5: Full regression pass**

```bash
.venv/Scripts/python -m pytest
```

Expected: all existing tests still pass (new path gated behind `tree_mode = False` default).

- [ ] **Step 6: Commit**

```bash
git add src/talos/engine.py tests/test_check_exit_only_cascade.py
git commit -m "feat(engine): _check_exit_only cascade — gated on tree_mode"
```

---

## Task 14: `Engine.add_pairs_from_selection`

**Files:**
- Modify: `src/talos/engine.py`
- Test: `tests/test_engine_add_pairs_from_selection.py` (new)

Orchestrates the full 6-step add flow per spec §5.1 including `resolve_batch()` and volume seeding.

- [ ] **Step 1: Write the test**

Create `tests/test_engine_add_pairs_from_selection.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.engine import TradingEngine
from talos.models.strategy import ArbPair
from talos.models.tree import ArbPairRecord


def _engine_with_collaborators():
    e = TradingEngine.__new__(TradingEngine)

    # GameManager — returns a fake ArbPair when restore_game is called
    def _restore(record):
        return ArbPair(
            event_ticker=record["event_ticker"],
            ticker_a=record["ticker_a"],
            ticker_b=record["ticker_b"],
            side_a=record.get("side_a", "yes"),
            side_b=record.get("side_b", "no"),
            source=record.get("source"),
        )
    gm = MagicMock()
    gm.restore_game = MagicMock(side_effect=_restore)
    gm.subtitles = {}
    gm.volumes_24h = {}
    gm._volumes_24h = gm.volumes_24h
    # suppress_on_change: context manager that does nothing
    from contextlib import contextmanager
    @contextmanager
    def _suppress():
        yield
    gm.suppress_on_change = MagicMock(side_effect=_suppress)
    e._game_manager = gm

    e._adjuster = MagicMock()
    e._game_status_resolver = MagicMock()
    e._game_status_resolver.resolve_batch = AsyncMock(return_value={})
    e._game_status_resolver.get = MagicMock(return_value=None)
    e._feed = MagicMock()
    e._feed.subscribe = AsyncMock()
    e._data_collector = None
    e._persist_active_games = MagicMock()
    return e


@pytest.mark.asyncio
async def test_add_pairs_wires_adjuster_gsr_feeds_and_persists():
    e = _engine_with_collaborators()
    r = ArbPairRecord(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
        kalshi_event_ticker="KXFEDMENTION-26APR",
        series_ticker="KXFEDMENTION",
        category="Mentions",
    )
    pairs = await e.add_pairs_from_selection([r.model_dump()])
    assert len(pairs) == 1
    e._adjuster.add_event.assert_called_once()
    e._game_status_resolver.resolve_batch.assert_awaited_once()
    e._feed.subscribe.assert_awaited()  # at least one subscribe call
    e._persist_active_games.assert_called_once()


@pytest.mark.asyncio
async def test_add_pairs_seeds_volume_from_record():
    e = _engine_with_collaborators()
    r = ArbPairRecord(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
        kalshi_event_ticker="KXFEDMENTION-26APR",
        series_ticker="KXFEDMENTION",
        category="Mentions",
        volume_24h_a=500,
        volume_24h_b=500,
    )
    await e.add_pairs_from_selection([r.model_dump()])
    assert e._game_manager._volumes_24h.get("KXFEDMENTION-26APR-YIEL") == 500


@pytest.mark.asyncio
async def test_add_pairs_sports_calls_resolve_batch_with_subtitles():
    e = _engine_with_collaborators()
    e._game_manager.subtitles = {"KXNBAGAME-26APR20BOSNYR": "BOS at NYR"}
    r = ArbPairRecord(
        event_ticker="KXNBAGAME-26APR20BOSNYR",
        ticker_a="KXNBAGAME-26APR20BOSNYR-BOS",
        ticker_b="KXNBAGAME-26APR20BOSNYR-NYR",
        kalshi_event_ticker="KXNBAGAME-26APR20BOSNYR",
        series_ticker="KXNBAGAME",
        category="Sports",
        side_a="no",
        side_b="no",
    )
    await e.add_pairs_from_selection([r.model_dump()])
    args, kwargs = e._game_status_resolver.resolve_batch.call_args
    # First positional arg is the batch list [(event_ticker, subtitle), ...]
    batch = args[0]
    assert batch == [("KXNBAGAME-26APR20BOSNYR", "BOS at NYR")]


@pytest.mark.asyncio
async def test_add_pairs_persists_only_once_at_batch_end():
    e = _engine_with_collaborators()
    records = [
        ArbPairRecord(
            event_ticker=f"KX-{i}",
            ticker_a=f"KX-{i}",
            ticker_b=f"KX-{i}",
            kalshi_event_ticker=f"KX-{i}",
            series_ticker="KX",
            category="Mentions",
        ).model_dump()
        for i in range(5)
    ]
    await e.add_pairs_from_selection(records)
    e._persist_active_games.assert_called_once()
```

- [ ] **Step 2: Run test — expect fail**

- [ ] **Step 3: Implement `add_pairs_from_selection` in `Engine`**

In `src/talos/engine.py`, add:

```python
    async def add_pairs_from_selection(
        self, records: list[dict[str, Any]]
    ) -> list[ArbPair]:
        """Commit path for tree-selected pairs.

        Mirrors the 6 orchestration steps of today's add_games (line 2839):
          1. restore_game per record (inside suppress_on_change)
          1.5 seed _volumes_24h from record fields (not populated by restore_game)
          2. adjuster ledger wiring
          3. GSR set_expiration + resolve_batch
          4. feed subscribes
          5. data_collector.log_game_add
          6. persist once
        """
        pairs: list[ArbPair] = []

        # Steps 1 + 1.5: reconstitute + volume seeding, with on_change suppressed
        with self._game_manager.suppress_on_change():
            for r in records:
                try:
                    pair = self._game_manager.restore_game(
                        {**r, "source": r.get("source", "tree")},
                    )
                    if pair is None:
                        continue
                    vol_a = r.get("volume_24h_a")
                    vol_b = r.get("volume_24h_b")
                    if vol_a is not None:
                        self._game_manager._volumes_24h[pair.ticker_a] = int(vol_a)
                    if vol_b is not None and pair.ticker_b != pair.ticker_a:
                        self._game_manager._volumes_24h[pair.ticker_b] = int(vol_b)
                    pairs.append(pair)
                except Exception:
                    logger.warning(
                        "tree_add_failed",
                        pair_ticker=r.get("event_ticker"),
                        exc_info=True,
                    )

        # Step 2: adjuster
        for pair in pairs:
            self._adjuster.add_event(pair)

        # Step 3: GSR wiring + resolve_batch (populates scheduled_start NOW)
        if self._game_status_resolver is not None and pairs:
            for pair in pairs:
                self._game_status_resolver.set_expiration(
                    pair.event_ticker, pair.expected_expiration_time,
                )
            batch = [
                (
                    p.event_ticker,
                    self._game_manager.subtitles.get(p.event_ticker, ""),
                )
                for p in pairs
            ]
            await self._game_status_resolver.resolve_batch(batch)

        # Step 4: feed subscribes
        for pair in pairs:
            await self._feed.subscribe(pair.ticker_a)
            if pair.ticker_b != pair.ticker_a:
                await self._feed.subscribe(pair.ticker_b)

        # Step 5: data_collector
        if self._data_collector is not None:
            for pair in pairs:
                gs = (
                    self._game_status_resolver.get(pair.event_ticker)
                    if self._game_status_resolver
                    else None
                )
                scheduled = (
                    gs.scheduled_start.isoformat()
                    if gs and gs.scheduled_start else None
                )
                self._data_collector.log_game_add(
                    event_ticker=pair.event_ticker,
                    series_ticker=pair.series_ticker,
                    sport="",
                    league="",
                    source="tree",
                    ticker_a=pair.ticker_a,
                    ticker_b=pair.ticker_b,
                    volume_a=self._game_manager.volumes_24h.get(pair.ticker_a, 0),
                    volume_b=self._game_manager.volumes_24h.get(pair.ticker_b, 0),
                    fee_type=pair.fee_type,
                    fee_rate=pair.fee_rate,
                    scheduled_start=scheduled,
                )

        # Step 6: persist once
        self._persist_active_games()
        return pairs
```

Add `_persist_active_games` helper (if not yet present):

```python
    def _persist_active_games(self) -> None:
        """Single persist point for batch add/remove paths.

        Delegates to GameManager.on_change if it's still wired to the
        legacy _persist_games writer in __main__.py. Falls back to a
        direct save_games_full call if not.
        """
        if self._game_manager.on_change is not None:
            try:
                self._game_manager.on_change()
            except Exception:
                logger.warning("persist_active_games_failed", exc_info=True)
```

- [ ] **Step 4: Run test — verify pass**

```bash
.venv/Scripts/python -m pytest tests/test_engine_add_pairs_from_selection.py -v
```

Expected: 4 pass.

- [ ] **Step 5: Commit**

```bash
git add src/talos/engine.py tests/test_engine_add_pairs_from_selection.py
git commit -m "feat(engine): add_pairs_from_selection mirrors add_games orchestration"
```

---

## Task 15: `Engine.remove_pairs_from_selection` with `RemoveOutcome`

**Files:**
- Modify: `src/talos/engine.py`
- Test: `tests/test_engine_remove_pairs_from_selection.py` (new)

- [ ] **Step 1: Write the test**

Create `tests/test_engine_remove_pairs_from_selection.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.engine import TradingEngine


def _engine():
    e = TradingEngine.__new__(TradingEngine)
    gm = MagicMock()
    # active games keyed by pair_ticker
    gm._games = {}
    def _get_game(pt):
        return gm._games.get(pt)
    gm.get_game = MagicMock(side_effect=_get_game)
    gm.remove_game = AsyncMock()
    from contextlib import contextmanager
    @contextmanager
    def _suppress():
        yield
    gm.suppress_on_change = MagicMock(side_effect=_suppress)
    e._game_manager = gm

    e._adjuster = MagicMock()
    e._game_status_resolver = MagicMock()
    e._exit_only_events = set()
    e._stale_candidates = set()
    e._winding_down = set()
    e._persist_active_games = MagicMock()
    e.enforce_exit_only = AsyncMock()
    e._mark_engine_state = MagicMock()
    return e


@pytest.mark.asyncio
async def test_remove_clean_pair_returns_removed_outcome():
    e = _engine()
    p = MagicMock()
    p.kalshi_event_ticker = "K"
    e._game_manager._games["K-1"] = p
    # No inventory
    e._adjuster.get_ledger.return_value = None

    outcomes = await e.remove_pairs_from_selection(["K-1"])
    assert len(outcomes) == 1
    assert outcomes[0].status == "removed"
    assert outcomes[0].kalshi_event_ticker == "K"
    e._game_manager.remove_game.assert_awaited_once_with("K-1")


@pytest.mark.asyncio
async def test_remove_pair_with_inventory_returns_winding_down():
    e = _engine()
    p = MagicMock()
    p.kalshi_event_ticker = "K"
    e._game_manager._games["K-1"] = p

    ledger = MagicMock()
    ledger.has_filled_positions.return_value = True
    ledger.has_resting_orders.return_value = False
    ledger.filled_count.side_effect = lambda side: 5
    ledger.resting_count.side_effect = lambda side: 0
    e._adjuster.get_ledger.return_value = ledger

    outcomes = await e.remove_pairs_from_selection(["K-1"])
    assert outcomes[0].status == "winding_down"
    assert "K-1" in e._winding_down
    e.enforce_exit_only.assert_awaited_once_with("K-1")
    e._mark_engine_state.assert_called_once_with("K-1", "winding_down")


@pytest.mark.asyncio
async def test_remove_missing_pair_returns_not_found():
    e = _engine()
    outcomes = await e.remove_pairs_from_selection(["K-NONEXISTENT"])
    assert outcomes[0].status == "not_found"


@pytest.mark.asyncio
async def test_remove_batch_persists_once():
    e = _engine()
    for i in range(3):
        p = MagicMock()
        p.kalshi_event_ticker = "K"
        e._game_manager._games[f"K-{i}"] = p
    e._adjuster.get_ledger.return_value = None

    await e.remove_pairs_from_selection(["K-0", "K-1", "K-2"])
    e._persist_active_games.assert_called_once()
```

- [ ] **Step 2: Run test — expect fail**

- [ ] **Step 3: Implement `remove_pairs_from_selection` + `_mark_engine_state`**

In `src/talos/engine.py` add:

```python
    async def remove_pairs_from_selection(
        self, pair_tickers: list[str],
    ) -> list[RemoveOutcome]:
        """Commit path for tree-unticked pairs.

        Returns per-pair RemoveOutcome so TreeScreen can decide per-event
        whether to set deliberately_unticked, defer, or retry.
        """
        outcomes: list[RemoveOutcome] = []

        with self._game_manager.suppress_on_change():
            for pt in pair_tickers:
                pair = self._game_manager.get_game(pt)
                if pair is None:
                    outcomes.append(RemoveOutcome(
                        pair_ticker=pt,
                        kalshi_event_ticker="",
                        status="not_found",
                    ))
                    continue
                kalshi_et = pair.kalshi_event_ticker or pair.event_ticker

                try:
                    ledger = self._adjuster.get_ledger(pt)
                    has_inventory = ledger and (
                        ledger.has_filled_positions()
                        or ledger.has_resting_orders()
                    )

                    if has_inventory:
                        self._winding_down.add(pt)
                        await self.enforce_exit_only(pt)
                        self._mark_engine_state(pt, "winding_down")
                        reason = (
                            f"filled={ledger.filled_count(0)},"
                            f"{ledger.filled_count(1)} "
                            f"resting={ledger.resting_count(0)},"
                            f"{ledger.resting_count(1)}"
                        )
                        logger.info("winding_down_started",
                                    pair_ticker=pt, reason=reason)
                        outcomes.append(RemoveOutcome(
                            pair_ticker=pt,
                            kalshi_event_ticker=kalshi_et,
                            status="winding_down",
                            reason=reason,
                        ))
                        continue

                    # Clean removal (reverse of add flow)
                    self._exit_only_events.discard(pt)
                    self._stale_candidates.discard(pt)
                    if self._game_status_resolver is not None:
                        self._game_status_resolver.remove(pt)
                    self._adjuster.remove_event(pt)
                    await self._game_manager.remove_game(pt)
                    outcomes.append(RemoveOutcome(
                        pair_ticker=pt,
                        kalshi_event_ticker=kalshi_et,
                        status="removed",
                    ))
                except Exception as e:
                    logger.warning(
                        "tree_remove_failed",
                        pair_ticker=pt, exc_info=True,
                    )
                    outcomes.append(RemoveOutcome(
                        pair_ticker=pt,
                        kalshi_event_ticker=kalshi_et,
                        status="failed",
                        reason=str(e),
                    ))

        self._persist_active_games()
        return outcomes

    def _mark_engine_state(self, pair_ticker: str, state: str) -> None:
        """Set per-pair engine_state on the ArbPair in GameManager._games
        so the next _persist_games write picks it up."""
        pair = self._game_manager.get_game(pair_ticker)
        if pair is not None:
            pair.engine_state = state
```

Add `_winding_down` to `__init__`:
```python
        self._winding_down: set[str] = set()
```

And adjust the signature of `filled_count` / `resting_count` — in the test I used ints (0/1) as side keys. Check the actual Side enum in the codebase and use it if the ledger expects `Side.A`/`Side.B`. Adjust test accordingly.

- [ ] **Step 4: Run test — verify pass**

```bash
.venv/Scripts/python -m pytest tests/test_engine_remove_pairs_from_selection.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/talos/engine.py tests/test_engine_remove_pairs_from_selection.py
git commit -m "feat(engine): remove_pairs_from_selection returns RemoveOutcome list"
```

---

## Task 16: `engine_state`-aware restore + winding-down re-entry

**Files:**
- Modify: `src/talos/engine.py` (the `_setup_initial_games` / startup restore loop)
- Test: `tests/test_engine_restore_with_state.py` (new)

- [ ] **Step 1: Write the test**

Create `tests/test_engine_restore_with_state.py`:

```python
from unittest.mock import MagicMock, AsyncMock

import pytest

# Test the post-restore state-adjustment logic in isolation.
# Scenario: games_full.json has a record with engine_state="winding_down".
# After GameManager.restore_game reconstitutes it, the engine's post-restore
# loop must re-add the pair to _winding_down and _exit_only_events.


def test_apply_persisted_engine_state_winding_down():
    from talos.engine import TradingEngine
    e = TradingEngine.__new__(TradingEngine)
    e._winding_down = set()
    e._exit_only_events = set()

    # Simulate what restore_game gave us
    pair = MagicMock()
    pair.event_ticker = "K-1"
    pair.engine_state = "winding_down"

    e._apply_persisted_engine_state(pair)

    assert "K-1" in e._winding_down
    assert "K-1" in e._exit_only_events


def test_apply_persisted_engine_state_exit_only():
    from talos.engine import TradingEngine
    e = TradingEngine.__new__(TradingEngine)
    e._winding_down = set()
    e._exit_only_events = set()
    pair = MagicMock()
    pair.event_ticker = "K-1"
    pair.engine_state = "exit_only"
    e._apply_persisted_engine_state(pair)
    assert "K-1" in e._exit_only_events
    assert "K-1" not in e._winding_down


def test_apply_persisted_engine_state_active_is_noop():
    from talos.engine import TradingEngine
    e = TradingEngine.__new__(TradingEngine)
    e._winding_down = set()
    e._exit_only_events = set()
    pair = MagicMock()
    pair.event_ticker = "K-1"
    pair.engine_state = "active"
    e._apply_persisted_engine_state(pair)
    assert not e._winding_down
    assert not e._exit_only_events
```

- [ ] **Step 2: Run test — expect fail**

- [ ] **Step 3: Add `_apply_persisted_engine_state` to `Engine`**

```python
    def _apply_persisted_engine_state(self, pair: ArbPair) -> None:
        """Apply a pair's persisted engine_state after restore.

        Winding-down pairs re-enter _winding_down + _exit_only_events so the
        next tick immediately applies exit-only behavior — preventing the
        SURVIVOR-class failure mode where a crash during wind-down could
        result in the pair resuming normal trading after restart.
        """
        state = getattr(pair, "engine_state", "active")
        if state == "winding_down":
            self._winding_down.add(pair.event_ticker)
            self._exit_only_events.add(pair.event_ticker)
            logger.info(
                "winding_down_restored",
                pair_ticker=pair.event_ticker,
            )
        elif state == "exit_only":
            self._exit_only_events.add(pair.event_ticker)
            logger.info(
                "exit_only_restored",
                pair_ticker=pair.event_ticker,
            )
```

In the existing startup restore loop (search for `restore_game` in `engine.py` — around line 896 in `_setup_initial_games`), after each successful restore call:

```python
                    pair = self._game_manager.restore_game(data)
                    # ... existing ledger/volume restoration ...
                    self._apply_persisted_engine_state(pair)  # NEW
                    pairs.append(pair)
```

Place the call after ledger seeding and volume restoration, before `pairs.append(pair)`.

- [ ] **Step 4: Run test — verify pass**

- [ ] **Step 5: Commit**

```bash
git add src/talos/engine.py tests/test_engine_restore_with_state.py
git commit -m "feat(engine): restore path honors persisted engine_state"
```

---

## Task 17: `event_fully_removed` emission + winding-down reconciliation

**Files:**
- Modify: `src/talos/engine.py`
- Test: `tests/test_winding_reconciliation.py` (new)

Engine watches `_winding_down` pairs each tick. When a pair's ledger clears (balanced + no resting orders), it's removed cleanly and `event_fully_removed` is emitted if the last sibling pair for that `kalshi_event_ticker` is gone.

- [ ] **Step 1: Write the test**

Create `tests/test_winding_reconciliation.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_winding_down_pair_removed_when_flat():
    from talos.engine import TradingEngine
    e = TradingEngine.__new__(TradingEngine)

    # Flat ledger
    ledger = MagicMock()
    ledger.has_filled_positions.return_value = False
    ledger.has_resting_orders.return_value = False
    e._adjuster = MagicMock()
    e._adjuster.get_ledger.return_value = ledger

    p = MagicMock()
    p.event_ticker = "K-1"
    p.kalshi_event_ticker = "K"
    gm = MagicMock()
    gm._games = {"K-1": p}
    gm.get_game.return_value = p
    e._game_manager = gm

    e._winding_down = {"K-1"}
    e._exit_only_events = {"K-1"}
    e._stale_candidates = set()
    e.remove_pairs_from_selection = AsyncMock(return_value=[
        MagicMock(status="removed", kalshi_event_ticker="K"),
    ])
    e._event_fully_removed_listeners = []
    emitted = []
    def listener(kalshi_et: str):
        emitted.append(kalshi_et)
    e._event_fully_removed_listeners.append(listener)

    await e._reconcile_winding_down()

    assert "K-1" not in e._winding_down
    e.remove_pairs_from_selection.assert_awaited_once_with(["K-1"])
    assert emitted == ["K"]


@pytest.mark.asyncio
async def test_winding_down_pair_with_inventory_stays():
    from talos.engine import TradingEngine
    e = TradingEngine.__new__(TradingEngine)

    ledger = MagicMock()
    ledger.has_filled_positions.return_value = True
    ledger.has_resting_orders.return_value = False
    e._adjuster = MagicMock()
    e._adjuster.get_ledger.return_value = ledger

    p = MagicMock()
    p.event_ticker = "K-1"
    p.kalshi_event_ticker = "K"
    gm = MagicMock()
    gm._games = {"K-1": p}
    gm.get_game.return_value = p
    e._game_manager = gm

    e._winding_down = {"K-1"}
    e._event_fully_removed_listeners = []
    e.remove_pairs_from_selection = AsyncMock()

    await e._reconcile_winding_down()

    assert "K-1" in e._winding_down  # still waiting
    e.remove_pairs_from_selection.assert_not_awaited()
```

- [ ] **Step 2: Run test — expect fail**

- [ ] **Step 3: Implement `_reconcile_winding_down` + listener API**

```python
    async def _reconcile_winding_down(self) -> None:
        """Remove winding-down pairs whose ledger has cleared.

        For each cleanly-removed pair, check if it was the last one sharing
        its kalshi_event_ticker in GameManager._games. If so, emit
        event_fully_removed to all subscribed listeners (TreeScreen uses
        this to apply deferred [·] flags).
        """
        to_check = list(self._winding_down)
        to_remove: list[str] = []
        for pt in to_check:
            ledger = self._adjuster.get_ledger(pt)
            if ledger is None:
                continue  # pair already gone somehow
            if ledger.has_filled_positions() or ledger.has_resting_orders():
                continue  # still holding inventory
            to_remove.append(pt)

        if not to_remove:
            return

        # Snapshot kalshi_event_tickers before removal so we can check siblings
        pre_removal_events: dict[str, str] = {}
        for pt in to_remove:
            p = self._game_manager.get_game(pt)
            if p is not None:
                pre_removal_events[pt] = (
                    p.kalshi_event_ticker or p.event_ticker
                )

        outcomes = await self.remove_pairs_from_selection(to_remove)
        for pt in to_remove:
            self._winding_down.discard(pt)

        # event_fully_removed if no pair for that kalshi_event_ticker remains
        removed_events = {o.kalshi_event_ticker for o in outcomes
                          if o.status == "removed"}
        for kalshi_et in removed_events:
            still_present = any(
                (p.kalshi_event_ticker or p.event_ticker) == kalshi_et
                for p in self._game_manager._games.values()
            )
            if not still_present:
                for listener in self._event_fully_removed_listeners:
                    try:
                        listener(kalshi_et)
                    except Exception:
                        logger.warning(
                            "event_fully_removed_listener_failed", exc_info=True,
                        )

    def add_event_fully_removed_listener(self, fn) -> None:
        self._event_fully_removed_listeners.append(fn)
```

Add initialization:
```python
        self._event_fully_removed_listeners: list = []
```

Wire `_reconcile_winding_down` into the refresh loop. Find where `_check_exit_only` is called (inside the main refresh cycle) and add immediately after:

```python
            if self._auto_config.tree_mode:
                await self._reconcile_winding_down()
```

- [ ] **Step 4: Run test — verify pass**

- [ ] **Step 5: Commit**

```bash
git add src/talos/engine.py tests/test_winding_reconciliation.py
git commit -m "feat(engine): event_fully_removed emission for winding-down reconciliation"
```

---

## Task 18: `ready_for_trading` startup gate

**Files:**
- Modify: `src/talos/engine.py`
- Test: `tests/test_engine_ready_for_trading.py` (new)

Per spec §5.3. Engine waits for milestones before tick loop (with 30 s fallback).

- [ ] **Step 1: Write the test**

Create `tests/test_engine_ready_for_trading.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_ready_fires_when_milestones_signal():
    from talos.engine import TradingEngine
    e = TradingEngine.__new__(TradingEngine)
    e._ready_for_trading = asyncio.Event()
    e._auto_config = MagicMock(
        tree_mode=True, startup_milestone_wait_seconds=5.0,
    )

    async def _delayed_signal():
        await asyncio.sleep(0.02)
        e._ready_for_trading.set()

    asyncio.create_task(_delayed_signal())
    await e.wait_for_ready_for_trading()
    assert e._ready_for_trading.is_set()


@pytest.mark.asyncio
async def test_ready_fires_after_hard_cap_even_without_signal():
    from talos.engine import TradingEngine
    e = TradingEngine.__new__(TradingEngine)
    e._ready_for_trading = asyncio.Event()
    e._auto_config = MagicMock(
        tree_mode=True, startup_milestone_wait_seconds=0.05,
    )

    start = asyncio.get_event_loop().time()
    await e.wait_for_ready_for_trading()
    elapsed = asyncio.get_event_loop().time() - start
    assert e._ready_for_trading.is_set()
    assert elapsed >= 0.05


@pytest.mark.asyncio
async def test_flag_off_wait_returns_immediately():
    from talos.engine import TradingEngine
    e = TradingEngine.__new__(TradingEngine)
    e._ready_for_trading = asyncio.Event()
    e._auto_config = MagicMock(tree_mode=False)
    # should not need to set the event
    await asyncio.wait_for(e.wait_for_ready_for_trading(), timeout=0.5)
```

- [ ] **Step 2: Run test — expect fail**

- [ ] **Step 3: Implement**

In `Engine.__init__` add:
```python
        self._ready_for_trading: asyncio.Event = asyncio.Event()
```

Add:
```python
    async def wait_for_ready_for_trading(self) -> None:
        """Block until the resolver cascade is armed, or a hard cap expires.

        Flag-off (tree_mode = False): return immediately (legacy behavior).
        Flag-on: await _ready_for_trading.set() OR startup_milestone_wait_seconds
        elapsed — whichever first. Emits structured log on timeout.
        """
        if not self._auto_config.tree_mode:
            return

        timeout = self._auto_config.startup_milestone_wait_seconds
        try:
            await asyncio.wait_for(self._ready_for_trading.wait(), timeout=timeout)
            logger.info("startup_gate_ready", elapsed_s=None)
        except asyncio.TimeoutError:
            self._ready_for_trading.set()  # so subsequent callers don't block
            logger.warning(
                "startup_gate_timeout",
                elapsed_s=timeout,
                exit_only_degraded=True,
            )
```

- [ ] **Step 4: Run test — verify pass**

- [ ] **Step 5: Commit**

```bash
git add src/talos/engine.py tests/test_engine_ready_for_trading.py
git commit -m "feat(engine): ready_for_trading startup gate"
```

---

## Task 19: Add Principle "Safety over speed" to `brain/principles.md`

**Files:**
- Modify: `brain/principles.md`

Pure documentation. No test.

- [ ] **Step 1: Read current principles file**

```bash
# No command needed — just mentally note the format
```

Open `brain/principles.md` and find the last-numbered principle. Append the new one directly below, using the same numbering + format conventions already present.

- [ ] **Step 2: Append principle**

At the end of `brain/principles.md`, add (adjusting the number to match whatever the next is):

```markdown
## Principle N: Safety over speed

When trading and scheduling decisions are time-sensitive, prefer delay or pause over proceeding on incomplete data. A five-second delayed decision is recoverable; a decision made with stale or missing data is not.

This applies to:
- **Startup sequencing.** Engine waits for milestone data to load before beginning the tick loop, up to a 30-second hard cap.
- **Resolver cascades.** When `_check_exit_only` has no schedule source for an event, it logs and defers rather than guessing.
- **Milestone conflicts.** When a manual override and a Kalshi milestone disagree by more than the threshold, the user is prompted rather than silently overridden.
- **Persisted engine state.** Winding-down pairs survive restarts via `engine_state = "winding_down"` in `games_full.json`, so a crash mid-wind-down doesn't result in a pair resuming normal trading post-restart.

The SURVIVOR incident of 2026-04-15 is the motivating case. Acting fast on a broken proxy (`expiration − 3h`) led to adverse-selection fills during a live broadcast. The fix is not a better heuristic — it's to gate trading on having real schedule data.
```

- [ ] **Step 3: Commit**

```bash
git add brain/principles.md
git commit -m "docs(principles): add 'Safety over speed' principle"
```

---

## Task 20: `TreeScreen` — tree widget skeleton + read-only render

**Files:**
- Create: `src/talos/ui/tree_screen.py`
- Test: `tests/test_tree_screen_skeleton.py` (new)

Read-only first. Tickboxes and commit come in later tasks.

- [ ] **Step 1: Write a smoke test for the screen**

Create `tests/test_tree_screen_skeleton.py`:

```python
import pytest
from textual.app import App

from talos.ui.tree_screen import TreeScreen


class _HarnessApp(App):
    def on_mount(self):
        self.push_screen(
            TreeScreen(discovery=None, milestones=None, metadata=None,
                       engine=None),
        )


@pytest.mark.asyncio
async def test_tree_screen_can_be_instantiated():
    app = _HarnessApp()
    async with app.run_test() as pilot:
        # Smoke: screen mounts, doesn't crash
        await pilot.pause()
        assert True
```

- [ ] **Step 2: Run test — expect fail (missing module)**

- [ ] **Step 3: Create `src/talos/ui/tree_screen.py` with minimal skeleton**

```python
"""TreeScreen — tree-driven selection surface for Talos.

This screen is pushed on top of the main monitoring view. It renders the
discovery cache as an expandable tree and lets the user stage tick/untick
changes before committing them to the Engine.

See spec §4 for UX details.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

if TYPE_CHECKING:
    from talos.discovery import DiscoveryService
    from talos.milestones import MilestoneResolver
    from talos.tree_metadata import TreeMetadataStore
    from talos.engine import TradingEngine


class TreeScreen(Screen):
    """Tree-driven selection screen."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("r", "manual_refresh", "Refresh"),
    ]

    def __init__(
        self,
        *,
        discovery: DiscoveryService | None,
        milestones: MilestoneResolver | None,
        metadata: TreeMetadataStore | None,
        engine: TradingEngine | None,
    ) -> None:
        super().__init__()
        self._discovery = discovery
        self._milestones = milestones
        self._metadata = metadata
        self._engine = engine

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static("Tree Selection (placeholder — render coming)"),
            id="tree-body",
        )
        yield Footer()

    async def action_manual_refresh(self) -> None:
        if self._discovery is not None:
            await self._discovery.bootstrap()
```

- [ ] **Step 4: Run test — verify pass**

```bash
.venv/Scripts/python -m pytest tests/test_tree_screen_skeleton.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/talos/ui/tree_screen.py tests/test_tree_screen_skeleton.py
git commit -m "feat(ui): TreeScreen skeleton with empty render"
```

---

## Task 21: `TreeScreen` — tree render from DiscoveryService cache

**Files:**
- Modify: `src/talos/ui/tree_screen.py`
- Test: `tests/test_tree_screen_render.py` (new)

Render CategoryNode → SeriesNode → EventNode nesting using Textual's built-in `Tree` widget. Markets are rendered on event-expand.

- [ ] **Step 1: Write the test**

Create `tests/test_tree_screen_render.py`:

```python
import pytest
from textual.app import App
from textual.widgets import Tree

from talos.discovery import DiscoveryService
from talos.models.tree import CategoryNode, SeriesNode, EventNode
from talos.ui.tree_screen import TreeScreen


class _HarnessApp(App):
    def __init__(self, ds):
        super().__init__()
        self._ds = ds
    def on_mount(self):
        self.push_screen(TreeScreen(
            discovery=self._ds, milestones=None, metadata=None, engine=None,
        ))


def _ds_with_one_mention():
    ds = DiscoveryService()
    s = SeriesNode(
        ticker="KXFEDMENTION", title="...", category="Mentions",
        tags=[], frequency="one_off",
    )
    ds.categories["Mentions"] = CategoryNode(
        name="Mentions", series_count=1, series={"KXFEDMENTION": s},
    )
    return ds


@pytest.mark.asyncio
async def test_tree_renders_categories_and_series():
    ds = _ds_with_one_mention()
    app = _HarnessApp(ds)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Find the Tree widget in the screen
        screen = app.screen
        tree = screen.query_one(Tree)
        labels = [str(n.label) for n in tree.root.children]
        assert any("Mentions" in lbl for lbl in labels)
```

- [ ] **Step 2: Run — expect fail (no Tree widget yet)**

- [ ] **Step 3: Replace `compose` + add `_build_tree`**

In `src/talos/ui/tree_screen.py`, replace the `compose` method and add helpers:

```python
from textual.widgets import Tree, Input
from textual.widgets.tree import TreeNode


class TreeScreen(Screen):
    # ... existing ...

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="filter", id="filter-input")
        yield Tree[dict[str, Any]]("Kalshi", id="tree")
        yield Footer()

    def on_mount(self) -> None:
        self._rebuild_tree()

    def _rebuild_tree(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.root.remove_children()
        if self._discovery is None:
            return

        for cat_name, cat in sorted(self._discovery.categories.items()):
            cat_node = tree.root.add(
                f"[ ] {cat_name}   {cat.series_count} open",
                data={"kind": "category", "name": cat_name},
                expand=False,
            )
            # Series loaded lazily on expand — add placeholder
            cat_node.add("…", data={"kind": "placeholder"})

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node: TreeNode = event.node
        data = node.data or {}
        kind = data.get("kind")
        if kind == "category":
            self._expand_category(node, data["name"])
        elif kind == "series":
            self.run_worker(self._expand_series(node, data["ticker"]))

    def _expand_category(self, node: TreeNode, category: str) -> None:
        node.remove_children()
        if self._discovery is None:
            return
        cat = self._discovery.categories.get(category)
        if not cat:
            return
        for ticker, series in sorted(cat.series.items()):
            child = node.add(
                f"[ ] {ticker}",
                data={"kind": "series", "ticker": ticker},
                expand=False,
            )
            child.add("…", data={"kind": "placeholder"})

    async def _expand_series(self, node: TreeNode, series_ticker: str) -> None:
        if self._discovery is None:
            return
        events = await self._discovery.get_events_for_series(series_ticker)
        node.remove_children()
        for event_ticker, ev in sorted(events.items()):
            child = node.add_leaf(
                f"[ ] {event_ticker}   {ev.title[:40]}",
                data={"kind": "event", "ticker": event_ticker},
            )
            _ = child  # silence unused-var warning
```

- [ ] **Step 4: Run — verify pass**

- [ ] **Step 5: Commit**

```bash
git add src/talos/ui/tree_screen.py tests/test_tree_screen_render.py
git commit -m "feat(ui): TreeScreen renders category/series/event tree"
```

---

## Task 22: `TreeScreen` — tickbox state + staged changes

**Files:**
- Modify: `src/talos/ui/tree_screen.py`
- Test: `tests/test_tree_screen_tickboxes.py` (new)

Add `space` keybinding to toggle, render `[ ]` / `[-]` / `[✓]` / `[·]` / `[W]` states, hold `StagedChanges` in memory.

- [ ] **Step 1: Write the test**

Create `tests/test_tree_screen_tickboxes.py`:

```python
import pytest
from textual.app import App
from textual.widgets import Tree

from talos.discovery import DiscoveryService
from talos.models.tree import (
    CategoryNode, EventNode, MarketNode, SeriesNode, StagedChanges,
)
from talos.ui.tree_screen import TreeScreen


def _ds_with_event_and_market():
    ds = DiscoveryService()
    s = SeriesNode(
        ticker="KXFEDMENTION", title="...", category="Mentions",
        tags=[], frequency="one_off",
    )
    ev = EventNode(
        ticker="KXFEDMENTION-26APR", series_ticker="KXFEDMENTION", title="X",
    )
    ev.markets = [
        MarketNode(ticker="KXFEDMENTION-26APR-YIEL", title="Yield"),
    ]
    s.events = {"KXFEDMENTION-26APR": ev}
    ds.categories["Mentions"] = CategoryNode(
        name="Mentions", series_count=1, series={"KXFEDMENTION": s},
    )
    return ds


@pytest.mark.asyncio
async def test_tickbox_renders_empty_by_default():
    ds = _ds_with_event_and_market()

    class _App(App):
        def on_mount(self):
            self.push_screen(
                TreeScreen(
                    discovery=ds, milestones=None, metadata=None, engine=None,
                ),
            )

    app = _App()
    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, TreeScreen)
        assert screen.staged_changes.is_empty()


@pytest.mark.asyncio
async def test_toggle_tickbox_stages_event():
    ds = _ds_with_event_and_market()

    class _App(App):
        def on_mount(self):
            self.push_screen(
                TreeScreen(
                    discovery=ds, milestones=None, metadata=None, engine=None,
                ),
            )

    app = _App()
    async with app.run_test() as pilot:
        screen = app.screen
        screen.toggle_event_by_ticker("KXFEDMENTION-26APR")
        assert len(screen.staged_changes.to_add) == 1
        assert (
            screen.staged_changes.to_add[0].kalshi_event_ticker
            == "KXFEDMENTION-26APR"
        )
```

- [ ] **Step 2: Expect fail**

- [ ] **Step 3: Add staged state + toggle helpers**

Extend `TreeScreen` with:

```python
    def __init__(self, ...):
        super().__init__()
        # ... existing ...
        self.staged_changes: StagedChanges = StagedChanges.empty()

    def toggle_event_by_ticker(self, kalshi_event_ticker: str) -> None:
        """Programmatic toggle (used by tests and by the space keybinding).

        Locates the event in the discovery cache, builds an ArbPairRecord
        per market, and stages them for addition. If the event is already
        staged, unstage it.
        """
        if self._discovery is None:
            return

        # Find the event
        event_node = None
        for cat in self._discovery.categories.values():
            for series in cat.series.values():
                if series.events is None:
                    continue
                if kalshi_event_ticker in series.events:
                    event_node = series.events[kalshi_event_ticker]
                    series_ref = series
                    cat_ref = cat
                    break
            if event_node is not None:
                break
        if event_node is None:
            return

        # Check if any of this event's markets are already staged for add
        existing = [
            r for r in self.staged_changes.to_add
            if r.kalshi_event_ticker == kalshi_event_ticker
        ]
        if existing:
            # Untoggle: remove them all
            for r in list(existing):
                self.staged_changes.to_add.remove(r)
            return

        # Toggle ON: build one ArbPairRecord per market
        from talos.models.tree import ArbPairRecord
        for mkt in event_node.markets:
            if mkt.status != "active":
                continue
            self.staged_changes.to_add.append(ArbPairRecord(
                event_ticker=mkt.ticker,
                ticker_a=mkt.ticker,
                ticker_b=mkt.ticker,
                side_a="yes",
                side_b="no",
                kalshi_event_ticker=kalshi_event_ticker,
                series_ticker=series_ref.ticker,
                category=cat_ref.name,
                fee_type=series_ref.fee_type,
                sub_title=event_node.sub_title,
                close_time=(
                    event_node.close_time.isoformat()
                    if event_node.close_time else None
                ),
                volume_24h_a=mkt.volume_24h,
                volume_24h_b=mkt.volume_24h,
            ))
```

(Full glyph rendering across tickbox states is large; for this task we only need the data structure + programmatic toggle. Visual glyphs and `space`-keybinding can be refined later. The spec's full tickbox-state rendering at §4.2 can be filled in incrementally as the screen matures.)

- [ ] **Step 4: Run — verify pass**

- [ ] **Step 5: Commit**

```bash
git add src/talos/ui/tree_screen.py tests/test_tree_screen_tickboxes.py
git commit -m "feat(ui): TreeScreen staged changes + programmatic toggle"
```

---

## Task 23: `TreeScreen` — commit flow with Engine integration

**Files:**
- Modify: `src/talos/ui/tree_screen.py`
- Test: `tests/test_tree_commit_flow.py` (new)

- [ ] **Step 1: Write the test**

Create `tests/test_tree_commit_flow.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.models.tree import (
    ArbPairRecord, RemoveOutcome, StagedChanges,
)
from talos.ui.tree_screen import TreeScreen


class _FakeEngine:
    def __init__(self):
        self.add_pairs_from_selection = AsyncMock(return_value=[])
        self.remove_pairs_from_selection = AsyncMock(return_value=[])


class _FakeMetadata:
    def __init__(self):
        self.applied: list = []
        self.cleared: list = []
    def set_deliberately_unticked(self, k): self.applied.append(k)
    def clear_deliberately_unticked(self, k): self.cleared.append(k)
    def manual_event_start(self, _): return None
    def set_manual_event_start(self, k, v): pass
    def set_deliberately_unticked_pending(self, k): pass


@pytest.mark.asyncio
async def test_commit_clean_add_triggers_engine_add():
    engine = _FakeEngine()
    screen = TreeScreen.__new__(TreeScreen)
    screen._engine = engine
    screen._metadata = _FakeMetadata()
    r = ArbPairRecord(
        event_ticker="K-1", ticker_a="K-1", ticker_b="K-1",
        kalshi_event_ticker="K", series_ticker="KX", category="Mentions",
    )
    screen.staged_changes = StagedChanges(to_add=[r])

    await screen.commit()

    engine.add_pairs_from_selection.assert_awaited_once()
    assert screen.staged_changes.is_empty()


@pytest.mark.asyncio
async def test_commit_all_removed_applies_unticked():
    engine = _FakeEngine()
    engine.remove_pairs_from_selection.return_value = [
        RemoveOutcome(pair_ticker="K-1", kalshi_event_ticker="K", status="removed"),
    ]
    md = _FakeMetadata()
    screen = TreeScreen.__new__(TreeScreen)
    screen._engine = engine
    screen._metadata = md
    screen.staged_changes = StagedChanges(
        to_remove=["K-1"],
        to_set_unticked=["K"],
    )

    await screen.commit()

    assert md.applied == ["K"]


@pytest.mark.asyncio
async def test_commit_winding_down_defers_unticked():
    engine = _FakeEngine()
    engine.remove_pairs_from_selection.return_value = [
        RemoveOutcome(
            pair_ticker="K-1", kalshi_event_ticker="K", status="winding_down",
            reason="filled=5,3",
        ),
    ]
    md = _FakeMetadata()
    screen = TreeScreen.__new__(TreeScreen)
    screen._engine = engine
    screen._metadata = md
    screen.staged_changes = StagedChanges(
        to_remove=["K-1"],
        to_set_unticked=["K"],
    )
    screen._deferred_set_unticked = set()

    await screen.commit()

    # NOT applied directly
    assert md.applied == []
    # Instead, deferred
    assert "K" in screen._deferred_set_unticked
```

- [ ] **Step 2: Expect fail**

- [ ] **Step 3: Add `commit` + `_deferred_set_unticked` to `TreeScreen`**

```python
    async def commit(self) -> None:
        """Push staged changes through Engine and reconcile metadata."""
        if self._engine is None or self._metadata is None:
            return
        staged = self.staged_changes

        # Engine add/remove
        added = []
        remove_outcomes = []
        if staged.to_add:
            added = await self._engine.add_pairs_from_selection(
                [r.model_dump() for r in staged.to_add]
            )
        if staged.to_remove:
            remove_outcomes = await self._engine.remove_pairs_from_selection(
                staged.to_remove,
            )

        # Apply deferred/applied unticked per §5.1a rules
        staged_remove_set = set(staged.to_remove)
        for k in staged.to_set_unticked:
            matching = [
                o for o in remove_outcomes
                if o.kalshi_event_ticker == k
                and o.pair_ticker in staged_remove_set
            ]
            if matching and all(o.status == "removed" for o in matching):
                self._metadata.set_deliberately_unticked(k)
            else:
                # Some pair(s) went winding_down → defer
                self._deferred_set_unticked.add(k)

        # Re-ticks clear the [·] flag immediately on success
        added_keys = {
            p.kalshi_event_ticker or p.event_ticker for p in added
        }
        for k in staged.to_clear_unticked:
            if k in added_keys:
                self._metadata.clear_deliberately_unticked(k)

        # Apply manual_event_start from staged popup
        for k, v in staged.to_set_manual_start.items():
            self._metadata.set_manual_event_start(k, v)

        # Clear staged
        self.staged_changes = StagedChanges.empty()

    def on_event_fully_removed(self, kalshi_event_ticker: str) -> None:
        """Engine listener callback: promote deferred [·] to applied."""
        if kalshi_event_ticker in self._deferred_set_unticked:
            self._metadata.promote_pending_to_applied(kalshi_event_ticker)
            self._deferred_set_unticked.discard(kalshi_event_ticker)
```

Add to `__init__`:
```python
        self._deferred_set_unticked: set[str] = set()
```

- [ ] **Step 4: Verify pass**

- [ ] **Step 5: Commit**

```bash
git add src/talos/ui/tree_screen.py tests/test_tree_commit_flow.py
git commit -m "feat(ui): TreeScreen.commit pushes staged changes through Engine"
```

---

## Task 24: `__main__.py` — wire tree-mode collaborators

**Files:**
- Modify: `src/talos/__main__.py`

Hook up `TreeMetadataStore`, `MilestoneResolver`, `DiscoveryService` into the engine on startup when `tree_mode = True`.

- [ ] **Step 1: Add wiring (no new test — integration path)**

In `src/talos/__main__.py`, after `AutomationConfig` is loaded and before `TradingEngine(...)` is instantiated, add:

```python
    # Tree-mode collaborators (only used when automation_config.tree_mode=True)
    tree_metadata_store: TreeMetadataStore | None = None
    milestone_resolver: MilestoneResolver | None = None
    discovery_service: DiscoveryService | None = None

    if automation_config.tree_mode:
        from talos.tree_metadata import TreeMetadataStore
        from talos.milestones import MilestoneResolver
        from talos.discovery import DiscoveryService

        tree_metadata_store = TreeMetadataStore()
        tree_metadata_store.load()

        milestone_resolver = MilestoneResolver()
        discovery_service = DiscoveryService(
            concurrent_limit=automation_config.discovery_concurrent_limit,
        )
```

Pass them to `TradingEngine(...)` as constructor kwargs (matching whatever pattern other collaborators use — likely `tree_metadata_store=tree_metadata_store`, etc.).

Update `TradingEngine.__init__` to accept these kwargs and store them on `self`.

After the engine is started, schedule background tasks:

```python
    if automation_config.tree_mode:
        assert discovery_service is not None
        assert milestone_resolver is not None
        asyncio.create_task(discovery_service.bootstrap())
        asyncio.create_task(
            discovery_service.run_milestone_loop(
                milestone_resolver,
                interval_seconds=automation_config.milestone_refresh_seconds,
            ),
        )

        # Wire TreeScreen ↔ Engine event_fully_removed
        # (TreeScreen instance lives inside TalosApp — wire it when pushed)
```

- [ ] **Step 2: Manual smoke**

```bash
.venv/Scripts/python -m talos
```

Expected: Talos starts normally with `tree_mode = False` (default). No regression.

- [ ] **Step 3: Commit**

```bash
git add src/talos/__main__.py src/talos/engine.py
git commit -m "feat(main): wire tree-mode collaborators into engine"
```

---

## Task 25: `TalosApp` — keybinding to push TreeScreen

**Files:**
- Modify: `src/talos/ui/app.py`

- [ ] **Step 1: Add keybinding**

Locate `TalosApp.BINDINGS` (or equivalent). Add:

```python
        ("t", "push_tree_screen", "Tree"),
```

And add the action method:

```python
    def action_push_tree_screen(self) -> None:
        from talos.ui.tree_screen import TreeScreen
        if not self._automation_config.tree_mode:
            self.notify("Tree mode disabled. Set tree_mode=True in config.")
            return
        screen = TreeScreen(
            discovery=self._discovery_service,
            milestones=self._milestone_resolver,
            metadata=self._tree_metadata_store,
            engine=self._engine,
        )
        # Wire event_fully_removed → screen.on_event_fully_removed
        self._engine.add_event_fully_removed_listener(
            screen.on_event_fully_removed,
        )
        self.push_screen(screen)
```

Ensure `TalosApp.__init__` receives the new collaborators from `__main__.py` (or reads them off the engine).

- [ ] **Step 2: Manual smoke**

Start Talos with `tree_mode = True`, press `t`, verify TreeScreen renders (possibly empty if no discovery data yet), press `escape` to return.

- [ ] **Step 3: Commit**

```bash
git add src/talos/ui/app.py
git commit -m "feat(ui): 't' keybinding pushes TreeScreen"
```

---

## Task 26: Integration test — SURVIVOR replay

**Files:**
- Create: `tests/test_survivor_replay.py`

Simulates the April 15 incident under `tree_mode = True` with a manual override, asserts no fills after exit-only trigger.

- [ ] **Step 1: Write the test**

Create `tests/test_survivor_replay.py`:

```python
"""SURVIVOR replay acceptance test.

Reproduces the 2026-04-15 scenario:
- KXSURVIVORMENTION-26APR16-MRBE market
- No Kalshi milestone
- User manually enters 2026-04-15T20:00:00-04:00 as event-start
- Engine ticks at times spanning 19:00 → 21:30 EDT

Assertion: after 19:30 EDT (30 min before event-start), pair is in
exit-only. No new fills can be accepted.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest


def test_manual_override_triggers_exit_only_at_lead_time():
    from talos.engine import TradingEngine

    e = TradingEngine.__new__(TradingEngine)
    e._tree_metadata_store = MagicMock()
    e._milestone_resolver = MagicMock()
    e._game_status_resolver = None
    e._exit_only_events = set()
    e._stale_candidates = set()
    e._game_started_events = set()
    e._log_once_keys = set()
    e._auto_config = MagicMock(exit_only_minutes=30.0, tree_mode=True)
    e._scanner = MagicMock()

    class _Pair:
        event_ticker = "KXSURVIVORMENTION-26APR16-MRBE"
        kalshi_event_ticker = "KXSURVIVORMENTION-26APR16"
    pair = _Pair()
    e._scanner.pairs = [pair]

    # Kalshi has no milestone for this event
    e._milestone_resolver.event_start.return_value = None

    # User set a manual override: Apr 15 8pm EDT = Apr 16 00:00 UTC
    manual_dt = datetime(2026, 4, 16, 0, 0)  # naive UTC for simplicity
    e._tree_metadata_store.manual_event_start.return_value = manual_dt

    # Pre-event check (20 hours before) — should NOT trigger
    import unittest.mock
    with unittest.mock.patch("talos.engine.datetime") as mock_dt:
        mock_dt.now.return_value = manual_dt - timedelta(hours=20)
        mock_dt.fromisoformat = datetime.fromisoformat
        e._flip_exit_only_for_key = MagicMock(
            side_effect=lambda k, **kw: e._exit_only_events.add(k),
        )
        e._log_once = MagicMock()
        e._check_exit_only_tree_mode()
    assert pair.kalshi_event_ticker not in e._exit_only_events

    # 29 minutes before event-start — SHOULD trigger
    with unittest.mock.patch("talos.engine.datetime") as mock_dt:
        mock_dt.now.return_value = manual_dt - timedelta(minutes=29)
        mock_dt.fromisoformat = datetime.fromisoformat
        e._check_exit_only_tree_mode()
    assert pair.kalshi_event_ticker in e._exit_only_events
```

- [ ] **Step 2: Run the test**

```bash
.venv/Scripts/python -m pytest tests/test_survivor_replay.py -v
```

Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_survivor_replay.py
git commit -m "test: SURVIVOR replay — manual override triggers exit-only at lead time"
```

---

## Task 27: Full test suite + lint + type-check gate

**Files:** none

- [ ] **Step 1: Run full test suite**

```bash
.venv/Scripts/python -m pytest
```

Expected: all tests pass, no regressions. If a pre-existing test fails, investigate per CLAUDE.md "Code Quality" — fix rather than punt.

- [ ] **Step 2: Run lint**

```bash
.venv/Scripts/python -m ruff check src/ tests/
.venv/Scripts/python -m ruff format --check src/ tests/
```

Fix any issues with `--fix` / reformat as needed.

- [ ] **Step 3: Run pyright**

```bash
.venv/Scripts/python -m pyright
```

Expected: clean. Any new `type: ignore` comments need justification.

- [ ] **Step 4: Commit lint/format fixes if any**

```bash
git add -A
git commit -m "chore: ruff + pyright clean after scanner-tree scaffold"
```

---

## Task 28: Manual Phase 2 dogfood checklist

**Files:** none — runtime validation

Per spec §7.2 Phase 2. This is NOT a committed test — it's a manual smoke walkthrough that must pass before any broader rollout.

- [ ] **Set `automation_config.tree_mode = True`** in your dev config.
- [ ] **Start Talos** (in demo environment per CLAUDE.md).
- [ ] **Press `t`** → verify TreeScreen mounts.
- [ ] **Expand a category, then a series** → verify lazy fetch happens (watch logs for `discovery_events_fetch_ok` or similar).
- [ ] **Tick a milestone-covered event** (e.g., KXTRUMPMENTION if Kalshi has curated one). Commit.
- [ ] **Verify**: engine adds the pair, main table shows it, `log_game_add` record has non-zero volume and a `scheduled_start`.
- [ ] **Tick an uncurated event** (e.g., KXSURVIVORMENTION if available). Commit → popup should appear. Enter a date/time. Commit.
- [ ] **Verify**: `tree_metadata.json` contains the manual override.
- [ ] **Untick an event with no inventory** → should disappear from main table immediately.
- [ ] **Ctrl+C Talos and restart** → verify `games_full.json` still contains all selected pairs. Verify that `source = "tree"` and `engine_state = "active"` are in the file.
- [ ] **Flip `tree_mode = False` and restart** → verify Talos still works. Verify `games_full.json` still has `source` / `engine_state` fields (legacy writer preserves them — Codex P1 fix).
- [ ] **Flip `tree_mode = True` again and restart** → verify selections persist.

Document any issues in a new spec revision before moving to Phase 3 (dual-run) or Phase 4 (default on).

---

## Deferred (Phase 5 — NOT in this plan)

- Delete `GameManager.scan_events()`
- Delete `DEFAULT_NONSPORTS_CATEGORIES`, `_nonsports_max_days`
- Delete hardcoded `volume_24h > 0` gates at [game_manager.py:559](src/talos/game_manager.py:559) and [game_manager.py:694](src/talos/game_manager.py:694)
- Delete `_expiration_fallback` path in `GameStatusResolver`
- Delete the `tree_mode` flag itself
- Delete `_check_exit_only_legacy`

These are pure-deletion work. They should be their own PR after Phase 4 (default on) has soaked for at least one full session.

---

## Plan Complete

Save location: `docs/superpowers/plans/2026-04-16-scanner-tree-redesign.md`

Spec: [`docs/superpowers/specs/2026-04-16-scanner-tree-redesign-design.md`](../specs/2026-04-16-scanner-tree-redesign-design.md)

Phase 1 scope: Tasks 1-28. All gated behind `tree_mode = False` until Phase 2 dogfood passes.
