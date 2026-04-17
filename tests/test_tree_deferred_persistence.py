"""Tests for deferred-untick persistence across TreeScreen restarts.

Covers P2-A from the Codex post-implementation review:
- commit() defers must hit TreeMetadataStore (not just the in-memory set).
- On-mount rehydrates the deferred set from metadata.
- promote_pending_to_applied clears the persisted pending state.
"""

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from talos.models.tree import RemoveOutcome, StagedChanges
from talos.tree_metadata import TreeMetadataStore
from talos.ui.tree_screen import TreeScreen


class _FakeEngine:
    def __init__(self) -> None:
        self.add_pairs_from_selection = AsyncMock(return_value=[])
        self.remove_pairs_from_selection = AsyncMock(return_value=[])


def _make_screen(engine: _FakeEngine, metadata: TreeMetadataStore) -> Any:
    screen = cast(Any, TreeScreen.__new__(TreeScreen))
    screen._engine = engine
    screen._metadata = metadata
    screen._deferred_set_unticked = set()
    screen.staged_changes = StagedChanges.empty()
    return screen


@pytest.mark.asyncio
async def test_commit_winding_down_persists_pending(tmp_path: Path) -> None:
    path = tmp_path / "tree_metadata.json"
    md = TreeMetadataStore(path=path)
    md.load()

    engine = _FakeEngine()
    engine.remove_pairs_from_selection.return_value = [
        RemoveOutcome(
            pair_ticker="K-1",
            kalshi_event_ticker="K",
            status="winding_down",
            reason="filled=5,0",
        ),
    ]
    screen = _make_screen(engine, md)
    screen.staged_changes = StagedChanges(
        to_remove=["K-1"],
        to_set_unticked=["K"],
    )

    await screen.commit()

    # Must persist across a fresh load.
    md2 = TreeMetadataStore(path=path)
    md2.load()
    assert md2.is_deliberately_unticked_pending("K")


@pytest.mark.asyncio
async def test_restart_restores_deferred_set(tmp_path: Path) -> None:
    """Simulate: user unticks → winding → restart. On screen mount,
    _deferred_set_unticked must be rehydrated from tree_metadata.json."""
    path = tmp_path / "tree_metadata.json"

    # Seed: a prior session persisted pending=["K"]
    md1 = TreeMetadataStore(path=path)
    md1.load()
    md1.set_deliberately_unticked_pending("K")
    md1.save()

    # New session: fresh screen, fresh metadata store loaded from same path.
    md2 = TreeMetadataStore(path=path)
    md2.load()
    screen = _make_screen(_FakeEngine(), md2)

    # Simulate the on_mount rehydrate step (full Textual app harness not
    # needed here — we call the helper directly).
    screen._load_persisted_deferred()

    assert "K" in screen._deferred_set_unticked


@pytest.mark.asyncio
async def test_promote_pending_clears_persisted_state(tmp_path: Path) -> None:
    """event_fully_removed → promote_pending_to_applied clears pending and
    writes to applied. Result must persist across a fresh load."""
    path = tmp_path / "tree_metadata.json"
    md = TreeMetadataStore(path=path)
    md.load()
    md.set_deliberately_unticked_pending("K")

    screen = _make_screen(_FakeEngine(), md)
    screen._deferred_set_unticked = {"K"}

    screen.on_event_fully_removed("K")

    # Fresh load should show K as APPLIED (not pending).
    md2 = TreeMetadataStore(path=path)
    md2.load()
    assert not md2.is_deliberately_unticked_pending("K")
    assert md2.is_deliberately_unticked("K")
