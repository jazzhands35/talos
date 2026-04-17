from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from talos.models.tree import ArbPairRecord, RemoveOutcome, StagedChanges
from talos.ui.schedule_popup import SchedulePopup
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
    screen._milestones = None
    screen._deferred_set_unticked = set()
    screen.staged_changes = StagedChanges.empty()
    return screen


@pytest.mark.asyncio
async def test_commit_clean_add_triggers_engine_add():
    engine = _FakeEngine()
    md = _FakeMetadata()
    # Override manual_event_start so the pre-commit schedule validator sees
    # a schedule source and skips the popup path.
    md.manual_event_start = lambda _et: "none"  # type: ignore[method-assign]
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

    completed = await screen.commit()

    assert completed is True
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

    completed = await screen.commit()

    assert completed is True
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

    completed = await screen.commit()

    assert completed is True
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

    completed = await screen.commit()

    assert completed is True
    assert calls == [("K-SURV", "2026-04-22T20:00:00-04:00")]


# ── Commit-time schedule validator (Codex P1-A) ──────────────────────────


@pytest.mark.asyncio
async def test_commit_aborts_when_needs_schedule_and_no_popup_path():
    """Simulate: staged event has no milestone, no manual override,
    no sports coverage → _events_needing_schedule returns it."""
    engine = _FakeEngine()
    md = _FakeMetadata()
    # No manual override for this event.
    md.manual_event_start = lambda _: None  # type: ignore[method-assign]
    screen = _make_screen(engine, md)
    # Milestone resolver that returns None for all events.
    screen._milestones = type(
        "FakeMs",
        (),
        {"event_start": lambda s, _: None},
    )()
    r = ArbPairRecord(
        event_ticker="KXSURVIVORMENTION-26APR23-MRBE",
        ticker_a="KXSURVIVORMENTION-26APR23-MRBE",
        ticker_b="KXSURVIVORMENTION-26APR23-MRBE",
        kalshi_event_ticker="KXSURVIVORMENTION-26APR23",
        series_ticker="KXSURVIVORMENTION",
        category="Mentions",
    )
    screen.staged_changes = StagedChanges(to_add=[r])

    needs = screen._events_needing_schedule()
    assert len(needs) == 1
    assert needs[0].kalshi_event_ticker == "KXSURVIVORMENTION-26APR23"


@pytest.mark.asyncio
async def test_events_needing_schedule_skips_events_with_manual_override():
    engine = _FakeEngine()
    md = _FakeMetadata()
    md.manual_event_start = (  # type: ignore[method-assign]
        lambda et: "2026-04-22T20:00:00-04:00" if et == "K" else None
    )
    screen = _make_screen(engine, md)
    screen._milestones = type(
        "FakeMs",
        (),
        {"event_start": lambda s, _: None},
    )()
    r = ArbPairRecord(
        event_ticker="K-1",
        ticker_a="K-1",
        ticker_b="K-1",
        kalshi_event_ticker="K",
        series_ticker="KXSURVIVORMENTION",
        category="Mentions",
    )
    screen.staged_changes = StagedChanges(to_add=[r])
    assert screen._events_needing_schedule() == []


@pytest.mark.asyncio
async def test_events_needing_schedule_skips_sports():
    engine = _FakeEngine()
    md = _FakeMetadata()
    md.manual_event_start = lambda _: None  # type: ignore[method-assign]
    screen = _make_screen(engine, md)
    screen._milestones = type(
        "FakeMs",
        (),
        {"event_start": lambda s, _: None},
    )()
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
    screen.staged_changes = StagedChanges(to_add=[r])
    # Sports GSR-covered prefix → no manual entry required.
    assert screen._events_needing_schedule() == []


@pytest.mark.asyncio
async def test_events_needing_schedule_skips_milestone_covered():
    engine = _FakeEngine()
    md = _FakeMetadata()
    md.manual_event_start = lambda _: None  # type: ignore[method-assign]
    screen = _make_screen(engine, md)
    fake_ms = type(
        "FakeMs",
        (),
        {
            "event_start": lambda s, et: (
                datetime(2026, 4, 29, 18, 30, tzinfo=UTC) if et == "KXFEDMENTION-26APR" else None
            ),
        },
    )()
    screen._milestones = fake_ms
    r = ArbPairRecord(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
        kalshi_event_ticker="KXFEDMENTION-26APR",
        series_ticker="KXFEDMENTION",
        category="Mentions",
    )
    screen.staged_changes = StagedChanges(to_add=[r])
    assert screen._events_needing_schedule() == []


@pytest.mark.asyncio
async def test_commit_applies_manual_event_start_before_engine_add():
    """to_set_manual_start entries must be persisted to TreeMetadataStore
    BEFORE Engine.add_pairs_from_selection is called, so the first tick
    sees the override."""
    engine = _FakeEngine()
    md = _FakeMetadata()
    screen = _make_screen(engine, md)

    # Populate staged as if the popup filled it in.
    screen.staged_changes = StagedChanges(
        to_add=[
            ArbPairRecord(
                event_ticker="K-1",
                ticker_a="K-1",
                ticker_b="K-1",
                kalshi_event_ticker="K",
                series_ticker="KXSURVIVORMENTION",
                category="Mentions",
            )
        ],
        to_set_manual_start={"K": "2026-04-22T20:00:00-04:00"},
    )

    # Track call order.
    order: list[str] = []

    def _set_start(k: str, _v: str) -> None:
        order.append(f"set_manual:{k}")

    md.set_manual_event_start = _set_start  # type: ignore[method-assign]

    async def _add(_records):
        order.append("engine_add")
        return []

    engine.add_pairs_from_selection = _add  # type: ignore[method-assign]

    # Ensure the popup path is NOT triggered — the override is treated as
    # already satisfying the schedule requirement.
    md.manual_event_start = (  # type: ignore[method-assign]
        lambda et: "2026-04-22T20:00:00-04:00" if et == "K" else None
    )

    completed = await screen.commit()
    assert completed is True
    # Manual start must be applied before engine add.
    assert order.index("set_manual:K") < order.index("engine_add")


def test_schedule_popup_rejects_naive_datetime():
    with pytest.raises(ValueError):
        SchedulePopup._parse_aware_datetime("2026-04-22T20:00:00")

    parsed = SchedulePopup._parse_aware_datetime("2026-04-22T20:00:00-04:00")
    assert parsed.tzinfo is not None


@pytest.mark.asyncio
async def test_action_commit_changes_does_not_announce_success_on_cancel():
    screen = cast(Any, TreeScreen.__new__(TreeScreen))
    screen.staged_changes = StagedChanges(
        to_add=[
            ArbPairRecord(
                event_ticker="K-1",
                ticker_a="K-1",
                ticker_b="K-1",
                kalshi_event_ticker="K",
                series_ticker="KXSURVIVORMENTION",
                category="Mentions",
            )
        ]
    )

    notifications: list[tuple[str, str]] = []
    rebuilds: list[str] = []

    async def _commit() -> bool:
        return False

    screen.commit = _commit  # type: ignore[method-assign]
    screen.notify = lambda msg, severity="information": notifications.append(  # type: ignore[method-assign]
        (msg, severity)
    )
    screen._rebuild_tree = lambda: rebuilds.append("rebuilt")  # type: ignore[method-assign]

    await screen.action_commit_changes()

    assert notifications == []
    assert rebuilds == []
