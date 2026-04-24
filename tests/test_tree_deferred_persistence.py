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

from talos.game_manager import CommitResult
from talos.models.tree import RemoveOutcome, StagedChanges
from talos.tree_metadata import TreeMetadataStore
from talos.ui.tree_screen import TreeScreen


class _FakeEngine:
    def __init__(self) -> None:
        self.add_pairs_from_selection = AsyncMock(return_value=CommitResult())
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
        to_remove=[("K-1", "K")],
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

    # Inner handler — public on_event_fully_removed marshals via
    # _app_loop.call_soon_threadsafe which requires a mounted screen.
    screen._handle_event_fully_removed("K")

    # Fresh load should show K as APPLIED (not pending).
    md2 = TreeMetadataStore(path=path)
    md2.load()
    assert not md2.is_deliberately_unticked_pending("K")
    assert md2.is_deliberately_unticked("K")


@pytest.mark.asyncio
async def test_restart_retries_stuck_pending_promotion(tmp_path: Path) -> None:
    """Round-4 review fix #1: simulates the bug Codex identified.

    Scenario:
    1. Prior session: user unticked, pair wound down, event emitted
       event_fully_removed, promotion was attempted but the metadata
       write FAILED. The pending flag stayed in the JSON file; pair
       was already removed from games_full.json.
    2. Talos crashes / restarts.
    3. On mount, the engine has restored from games_full.json. The
       event has zero engine pairs. Without restart-time reconciliation,
       no live pair would ever fire event_fully_removed again, so the
       pending flag would stay stuck forever.

    Fix: TreeScreen._retry_stuck_pending_promotions() runs on mount
    after _load_persisted_deferred(). It walks the rehydrated pending
    set and, for any ticker with no live engine pairs, calls
    _handle_event_fully_removed() to retry promotion.

    Verifies: after the simulated restart, K is APPLIED on disk."""
    path = tmp_path / "tree_metadata.json"

    # Seed the prior session's stuck state: pending=["K"] but no engine
    # pairs for K (i.e. event already fully removed before the failed
    # promotion attempt).
    md1 = TreeMetadataStore(path=path)
    md1.load()
    md1.set_deliberately_unticked_pending("K")
    md1.save()
    assert md1.is_deliberately_unticked_pending("K")
    assert not md1.is_deliberately_unticked("K")

    # New session — fresh metadata, fresh screen, engine has no pairs.
    md2 = TreeMetadataStore(path=path)
    md2.load()
    engine = _FakeEngine()

    # Important: the engine's _game_manager._games is empty (no pairs
    # for K), so _engine_pairs_for_event("K") returns []. Build a
    # minimal stub that satisfies the helper's hasattr check.
    class _FakeGM:
        _games: dict = {}

    engine._game_manager = _FakeGM()  # type: ignore[attr-defined]
    screen = _make_screen(engine, md2)

    # Simulate the on_mount flow: load deferred, then retry stuck.
    screen._load_persisted_deferred()
    assert "K" in screen._deferred_set_unticked
    screen._retry_stuck_pending_promotions()

    # Promotion should have run because event K has zero pairs.
    assert "K" not in screen._deferred_set_unticked

    # Persisted on disk now: APPLIED, pending cleared.
    md3 = TreeMetadataStore(path=path)
    md3.load()
    assert not md3.is_deliberately_unticked_pending("K")
    assert md3.is_deliberately_unticked("K")


@pytest.mark.asyncio
async def test_restart_does_not_retry_when_engine_pairs_still_present(
    tmp_path: Path,
) -> None:
    """Counterpart to the retry test: if the event STILL has live engine
    pairs (the wind-down hasn't completed yet), _retry_stuck_pending_promotions
    must NOT promote — the live event_fully_removed path will handle
    promotion when the last pair clears. Otherwise restart would
    prematurely promote pending unticks for events that are still trading."""
    path = tmp_path / "tree_metadata.json"
    md = TreeMetadataStore(path=path)
    md.load()
    md.set_deliberately_unticked_pending("K")
    md.save()

    md2 = TreeMetadataStore(path=path)
    md2.load()
    engine = _FakeEngine()

    # Engine still has a live pair for K → reconciliation must skip.
    class _FakePair:
        kalshi_event_ticker = "K"
        event_ticker = "K-1"

    class _FakeGM:
        _games = {"K-1": _FakePair()}

    engine._game_manager = _FakeGM()  # type: ignore[attr-defined]
    screen = _make_screen(engine, md2)

    screen._load_persisted_deferred()
    screen._retry_stuck_pending_promotions()

    # K stays pending because the event has live pairs — promotion
    # will happen later when the last pair fires event_fully_removed.
    assert "K" in screen._deferred_set_unticked
    md3 = TreeMetadataStore(path=path)
    md3.load()
    assert md3.is_deliberately_unticked_pending("K")
    assert not md3.is_deliberately_unticked("K")
