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
        to_remove=[("K-1", "K")],
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
        to_remove=[("K-1", "K")],
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

    # Call the inner handler directly — the public on_event_fully_removed
    # marshals via _app_loop.call_soon_threadsafe (round-7 plan Fix #3),
    # which requires a mounted screen. _handle_event_fully_removed has
    # the same body but skips the marshaling.
    screen._handle_event_fully_removed("K")

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

    # action_commit_changes dispatches to a Textual worker; in an unmounted
    # test context we bypass the wrapper and drive the coroutine it would
    # schedule. Same logic — just without Textual's runtime.
    await screen._commit_worker()

    assert notifications == []
    assert rebuilds == []


def test_commit_in_flight_cleared_on_synchronous_run_worker_failure():
    """Codex round 3: _commit_in_flight is set BEFORE run_worker runs, and
    only cleared inside the worker's finally. If run_worker raises
    synchronously (screen unmounted, app shutting down), the worker never
    runs and the flag stays True forever — every subsequent commit is
    rejected as 'already in progress'. The fix wraps run_worker in
    try/except and clears the flag on synchronous failure."""
    screen = cast(Any, TreeScreen.__new__(TreeScreen))
    screen.staged_changes = StagedChanges(
        to_add=[
            ArbPairRecord(
                event_ticker="K-1",
                ticker_a="K-1",
                ticker_b="K-1",
                kalshi_event_ticker="K",
                series_ticker="KX",
                category="Mentions",
            )
        ]
    )
    notifications: list[tuple[str, str]] = []
    screen.notify = lambda msg, severity="information": notifications.append(  # type: ignore[method-assign]
        (msg, severity)
    )

    def _failing_run_worker(_coro):
        # Close the coroutine we'll never await to avoid the
        # "coroutine was never awaited" RuntimeWarning, then raise.
        _coro.close()
        raise RuntimeError("screen unmounted")

    screen.run_worker = _failing_run_worker  # type: ignore[method-assign]
    screen._commit_in_flight = False

    with pytest.raises(RuntimeError):
        screen.action_commit_changes()

    # Crucial: flag must be cleared so the next 'c' press isn't permanently
    # locked out.
    assert screen._commit_in_flight is False
    # And the user gets a toast explaining why nothing happened.
    assert any("could not start" in m for m, _ in notifications)


# ─── Round-1 review fix: metadata-failure preserves staging ───────────────
# Codex round-1 finding #1 (HIGH): commit() metadata-write failures used
# to clear staging and return True, silently losing the user's intent.
# The fix is return False and preserve staging on PersistenceError so the
# user can retry (engine ops are idempotent — removed pairs return
# not_found, adds become no-ops).


class _AppStub:
    """Minimal stub for screen.app — Textual's MessagePump.app is a
    read-only property, so tests patch it via monkeypatch on the class."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []

    def notify(self, msg: str, severity: str = "information") -> None:
        self.notifications.append((msg, severity))


@pytest.mark.asyncio
async def test_commit_set_deliberately_unticked_failure_preserves_staging(
    monkeypatch,
):
    """Round-1 review fix: when set_deliberately_unticked raises
    PersistenceError, commit() must return False AND keep staged_changes
    intact. Returning True with empty staging would silently drop the
    user's untick intent while engine state was already mutated."""
    from talos.persistence_errors import PersistenceError

    engine = _FakeEngine()
    engine.remove_pairs_from_selection.return_value = [
        RemoveOutcome(
            pair_ticker="K-1",
            kalshi_event_ticker="K",
            status="removed",
            reason="clean",
        ),
    ]
    md = _FakeMetadata()

    def _boom(_k: str) -> None:
        raise PersistenceError("disk full")

    md.set_deliberately_unticked = _boom  # type: ignore[method-assign]
    screen = _make_screen(engine, md)
    app_stub = _AppStub()
    monkeypatch.setattr(TreeScreen, "app", property(lambda _self: app_stub))
    original = StagedChanges(
        to_remove=[("K-1", "K")],
        to_set_unticked=["K"],
    )
    screen.staged_changes = original

    completed = await screen.commit()

    # Contract: return False AND preserve staging.
    assert completed is False
    assert not screen.staged_changes.is_empty()
    assert screen.staged_changes.to_set_unticked == ["K"]
    # User must see a retry-instructive toast.
    assert any(
        "press 'c' again" in m or "re-commit" in m
        for m, _ in app_stub.notifications
    )


@pytest.mark.asyncio
async def test_commit_clear_deliberately_unticked_failure_preserves_staging(
    monkeypatch,
):
    """Symmetric to the set_deliberately_unticked test: clear failures
    must also return False + preserve staging so re-commit re-attempts
    the metadata write after the disk issue is fixed."""
    from talos.persistence_errors import PersistenceError

    engine = _FakeEngine()
    # Engine-add succeeds → triggers the clear_deliberately_unticked branch.
    added_record = ArbPairRecord(
        event_ticker="K-1",
        ticker_a="K-1",
        ticker_b="K-1",
        kalshi_event_ticker="K",
        series_ticker="KX",
        category="Mentions",
    )
    engine.add_pairs_from_selection.return_value = [added_record]
    md = _FakeMetadata()
    md.manual_event_start = lambda _et: "none"  # type: ignore[method-assign]

    def _boom(_k: str) -> None:
        raise PersistenceError("disk full")

    md.clear_deliberately_unticked = _boom  # type: ignore[method-assign]
    screen = _make_screen(engine, md)
    app_stub = _AppStub()
    monkeypatch.setattr(TreeScreen, "app", property(lambda _self: app_stub))
    original = StagedChanges(
        to_add=[added_record],
        to_clear_unticked=["K"],
    )
    screen.staged_changes = original

    completed = await screen.commit()

    assert completed is False
    assert not screen.staged_changes.is_empty()
    assert screen.staged_changes.to_clear_unticked == ["K"]
    assert any(
        "press 'c' again" in m or "re-commit" in m
        for m, _ in app_stub.notifications
    )


@pytest.mark.asyncio
async def test_commit_pending_write_failure_does_not_leak_in_memory_marker(
    monkeypatch,
):
    """Round-2 review fix #1: when set_deliberately_unticked_pending()
    raises PersistenceError during commit() (winding_down branch), the
    in-memory _deferred_set_unticked must NOT contain the event ticker.
    Otherwise a subsequent event_fully_removed would promote the event
    to applied even though the pending flag never made it to disk —
    violating memory↔disk consistency.

    The fix orders the writes disk-first: set_deliberately_unticked_pending(k)
    is called BEFORE _deferred_set_unticked.add(k). If the metadata
    write raises, the in-memory mutation never happens.

    Then we also assert that on_event_fully_removed() is a no-op for K
    after the failure — proving the integrity gate holds end-to-end."""
    from talos.persistence_errors import PersistenceError

    engine = _FakeEngine()
    # winding_down outcome → triggers the deferred branch, not the
    # immediate set_deliberately_unticked branch.
    engine.remove_pairs_from_selection.return_value = [
        RemoveOutcome(
            pair_ticker="K-1",
            kalshi_event_ticker="K",
            status="winding_down",
            reason="filled=5,3",
        ),
    ]
    md = _FakeMetadata()

    def _boom_pending(_k: str) -> None:
        raise PersistenceError("disk full")

    md.set_deliberately_unticked_pending = _boom_pending  # type: ignore[method-assign]
    screen = _make_screen(engine, md)
    app_stub = _AppStub()
    monkeypatch.setattr(TreeScreen, "app", property(lambda _self: app_stub))
    screen.staged_changes = StagedChanges(
        to_remove=[("K-1", "K")],
        to_set_unticked=["K"],
    )

    completed = await screen.commit()

    # Contract 1: commit() returns False, staging preserved.
    assert completed is False
    assert not screen.staged_changes.is_empty()
    # Contract 2: _deferred_set_unticked must NOT contain "K".
    # This is the bug Codex found — pre-fix, the in-memory add ran
    # BEFORE the metadata write, leaking the marker on failure.
    assert "K" not in screen._deferred_set_unticked
    # Contract 3: end-to-end integrity — on_event_fully_removed for K
    # must NOT promote anything because nothing is deferred.
    screen._handle_event_fully_removed("K")
    assert md.promoted == []


@pytest.mark.asyncio
async def test_on_event_fully_removed_marshals_via_call_soon_threadsafe():
    """Round-1 review fix #2 + plan Fix #3: the public listener must
    marshal onto the captured loop via call_soon_threadsafe rather than
    invoking _handle_event_fully_removed inline. Otherwise an engine
    callback originating off the Textual loop would mutate UI state on
    the wrong thread.

    Test technique: install a fake _app_loop with a recording
    call_soon_threadsafe; assert the inner handler is enqueued, not run
    inline."""
    md = _FakeMetadata()
    screen = _make_screen(_FakeEngine(), md)
    screen._deferred_set_unticked = {"K"}

    enqueued: list[tuple[Any, tuple[Any, ...]]] = []

    class _FakeLoop:
        def call_soon_threadsafe(self, fn, *args):
            enqueued.append((fn, args))

    screen._app_loop = _FakeLoop()

    # Public entry point — must enqueue, not call inline.
    screen.on_event_fully_removed("K")

    # Enqueued exactly once with the inner handler + ticker.
    assert len(enqueued) == 1
    fn, args = enqueued[0]
    assert fn == screen._handle_event_fully_removed
    assert args == ("K",)
    # Crucially: NOT promoted yet (inner handler hasn't run).
    assert md.promoted == []
    assert "K" in screen._deferred_set_unticked


def test_on_mount_registers_listener_before_rebuild_tree():
    """Round-1 review fix #2: listener registration must happen
    immediately after _app_loop capture and BEFORE _rebuild_tree() /
    _load_persisted_deferred(). Otherwise _reconcile_winding_down
    triggered during mount could fire event_fully_removed before any
    listener exists, leaving a persisted pending flag stuck.

    Strategy: instrument _rebuild_tree and _load_persisted_deferred so
    we record the order of (register-listener vs rebuild). Listener
    registration must come first."""
    import asyncio

    screen = cast(Any, TreeScreen.__new__(TreeScreen))
    order: list[str] = []

    class _FakeEngineRecording:
        def add_event_fully_removed_listener(self, _cb):
            order.append("register")

    screen._engine = _FakeEngineRecording()
    screen._discovery = None
    screen._categories_seen = True
    screen._counts_seen = True
    screen._bootstrap_polls = 0
    # _retry_stuck_pending_promotions early-returns when _metadata is None
    # (round-4 review fix #1). Setting it here keeps this ordering test
    # focused on listener registration without dragging in metadata setup.
    screen._metadata = None
    screen._deferred_set_unticked = set()
    screen._rebuild_tree = lambda: order.append("rebuild")  # type: ignore[method-assign]
    screen._load_persisted_deferred = lambda: order.append("load_deferred")  # type: ignore[method-assign]
    screen._any_counts_populated = lambda: True  # type: ignore[method-assign]
    screen.set_interval = lambda *_a, **_kw: None  # type: ignore[method-assign]

    # query_one(...).focus() — return a stub with a no-op focus.
    class _FakeTree:
        def focus(self):
            order.append("focus")

    screen.query_one = lambda *_a, **_kw: _FakeTree()  # type: ignore[method-assign]

    # on_mount uses asyncio.get_running_loop() — needs a real loop.
    async def _runner() -> None:
        screen.on_mount()

    asyncio.run(_runner())

    # Listener must register BEFORE rebuild_tree and load_persisted_deferred.
    assert "register" in order
    register_idx = order.index("register")
    assert register_idx < order.index("rebuild")
    assert register_idx < order.index("load_deferred")


# ─── Hurricane bug fix: commit triggers refresh_volumes ───────────────────


@pytest.mark.asyncio
async def test_commit_triggers_refresh_volumes_on_added_pairs():
    """2026-04-19 hurricane bug, half 2: even after fixing refresh_volumes
    to use /markets, freshly-added pairs would still show 0 volume for
    up to 60 minutes (the next hourly refresh tick). commit() must
    trigger an immediate refresh_volumes after a successful add so the
    operator sees real volume within seconds."""
    engine = _FakeEngine()
    engine.refresh_volumes = AsyncMock(return_value=None)
    added_record = ArbPairRecord(
        event_ticker="K-1",
        ticker_a="K-1",
        ticker_b="K-1",
        kalshi_event_ticker="K",
        series_ticker="KX",
        category="Mentions",
    )
    engine.add_pairs_from_selection = AsyncMock(return_value=[added_record])
    md = _FakeMetadata()
    md.manual_event_start = lambda _et: "none"  # type: ignore[method-assign]
    screen = _make_screen(engine, md)
    screen.staged_changes = StagedChanges(to_add=[added_record])

    completed = await screen.commit()
    assert completed is True

    # Yield control so the create_task() scheduled refresh runs.
    import asyncio as _asyncio
    await _asyncio.sleep(0)

    engine.refresh_volumes.assert_awaited()


@pytest.mark.asyncio
async def test_commit_does_not_trigger_refresh_when_no_adds():
    """Symmetric: a remove-only commit (no to_add) should NOT trigger a
    refresh — there are no new tickers needing volume seeding, and an
    extra refresh just burns rate-limit budget."""
    engine = _FakeEngine()
    engine.refresh_volumes = AsyncMock(return_value=None)
    engine.remove_pairs_from_selection = AsyncMock(return_value=[
        RemoveOutcome(
            pair_ticker="K-1",
            kalshi_event_ticker="K",
            status="removed",
            reason="clean",
        ),
    ])
    md = _FakeMetadata()
    screen = _make_screen(engine, md)
    screen.staged_changes = StagedChanges(
        to_remove=[("K-1", "K")],
        to_set_unticked=["K"],
    )

    completed = await screen.commit()
    assert completed is True
    import asyncio as _asyncio
    await _asyncio.sleep(0)

    engine.refresh_volumes.assert_not_awaited()


# ─── Round-5 review fix #1: commit() rejects engine 'failed' outcomes ────


@pytest.mark.asyncio
async def test_commit_failed_remove_outcome_preserves_staging_and_skips_metadata(
    monkeypatch,
):
    """Round-5 review fix #1: TradingEngine.remove_pairs_from_selection()
    converts non-persistence per-pair errors into RemoveOutcome(status=
    'failed') and continues. Without an explicit gate in commit(), the
    failed pair would fall through to the deferred branch — pending
    untick written to disk, staging cleared, return True, "Commit
    complete." toast — even though the pair is still live in
    GameManager and trading. The fix scans remove_outcomes for any
    status=='failed' before metadata application; if found, return
    False, preserve staging, do NOT write any untick metadata."""
    engine = _FakeEngine()
    engine.remove_pairs_from_selection.return_value = [
        RemoveOutcome(
            pair_ticker="K-1",
            kalshi_event_ticker="K",
            status="failed",
            reason="unsubscribe boom",
        ),
    ]
    md = _FakeMetadata()
    screen = _make_screen(engine, md)
    app_stub = _AppStub()
    monkeypatch.setattr(TreeScreen, "app", property(lambda _self: app_stub))
    original = StagedChanges(
        to_remove=[("K-1", "K")],
        to_set_unticked=["K"],
    )
    screen.staged_changes = original

    completed = await screen.commit()

    # Contract 1: hard failure — return False, preserve staging.
    assert completed is False
    assert not screen.staged_changes.is_empty()
    assert screen.staged_changes.to_set_unticked == ["K"]
    # Contract 2: NO metadata writes for the failed event. Pre-fix,
    # the deferred branch would have written set_deliberately_unticked_pending
    # and added "K" to _deferred_set_unticked.
    assert md.applied == []  # no immediate apply
    assert "K" not in screen._deferred_set_unticked
    # Contract 3: toast surfaces the failure with the pair tickers and
    # reason so the operator can act.
    assert any(
        "K-1" in m and ("Remove failed" in m or "still live" in m)
        for m, _ in app_stub.notifications
    )
