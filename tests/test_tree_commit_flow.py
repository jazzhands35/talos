from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from talos.models.tree import ArbPairRecord, RemoveOutcome, StagedChanges
from talos.ui.tree_screen import TreeScreen


class _FakeEngine:
    def __init__(self):
        self.add_pairs_from_selection = AsyncMock(return_value=[])
        self.remove_pairs_from_selection = AsyncMock(return_value=[])


class _FakeMetadata:
    def __init__(self):
        self.applied: list[str] = []
        self.cleared: list[str] = []
        self.promoted: list[str] = []

    def set_deliberately_unticked(self, k: str) -> None:
        self.applied.append(k)

    def clear_deliberately_unticked(self, k: str) -> None:
        self.cleared.append(k)

    def manual_event_start(self, _: str) -> None:
        return None

    def set_manual_event_start(self, k: str, v: str) -> None:
        pass

    def set_deliberately_unticked_pending(self, k: str) -> None:
        pass

    def promote_pending_to_applied(self, k: str) -> None:
        self.promoted.append(k)


def _make_screen(engine, metadata):
    screen = cast(Any, TreeScreen.__new__(TreeScreen))
    screen._engine = engine
    screen._metadata = metadata
    screen._deferred_set_unticked = set()
    screen.staged_changes = StagedChanges.empty()
    return screen


@pytest.mark.asyncio
async def test_commit_clean_add_triggers_engine_add():
    engine = _FakeEngine()
    md = _FakeMetadata()
    screen = _make_screen(engine, md)
    r = ArbPairRecord(
        event_ticker="K-1",
        ticker_a="K-1",
        ticker_b="K-1",
        kalshi_event_ticker="K",
        series_ticker="KX",
        category="Mentions",
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
    screen = _make_screen(engine, md)
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
            pair_ticker="K-1",
            kalshi_event_ticker="K",
            status="winding_down",
            reason="filled=5,3",
        ),
    ]
    md = _FakeMetadata()
    screen = _make_screen(engine, md)
    screen.staged_changes = StagedChanges(
        to_remove=["K-1"],
        to_set_unticked=["K"],
    )

    await screen.commit()

    # NOT applied directly
    assert md.applied == []
    # Instead, deferred
    assert "K" in screen._deferred_set_unticked


@pytest.mark.asyncio
async def test_event_fully_removed_promotes_deferred():
    """When engine emits event_fully_removed for a deferred event, [·] applies."""
    md = _FakeMetadata()
    screen = _make_screen(_FakeEngine(), md)
    screen._deferred_set_unticked = {"K"}

    screen.on_event_fully_removed("K")

    assert md.promoted == ["K"]
    assert "K" not in screen._deferred_set_unticked


@pytest.mark.asyncio
async def test_commit_set_manual_event_start():
    engine = _FakeEngine()
    md = _FakeMetadata()
    calls: list[tuple[str, str]] = []

    def _capture(k: str, v: str) -> None:
        calls.append((k, v))

    md.set_manual_event_start = _capture  # type: ignore[method-assign]

    screen = _make_screen(engine, md)
    screen.staged_changes = StagedChanges(
        to_set_manual_start={"K-SURV": "2026-04-22T20:00:00-04:00"},
    )

    await screen.commit()

    assert calls == [("K-SURV", "2026-04-22T20:00:00-04:00")]
