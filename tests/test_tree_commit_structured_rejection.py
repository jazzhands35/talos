"""End-to-end test: TreeScreen.commit() selective staging clear +
partial-failure dialog on rejected rows (F34 + F35 regression guard)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from talos.game_manager import CommitResult, MarketAdmissionError
from talos.models.tree import ArbPairRecord, StagedChanges
from talos.ui.tree_screen import TreeScreen


def _mk_record(event_ticker: str = "KXA-26JAN01") -> ArbPairRecord:
    return ArbPairRecord(
        event_ticker=event_ticker,
        ticker_a=f"{event_ticker}-A",
        ticker_b=f"{event_ticker}-B",
        kalshi_event_ticker=event_ticker,
        series_ticker=event_ticker.split("-")[0],
        category="test",
    )


class _StubEngine:
    """Minimal Engine stub for commit() testing."""

    def __init__(self, commit_result: CommitResult) -> None:
        self._commit_result = commit_result
        self.calls: list[list[dict[str, Any]]] = []

    async def add_pairs_from_selection(
        self, records: list[dict[str, Any]]
    ) -> CommitResult:
        self.calls.append(records)
        return self._commit_result

    async def remove_pairs_from_selection(self, removes: Any) -> list[Any]:
        return []

    async def refresh_volumes(self) -> None:
        return None


class _StubMetadata:
    def __init__(self) -> None:
        self.manual_starts: dict[str, Any] = {}
        self.unticked: set[str] = set()

    def set_manual_event_start(self, k: str, v: Any) -> None:
        self.manual_starts[k] = v

    def set_deliberately_unticked(self, k: str) -> None:
        self.unticked.add(k)

    def set_deliberately_unticked_pending(self, k: str) -> None:
        self.unticked.add(k)

    def clear_deliberately_unticked(self, k: str) -> None:
        self.unticked.discard(k)


class _AppStub:
    """Minimal stub for screen.app — Textual's MessagePump.app is a
    read-only property, so tests patch it via monkeypatch on the class."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []

    def notify(
        self, msg: str, severity: str = "information", **_: Any
    ) -> None:
        self.notifications.append((msg, severity))


@pytest.fixture
def ts_with_commit_result(monkeypatch):
    """Factory: build a TreeScreen-like object with a controlled CommitResult."""

    def _build(
        commit_result: CommitResult, staged_records: list[ArbPairRecord]
    ):
        # Build TreeScreen via __new__ to bypass Textual mounting.
        ts: Any = TreeScreen.__new__(TreeScreen)
        ts._engine = _StubEngine(commit_result)
        ts._metadata = _StubMetadata()
        ts._milestones = None
        ts.staged_changes = StagedChanges.empty()
        ts.staged_changes.to_add = list(staged_records)
        ts._deferred_set_unticked = set()

        # Capture notify calls via an _AppStub (screen.app is a read-only
        # Textual property, so we patch it at the class level).
        app_stub = _AppStub()
        monkeypatch.setattr(
            TreeScreen, "app", property(lambda _self: app_stub)
        )
        ts._notify_capture = app_stub.notifications
        # Bypass the needs_schedule branch by stubbing _events_needing_schedule.
        ts._events_needing_schedule = lambda: []
        return ts

    return _build


@pytest.mark.asyncio
async def test_mixed_batch_keeps_rejected_staged_and_clears_admitted(
    ts_with_commit_result,
):
    """Admitted rows clear from staging; rejected rows remain."""
    admitted_record = _mk_record("KXA-26JAN01")
    rejected_record = _mk_record("KXF-26JAN01")

    # The Engine stub doesn't actually use the record dict — admitted can be
    # any truthy object; rejected is the (dict, error) tuple list.
    admitted_pair = MagicMock(
        event_ticker="KXA-26JAN01",
        kalshi_event_ticker="KXA-26JAN01",
    )
    cr = CommitResult(
        admitted=[admitted_pair],
        rejected=[
            (
                {"event_ticker": "KXF-26JAN01", "ticker_a": "KXF-26JAN01-A"},
                MarketAdmissionError(
                    "KXF-26JAN01-A: fractional_trading_enabled ..."
                ),
            ),
        ],
    )
    ts = ts_with_commit_result(cr, [admitted_record, rejected_record])

    ok = await ts.commit()

    assert ok is False, (
        "mixed-batch commit must return False to suppress success toast"
    )
    staged_tickers = {r.event_ticker for r in ts.staged_changes.to_add}
    assert staged_tickers == {"KXF-26JAN01"}, (
        f"expected only KXF-26JAN01 to remain staged, got {staged_tickers}"
    )

    messages = [m for m, _ in ts._notify_capture]
    assert any("KXF-26JAN01" in m for m in messages), (
        f"partial-failure dialog must mention KXF-26JAN01: {messages}"
    )
    assert not any(m == "Commit complete." for m, _ in ts._notify_capture)


@pytest.mark.asyncio
async def test_all_rejected_returns_false_and_keeps_everything_staged(
    ts_with_commit_result,
):
    """When nothing is admitted, all rows stay staged and return False."""
    records = [_mk_record("KXF1-26JAN01"), _mk_record("KXF2-26JAN01")]
    cr = CommitResult(
        admitted=[],
        rejected=[
            (
                {"event_ticker": "KXF1-26JAN01"},
                MarketAdmissionError("KXF1: fractional"),
            ),
            (
                {"event_ticker": "KXF2-26JAN01"},
                MarketAdmissionError("KXF2: fractional"),
            ),
        ],
    )
    ts = ts_with_commit_result(cr, records)

    ok = await ts.commit()

    assert ok is False
    staged_tickers = {r.event_ticker for r in ts.staged_changes.to_add}
    assert staged_tickers == {"KXF1-26JAN01", "KXF2-26JAN01"}

    # All-rejected → error severity
    severities = [s for _, s in ts._notify_capture]
    assert "error" in severities
    # No success toast
    assert not any(m == "Commit complete." for m, _ in ts._notify_capture)


@pytest.mark.asyncio
async def test_clean_batch_clears_staging_and_returns_true(
    ts_with_commit_result,
):
    """All-admitted commit: staging fully cleared, return True."""
    record = _mk_record("KXA-26JAN01")
    admitted_pair = MagicMock(
        event_ticker="KXA-26JAN01",
        kalshi_event_ticker="KXA-26JAN01",
    )
    cr = CommitResult(admitted=[admitted_pair], rejected=[])
    ts = ts_with_commit_result(cr, [record])

    ok = await ts.commit()

    assert ok is True
    assert ts.staged_changes.to_add == []
    # No partial-failure dialog
    assert not any("rejected" in m.lower() for m, _ in ts._notify_capture)


@pytest.mark.asyncio
async def test_commit_worker_suppresses_success_toast_on_any_rejection(
    ts_with_commit_result,
):
    """Task 6 regression guard: _commit_worker must only fire the
    'Commit complete.' toast when commit() returns True. If ANY row
    was rejected (commit() returns False), the worker returns early
    and the success toast is suppressed."""
    admitted_record = _mk_record("KXA-26JAN01")
    rejected_record = _mk_record("KXF-26JAN01")
    admitted_pair = MagicMock(
        event_ticker="KXA-26JAN01",
        kalshi_event_ticker="KXA-26JAN01",
    )
    cr = CommitResult(
        admitted=[admitted_pair],
        rejected=[
            (
                {"event_ticker": "KXF-26JAN01", "ticker_a": "KXF-26JAN01-A"},
                MarketAdmissionError("KXF-26JAN01-A: fractional_trading_enabled ..."),
            ),
        ],
    )
    ts = ts_with_commit_result(cr, [admitted_record, rejected_record])
    # _commit_worker calls self._rebuild_tree only on success — stub it so
    # the fixture doesn't crash if that path is accidentally reached.
    ts._rebuild_tree = lambda: None
    ts._commit_in_flight = True  # worker's finally clause expects this attr

    await ts._commit_worker()

    success_toasts = [
        m for m, _ in ts._notify_capture if m == "Commit complete."
    ]
    assert success_toasts == [], (
        f"success toast must not fire when any row was rejected, got: "
        f"{ts._notify_capture}"
    )
    # The partial-failure notify fired from commit(), captured via app_stub.
    assert any("KXF-26JAN01" in m for m, _ in ts._notify_capture)


@pytest.mark.asyncio
async def test_commit_worker_fires_success_toast_on_clean_batch(
    ts_with_commit_result,
):
    """Complement of the rejection case: a clean commit DOES fire the
    success toast so we know the suppression logic is specific."""
    record = _mk_record("KXA-26JAN01")
    admitted_pair = MagicMock(
        event_ticker="KXA-26JAN01",
        kalshi_event_ticker="KXA-26JAN01",
    )
    cr = CommitResult(admitted=[admitted_pair], rejected=[])
    ts = ts_with_commit_result(cr, [record])
    ts._rebuild_tree = lambda: None
    ts._commit_in_flight = True

    await ts._commit_worker()

    success_toasts = [
        m for m, _ in ts._notify_capture if m == "Commit complete."
    ]
    assert len(success_toasts) == 1, (
        f"expected one success toast on clean commit, got: {ts._notify_capture}"
    )
