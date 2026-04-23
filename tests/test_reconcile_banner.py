"""Tests for ``ReconcileBanner`` — bps/fp100 migration UI banner.

Covers the nine cases in Task 6b-3:

1. Banner hidden when ``ledger.ready()``.
2. Info banner during auto-reconcile (<30s).
3. Warning banner after 30s timeout.
4. Warning banner for ``legacy_migration_pending``.
5. Error banner for ``reconcile_mismatch_pending``.
6. "Reconcile now" click triggers ``ledger.reconcile_from_fills``.
7. "Accept Kalshi-fills state" click triggers ``accept_pending_mismatch``.
8. ``StaleMismatchError`` handling — fresh reconcile auto-triggered.
9. Priority resolution — mismatch wins over legacy.

Uses Textual's ``App.run_test()`` harness for DOM interaction where needed,
plus direct state-resolution tests that bypass the full app for speed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button

from talos.models.strategy import ArbPair
from talos.position_ledger import (
    PositionLedger,
    ReconcileOutcome,
    ReconcileResult,
    StaleMismatchError,
)
from talos.ui.reconcile_banner import (
    STALE_WARNING_TIMEOUT_SECONDS,
    ReconcileBanner,
    _resolve_banner_state,
)

# ── Test fixtures ───────────────────────────────────────────────────────


def _make_pair(event_ticker: str = "EVT-TEST") -> ArbPair:
    return ArbPair(
        event_ticker=event_ticker,
        ticker_a=f"{event_ticker}-A",
        ticker_b=f"{event_ticker}-B",
        side_a="no",
        side_b="no",
    )


def _make_ledger(event_ticker: str = "EVT-TEST") -> PositionLedger:
    return PositionLedger(
        event_ticker=event_ticker,
        unit_size=10,
        side_a_str="no",
        side_b_str="no",
        ticker_a=f"{event_ticker}-A",
        ticker_b=f"{event_ticker}-B",
    )


def _make_ready_ledger() -> PositionLedger:
    """A ledger with no flags and first-orders-sync completed — ``ready()``."""
    ledger = _make_ledger()
    ledger._first_orders_sync.set()
    return ledger


def _make_engine_mock() -> MagicMock:
    """Engine stub with ``_rest`` + ``_persist_games_now`` + notification hook."""
    engine = MagicMock()
    engine._rest = MagicMock()
    engine._persist_games_now = MagicMock()
    engine._persist_active_games = MagicMock()
    engine.on_notification = MagicMock()
    return engine


class _BannerHost(App[None]):
    """Minimal host app that owns one ReconcileBanner."""

    def __init__(self, pair: ArbPair, ledger: PositionLedger, engine: Any) -> None:
        super().__init__()
        self._pair = pair
        self._ledger = ledger
        self._engine = engine

    def compose(self) -> ComposeResult:
        yield ReconcileBanner(
            pair=self._pair,
            ledger=self._ledger,
            engine=self._engine,
            id="reconcile-banner",
        )


# ── Pure state-resolver tests (fast, no Textual harness) ────────────────


class TestStateResolver:
    def test_ready_ledger_resolves_to_none(self) -> None:
        ledger = _make_ready_ledger()
        assert _resolve_banner_state(ledger, stale_elapsed_seconds=0.0) is None

    def test_stale_fills_under_timeout_is_info(self) -> None:
        ledger = _make_ready_ledger()
        ledger.stale_fills_unconfirmed = True
        state = _resolve_banner_state(ledger, stale_elapsed_seconds=5.0)
        assert state is not None
        mode, severity, message, secondary = state
        assert mode == "stale_info"
        assert severity == "info"
        assert "Confirming" in message
        assert secondary is None

    def test_stale_fills_over_timeout_is_warning(self) -> None:
        ledger = _make_ready_ledger()
        ledger.stale_fills_unconfirmed = True
        state = _resolve_banner_state(
            ledger, stale_elapsed_seconds=STALE_WARNING_TIMEOUT_SECONDS + 0.1
        )
        assert state is not None
        mode, severity, _message, secondary = state
        assert mode == "stale_warning"
        assert severity == "warning"
        assert secondary == "Manual reconcile"

    def test_stale_resting_also_triggers_stale_mode(self) -> None:
        """Either stale flag (fills or resting) should surface the banner."""
        ledger = _make_ready_ledger()
        ledger.stale_resting_unconfirmed = True
        state = _resolve_banner_state(ledger, stale_elapsed_seconds=1.0)
        assert state is not None
        assert state[0] == "stale_info"

    def test_legacy_pending_is_warning(self) -> None:
        ledger = _make_ready_ledger()
        ledger.legacy_migration_pending = True
        state = _resolve_banner_state(ledger, stale_elapsed_seconds=0.0)
        assert state is not None
        mode, severity, _message, secondary = state
        assert mode == "legacy"
        assert severity == "warning"
        assert secondary == "View what will change"

    def test_mismatch_is_error(self) -> None:
        ledger = _make_ready_ledger()
        ledger.reconcile_mismatch_pending = True
        state = _resolve_banner_state(ledger, stale_elapsed_seconds=0.0)
        assert state is not None
        mode, severity, _message, secondary = state
        assert mode == "mismatch"
        assert severity == "error"
        assert secondary == "Resolve on Kalshi, then reset pair"

    def test_priority_mismatch_beats_legacy(self) -> None:
        """Case 9: mismatch + legacy both set → mismatch wins."""
        ledger = _make_ready_ledger()
        ledger.legacy_migration_pending = True
        ledger.reconcile_mismatch_pending = True
        state = _resolve_banner_state(ledger, stale_elapsed_seconds=0.0)
        assert state is not None
        assert state[0] == "mismatch"

    def test_priority_mismatch_beats_stale(self) -> None:
        ledger = _make_ready_ledger()
        ledger.stale_fills_unconfirmed = True
        ledger.reconcile_mismatch_pending = True
        state = _resolve_banner_state(
            ledger, stale_elapsed_seconds=STALE_WARNING_TIMEOUT_SECONDS + 10
        )
        assert state is not None
        assert state[0] == "mismatch"

    def test_priority_legacy_beats_stale(self) -> None:
        ledger = _make_ready_ledger()
        ledger.stale_fills_unconfirmed = True
        ledger.legacy_migration_pending = True
        state = _resolve_banner_state(
            ledger, stale_elapsed_seconds=STALE_WARNING_TIMEOUT_SECONDS + 10
        )
        assert state is not None
        assert state[0] == "legacy"

    def test_awaiting_first_sync_info_when_no_flags(self) -> None:
        """``ready()`` is False but no flags are set — awaiting first sync."""
        ledger = _make_ledger()  # _first_orders_sync NOT set
        assert not ledger.ready()
        state = _resolve_banner_state(ledger, stale_elapsed_seconds=0.0)
        assert state is not None
        assert state[0] == "awaiting_first_sync"


# ── Textual harness tests (DOM + button interaction) ────────────────────


@pytest.mark.asyncio
class TestBannerRendering:
    async def test_banner_hidden_when_ledger_ready(self) -> None:
        """Case 1: ledger.ready() → banner has no 'visible' class, no mode."""
        ledger = _make_ready_ledger()
        pair = _make_pair()
        engine = _make_engine_mock()
        async with _BannerHost(pair, ledger, engine).run_test() as pilot:
            banner = pilot.app.query_one(ReconcileBanner)
            banner.refresh_state()
            await pilot.pause()
            assert banner.current_mode is None
            assert not banner.has_class("visible")

    async def test_info_banner_during_auto_reconcile(self) -> None:
        """Case 2: stale flag set + <30s elapsed → info banner."""
        ledger = _make_ready_ledger()
        ledger.stale_fills_unconfirmed = True
        pair = _make_pair()
        engine = _make_engine_mock()
        async with _BannerHost(pair, ledger, engine).run_test() as pilot:
            banner = pilot.app.query_one(ReconcileBanner)
            banner.refresh_state()  # stamps stale_observed_at = now
            await pilot.pause()
            assert banner.current_mode == "stale_info"
            assert banner.has_class("visible")
            assert banner.has_class("severity-info")
            # No action buttons in info mode
            assert len(banner.query(Button)) == 0

    async def test_warning_banner_after_30s_timeout(self) -> None:
        """Case 3: stale flag + elapsed > timeout → warning banner with buttons."""
        ledger = _make_ready_ledger()
        ledger.stale_fills_unconfirmed = True
        pair = _make_pair()
        engine = _make_engine_mock()
        async with _BannerHost(pair, ledger, engine).run_test() as pilot:
            banner = pilot.app.query_one(ReconcileBanner)
            # Fake out the elapsed timer: stamp a point >30s in the past.
            import time as _time

            banner._stale_observed_at = _time.monotonic() - (
                STALE_WARNING_TIMEOUT_SECONDS + 1
            )
            # Re-render without re-stamping (stale flag still True).
            # refresh_state will see stale=True and keep the existing timestamp.
            banner.refresh_state()
            await pilot.pause()
            assert banner.current_mode == "stale_warning"
            assert banner.has_class("severity-warning")
            buttons = banner.query(Button)
            assert len(buttons) == 2
            labels = {b.label.plain for b in buttons}  # type: ignore[attr-defined]
            assert "Retry sync" in labels
            assert "Manual reconcile" in labels

    async def test_warning_banner_for_legacy_pending(self) -> None:
        """Case 4: legacy_migration_pending → warning + 'Reconcile now' + 'View ...'"""
        ledger = _make_ready_ledger()
        ledger.legacy_migration_pending = True
        pair = _make_pair()
        engine = _make_engine_mock()
        async with _BannerHost(pair, ledger, engine).run_test() as pilot:
            banner = pilot.app.query_one(ReconcileBanner)
            banner.refresh_state()
            await pilot.pause()
            assert banner.current_mode == "legacy"
            assert banner.has_class("severity-warning")
            labels = {b.label.plain for b in banner.query(Button)}  # type: ignore[attr-defined]
            assert "Reconcile now" in labels
            assert "View what will change" in labels

    async def test_error_banner_for_mismatch_pending(self) -> None:
        """Case 5: reconcile_mismatch_pending → error + Accept/Reset."""
        ledger = _make_ready_ledger()
        ledger.reconcile_mismatch_pending = True
        pair = _make_pair()
        engine = _make_engine_mock()
        async with _BannerHost(pair, ledger, engine).run_test() as pilot:
            banner = pilot.app.query_one(ReconcileBanner)
            banner.refresh_state()
            await pilot.pause()
            assert banner.current_mode == "mismatch"
            assert banner.has_class("severity-error")
            labels = {b.label.plain for b in banner.query(Button)}  # type: ignore[attr-defined]
            assert "Accept Kalshi-fills state" in labels
            assert "Resolve on Kalshi, then reset pair" in labels

    async def test_priority_mismatch_over_legacy_in_dom(self) -> None:
        """Case 9 (DOM round-trip): mismatch + legacy both set → mismatch wins."""
        ledger = _make_ready_ledger()
        ledger.legacy_migration_pending = True
        ledger.reconcile_mismatch_pending = True
        pair = _make_pair()
        engine = _make_engine_mock()
        async with _BannerHost(pair, ledger, engine).run_test() as pilot:
            banner = pilot.app.query_one(ReconcileBanner)
            banner.refresh_state()
            await pilot.pause()
            assert banner.current_mode == "mismatch"
            assert banner.has_class("severity-error")


# ── Button click → ledger action wiring ─────────────────────────────────


@pytest.mark.asyncio
class TestBannerActions:
    async def test_reconcile_now_triggers_reconcile_from_fills(self) -> None:
        """Case 6: legacy banner's primary button calls reconcile_from_fills."""
        ledger = _make_ready_ledger()
        ledger.legacy_migration_pending = True
        pair = _make_pair()
        engine = _make_engine_mock()

        reconcile_mock = AsyncMock(
            return_value=ReconcileResult(outcome=ReconcileOutcome.OK)
        )
        # Patch the bound method on this ledger instance only.
        ledger.reconcile_from_fills = reconcile_mock  # type: ignore[method-assign]

        async with _BannerHost(pair, ledger, engine).run_test() as pilot:
            banner = pilot.app.query_one(ReconcileBanner)
            banner.refresh_state()
            await pilot.pause()
            primary = banner.query_one("#reconcile-primary", Button)
            await pilot.click(primary)
            await pilot.pause()
            await pilot.pause()
            reconcile_mock.assert_awaited_once()
            args = reconcile_mock.await_args
            assert args is not None
            # First positional arg should be the engine's rest client.
            assert args.args[0] is engine._rest

    async def test_accept_kalshi_fills_triggers_accept_pending_mismatch(self) -> None:
        """Case 7: mismatch banner's primary button calls accept_pending_mismatch."""
        ledger = _make_ready_ledger()
        ledger.reconcile_mismatch_pending = True
        pair = _make_pair()
        engine = _make_engine_mock()

        accept_mock = AsyncMock(return_value=None)
        ledger.accept_pending_mismatch = accept_mock  # type: ignore[method-assign]

        async with _BannerHost(pair, ledger, engine).run_test() as pilot:
            banner = pilot.app.query_one(ReconcileBanner)
            banner.refresh_state()
            await pilot.pause()
            primary = banner.query_one("#reconcile-primary", Button)
            await pilot.click(primary)
            await pilot.pause()
            await pilot.pause()
            accept_mock.assert_awaited_once()
            # The persist callback must be the engine's _persist_games_now.
            args = accept_mock.await_args
            assert args is not None
            assert args.args[0] is engine._persist_games_now

    async def test_stale_mismatch_triggers_fresh_reconcile(self) -> None:
        """Case 8: accept_pending_mismatch raising StaleMismatchError should
        auto-trigger a fresh reconcile_from_fills.
        """
        ledger = _make_ready_ledger()
        ledger.reconcile_mismatch_pending = True
        pair = _make_pair()
        engine = _make_engine_mock()

        accept_mock = AsyncMock(side_effect=StaleMismatchError("stale"))
        reconcile_mock = AsyncMock(
            return_value=ReconcileResult(outcome=ReconcileOutcome.OK)
        )
        ledger.accept_pending_mismatch = accept_mock  # type: ignore[method-assign]
        ledger.reconcile_from_fills = reconcile_mock  # type: ignore[method-assign]

        async with _BannerHost(pair, ledger, engine).run_test() as pilot:
            banner = pilot.app.query_one(ReconcileBanner)
            banner.refresh_state()
            await pilot.pause()
            primary = banner.query_one("#reconcile-primary", Button)
            await pilot.click(primary)
            await pilot.pause()
            await pilot.pause()
            accept_mock.assert_awaited_once()
            # And the stale-handler fell through to a fresh reconcile.
            reconcile_mock.assert_awaited_once()

    async def test_retry_sync_on_stale_warning_calls_reconcile(self) -> None:
        """stale_warning banner's 'Retry sync' also calls reconcile_from_fills.

        Per Task 6b-3 scope the "Retry sync" action nudges the polling cycle —
        we implement it as a reconcile kick, which drives a fresh fills sync
        and clears the stale state on success.
        """
        ledger = _make_ready_ledger()
        ledger.stale_fills_unconfirmed = True
        pair = _make_pair()
        engine = _make_engine_mock()

        reconcile_mock = AsyncMock(
            return_value=ReconcileResult(outcome=ReconcileOutcome.OK)
        )
        ledger.reconcile_from_fills = reconcile_mock  # type: ignore[method-assign]

        async with _BannerHost(pair, ledger, engine).run_test() as pilot:
            banner = pilot.app.query_one(ReconcileBanner)
            # Force the timeout branch.
            import time as _time

            banner._stale_observed_at = _time.monotonic() - (
                STALE_WARNING_TIMEOUT_SECONDS + 1
            )
            banner.refresh_state()
            await pilot.pause()
            assert banner.current_mode == "stale_warning"
            primary = banner.query_one("#reconcile-primary", Button)
            await pilot.click(primary)
            await pilot.pause()
            await pilot.pause()
            reconcile_mock.assert_awaited_once()

    async def test_reconcile_mismatch_transitions_banner_to_error(self) -> None:
        """When a reconcile kick from the legacy banner returns MISMATCH,
        refreshing the banner must transition it to the error state — the
        ledger's reconcile_mismatch_pending flag is already set.
        """
        ledger = _make_ready_ledger()
        ledger.legacy_migration_pending = True
        pair = _make_pair()
        engine = _make_engine_mock()

        async def fake_reconcile(rest: Any, persist_cb: Any) -> ReconcileResult:
            # Simulate the real ledger setting the mismatch flag on MISMATCH.
            ledger.reconcile_mismatch_pending = True
            return ReconcileResult(outcome=ReconcileOutcome.MISMATCH)

        ledger.reconcile_from_fills = fake_reconcile  # type: ignore[method-assign]

        async with _BannerHost(pair, ledger, engine).run_test() as pilot:
            banner = pilot.app.query_one(ReconcileBanner)
            banner.refresh_state()
            await pilot.pause()
            assert banner.current_mode == "legacy"
            primary = banner.query_one("#reconcile-primary", Button)
            await pilot.click(primary)
            await pilot.pause()
            await pilot.pause()
            # After click, banner's post-action refresh_state must pick up
            # the newly-set mismatch flag.
            assert banner.current_mode == "mismatch"
            assert banner.has_class("severity-error")
