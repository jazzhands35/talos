"""Tests for ``ReconcileBanner`` — bps/fp100 migration UI banner.

Covers the surviving cases after the mismatch-auto-adopt refactor
(Principle 7 — Kalshi is the single source of truth, so local/Kalshi
disagreement no longer surfaces a banner; one warning log inside
``reconcile_from_fills`` captures the diff, and the ledger adopts).

1. Banner hidden when ``ledger.ready()``.
2. Info banner during auto-reconcile (<30s).
3. Warning banner after 30s timeout.
4. Warning banner for ``legacy_migration_pending``.
5. "Reconcile now" click triggers ``ledger.reconcile_from_fills``.
6. Priority — legacy beats stale.

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

    def test_no_mismatch_mode_exists(self) -> None:
        """Regression guard: local/Kalshi mismatches are resolved by
        auto-adopt inside ``reconcile_from_fills`` — the banner must never
        surface a 'mismatch' mode. Any reintroduction would bring back the
        'Confirm or reconcile' operator gate that Principle 7 forbids.
        """
        ledger = _make_ready_ledger()
        # No attribute exists for reconcile_mismatch_pending anymore.
        assert not hasattr(ledger, "reconcile_mismatch_pending")
        # Fabricating one at runtime must NOT resurrect a mismatch mode.
        ledger.reconcile_mismatch_pending = True  # type: ignore[attr-defined]
        state = _resolve_banner_state(ledger, stale_elapsed_seconds=0.0)
        # Ledger is otherwise ready() → banner should hide entirely.
        assert state is None


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


# ── Button click → ledger action wiring ─────────────────────────────────


@pytest.mark.asyncio
class TestBannerActions:
    async def test_reconcile_now_triggers_reconcile_from_fills(self) -> None:
        """Case 5: legacy banner's primary button calls reconcile_from_fills."""
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

    async def test_retry_sync_on_stale_warning_calls_reconcile(self) -> None:
        """stale_warning banner's 'Retry sync' also calls reconcile_from_fills."""
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
