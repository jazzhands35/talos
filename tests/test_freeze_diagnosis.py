"""Diagnostic tests to isolate the UI freeze on WS disconnect.

Strategy: Start with the simplest possible reproduction, then layer
in components one at a time until the freeze reproduces.

Each test measures wall-clock time for operations that should be fast.
A freeze means some synchronous operation blocks the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.automation_config import AutomationConfig
from talos.bid_adjuster import BidAdjuster
from talos.engine import TradingEngine
from talos.game_manager import GameManager
from talos.market_feed import MarketFeed
from talos.models.strategy import ArbPair
from talos.orderbook import OrderBookManager
from talos.scanner import ArbitrageScanner
from talos.top_of_market import TopOfMarketTracker
from talos.ui.app import TalosApp

# Every test in this module deliberately simulates slow/hanging REST or WS
# scenarios to verify Talos doesn't deadlock. They EARN their seconds — but
# they're not needed on every dev iteration. Skipped by default; run via
# `pytest -m slow` or `pytest -m ""` (no filter) for the full suite.
pytestmark = pytest.mark.slow

# ── Helpers ──────────────────────────────────────────────────


def _make_rest() -> AsyncMock:
    rest = AsyncMock()
    rest.get_balance.return_value = MagicMock(balance_bps=1_000_000, portfolio_value_bps=1_000_000)
    rest.get_all_orders.return_value = []
    rest.get_positions.return_value = []
    rest.get_queue_positions.return_value = {}
    rest.get_event_positions.return_value = []
    rest.get_trades.return_value = []
    return rest


def _make_engine(*, n_pairs: int = 0, with_data_collector: bool = False) -> TradingEngine:
    """Build a minimal engine with mocked I/O."""
    books = OrderBookManager()
    scanner = ArbitrageScanner(books)
    tracker = TopOfMarketTracker(books)
    rest = _make_rest()
    feed = MagicMock(spec=MarketFeed)
    feed.subscribe_bulk = AsyncMock()
    adjuster = BidAdjuster(books, [], unit_size=10)
    game_mgr = MagicMock(spec=GameManager)
    game_mgr.active_games = []
    game_mgr.labels = {}
    game_mgr.subtitles = {}
    game_mgr.volumes_24h = {}
    game_mgr.leg_labels = {}

    # These diagnostics measure engine responsiveness without the tree-mode
    # startup gate — that path is covered by its own tests. Passing
    # tree_mode=False explicitly keeps refresh_account / refresh_balance
    # fast even after the default flipped to True.
    engine = TradingEngine(
        scanner=scanner,
        game_manager=game_mgr,
        rest_client=rest,
        market_feed=feed,
        tracker=tracker,
        adjuster=adjuster,
        automation_config=AutomationConfig(tree_mode=False),
    )

    # Add pairs if requested
    for i in range(n_pairs):
        pair = ArbPair(
            event_ticker=f"EVT-{i}",
            ticker_a=f"TK-{i}-A",
            ticker_b=f"TK-{i}-B",
        )
        scanner.add_pair(pair.event_ticker, pair.ticker_a, pair.ticker_b)
        adjuster.add_event(pair)

    if with_data_collector:
        import tempfile
        from pathlib import Path

        from talos.data_collector import DataCollector

        db_path = Path(tempfile.mktemp(suffix=".db"))
        engine._data_collector = DataCollector(db_path)

    return engine


# ── Level 1: Bare app (no engine) ───────────────────────────


class TestLevel1BareApp:
    """Does the app itself freeze? No engine, no polling, no I/O."""

    async def test_app_mounts_and_responds(self) -> None:
        """Bare app should mount and respond within 2 seconds."""
        app = TalosApp()
        async with app.run_test() as pilot:
            t0 = time.monotonic()
            await pilot.pause()
            elapsed = time.monotonic() - t0
            assert elapsed < 2.0, f"Bare mount took {elapsed:.1f}s"

    async def test_app_handles_notify_burst(self) -> None:
        """Fire 50 notifications rapidly — does the app survive?"""
        app = TalosApp()
        async with app.run_test() as pilot:
            for i in range(50):
                app.notify(f"Test notification {i}", severity="warning")
            t0 = time.monotonic()
            await pilot.pause()
            elapsed = time.monotonic() - t0
            assert elapsed < 5.0, f"50 notifications took {elapsed:.1f}s"

    async def test_app_handles_100_notifications(self) -> None:
        """Fire 100 notifications — stress test the toast layer."""
        app = TalosApp()
        async with app.run_test() as pilot:
            for i in range(100):
                app.notify(f"Notification {i}", severity="error")
            t0 = time.monotonic()
            await pilot.pause()
            elapsed = time.monotonic() - t0
            assert elapsed < 10.0, f"100 notifications took {elapsed:.1f}s"


# ── Level 2: Engine with no pairs ────────────────────────────


class TestLevel2EngineNoPairs:
    """App with engine but zero games. Timers fire but nothing to process."""

    async def test_engine_mount_and_timers(self) -> None:
        """Engine wired up, timers start. Should still be responsive."""
        engine = _make_engine()
        app = TalosApp(engine=engine)
        async with app.run_test() as pilot:
            # Let timers fire for 3 seconds
            await pilot.pause(delay=3.0)
            t0 = time.monotonic()
            await pilot.pause()
            elapsed = time.monotonic() - t0
            assert elapsed < 2.0, f"Post-timer pause took {elapsed:.1f}s"

    async def test_engine_notification_dedup(self) -> None:
        """Fire same notification 20 times — dedup should suppress repeats."""
        engine = _make_engine()
        app = TalosApp(engine=engine)
        notifications: list[str] = []
        original = app.notify

        def tracking_notify(msg: str, **kwargs) -> None:
            notifications.append(msg)
            original(msg, **kwargs)

        async with app.run_test() as pilot:
            app.notify = tracking_notify  # type: ignore[assignment]
            for _ in range(20):
                engine._notify("WEBSOCKET DISCONNECTED — prices are stale!", "error")
            await pilot.pause()
            # Dedup should have suppressed most of these
            ws_msgs = [m for m in notifications if "WEBSOCKET" in m]
            assert len(ws_msgs) <= 2, f"Dedup failed: {len(ws_msgs)} WS notifications"


# ── Level 3: Engine with pairs ───────────────────────────────


class TestLevel3EngineWithPairs:
    """App with engine and 20 pairs. More realistic load."""

    async def test_20_pairs_mount(self) -> None:
        """20 pairs loaded — should mount quickly."""
        engine = _make_engine(n_pairs=20)
        app = TalosApp(engine=engine)
        async with app.run_test() as pilot:
            await pilot.pause(delay=1.0)
            t0 = time.monotonic()
            await pilot.pause()
            elapsed = time.monotonic() - t0
            assert elapsed < 2.0, f"20-pair mount took {elapsed:.1f}s"

    async def test_20_pairs_refresh_account(self) -> None:
        """Run refresh_account with 20 pairs — should complete fast."""
        engine = _make_engine(n_pairs=20)
        t0 = time.monotonic()
        await engine.refresh_account()
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"refresh_account took {elapsed:.1f}s"

    async def test_20_pairs_reconcile(self) -> None:
        """Run _reconcile_with_kalshi with 20 pairs — should be instant."""
        engine = _make_engine(n_pairs=20)
        t0 = time.monotonic()
        engine._reconcile_with_kalshi([], {})
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, f"reconcile took {elapsed:.1f}s"


# ── Level 4: Simulate WS disconnect ─────────────────────────


class TestLevel4WSDisconnect:
    """Simulate what happens when the WS drops."""

    async def test_ws_disconnect_notification_path(self) -> None:
        """Engine fires disconnect notification — does the app handle it?"""
        engine = _make_engine(n_pairs=5)
        app = TalosApp(engine=engine)
        async with app.run_test() as pilot:
            # Simulate WS disconnect
            engine._ws_connected = False
            engine._notify("WEBSOCKET DISCONNECTED: ConnectionClosed", "error")
            engine._notify("Reconnecting WebSocket in 5s...", "warning")
            t0 = time.monotonic()
            await pilot.pause()
            elapsed = time.monotonic() - t0
            assert elapsed < 2.0, f"WS disconnect handling took {elapsed:.1f}s"

    async def test_ws_disconnect_banner_toggle(self) -> None:
        """Banner visibility toggle every 1s — does it cause layout thrash?"""
        engine = _make_engine(n_pairs=5)
        app = TalosApp(engine=engine)
        async with app.run_test() as pilot:
            engine._ws_connected = False
            # Let _refresh_proposals fire 5 times (5 seconds)
            await pilot.pause(delay=5.0)
            t0 = time.monotonic()
            await pilot.pause()
            elapsed = time.monotonic() - t0
            assert elapsed < 2.0, f"Banner toggle took {elapsed:.1f}s"

    async def test_rapid_ws_state_changes(self) -> None:
        """Toggle ws_connected rapidly — simulates flapping connection."""
        engine = _make_engine(n_pairs=5)
        app = TalosApp(engine=engine)
        async with app.run_test() as pilot:
            for i in range(20):
                engine._ws_connected = i % 2 == 0
                engine._notify(f"State change {i}", "warning")
            t0 = time.monotonic()
            await pilot.pause()
            elapsed = time.monotonic() - t0
            assert elapsed < 3.0, f"Rapid state changes took {elapsed:.1f}s"


# ── Level 5: DataCollector SQLite ────────────────────────────


class TestLevel5DataCollector:
    """Is synchronous SQLite I/O blocking the event loop?"""

    def test_snapshot_batch_write_speed(self) -> None:
        """50 snapshot inserts with batched commit — should be fast."""
        import tempfile
        from pathlib import Path

        from talos.data_collector import DataCollector

        db_path = Path(tempfile.mktemp(suffix=".db"))
        try:
            dc = DataCollector(db_path)
            snapshots = [
                {
                    "event_ticker": f"EVT-{i}",
                    "ticker_a": f"TK-{i}-A",
                    "ticker_b": f"TK-{i}-B",
                    "no_a": 40,
                    "no_b": 55,
                    "edge": 1.5,
                    "volume_a": 0,
                    "volume_b": 0,
                    "open_interest_a": 0,
                    "open_interest_b": 0,
                    "game_state": "pre",
                    "status": "Bidding",
                    "filled_a": 0,
                    "filled_b": 0,
                    "resting_a": 0,
                    "resting_b": 0,
                }
                for i in range(50)
            ]
            t0 = time.monotonic()
            dc.log_market_snapshots(snapshots)
            elapsed = time.monotonic() - t0
            dc.close()
            assert elapsed < 0.5, f"50 snapshots took {elapsed:.3f}s"
        finally:
            db_path.unlink(missing_ok=True)

    async def test_snapshot_write_doesnt_block_app(self) -> None:
        """Full app with DataCollector — snapshot write shouldn't freeze UI."""
        import tempfile
        from pathlib import Path

        from talos.data_collector import DataCollector

        engine = _make_engine(n_pairs=20)
        db_path = Path(tempfile.mktemp(suffix=".db"))
        try:
            engine._data_collector = DataCollector(db_path)
            app = TalosApp(engine=engine)
            async with app.run_test() as pilot:
                # Let _log_market_snapshots fire (10s interval)
                # Instead, call it directly
                app._log_market_snapshots()
                t0 = time.monotonic()
                await pilot.pause()
                elapsed = time.monotonic() - t0
                assert elapsed < 2.0, f"Snapshot write blocked for {elapsed:.1f}s"
        finally:
            if engine._data_collector is not None:
                engine._data_collector.close()
            db_path.unlink(missing_ok=True)


# ── Level 6: Full integration — mount + disconnect ───────────


class TestLevel6FullIntegration:
    """Closest to real conditions: engine + pairs + WS disconnect."""

    async def test_full_startup_then_disconnect(self) -> None:
        """Mount with engine, let timers run, then simulate WS drop."""
        engine = _make_engine(n_pairs=20)
        app = TalosApp(engine=engine)
        async with app.run_test() as pilot:
            # Let the app stabilize (timers fire)
            await pilot.pause(delay=2.0)

            # Simulate WS disconnect
            engine._ws_connected = False
            engine._notify("WEBSOCKET DISCONNECTED: test", "error")

            # Can the event loop still process?
            t0 = time.monotonic()
            await pilot.pause(delay=3.0)
            elapsed = time.monotonic() - t0

            # Should take ~3s (the delay), not much more
            assert elapsed < 6.0, f"Post-disconnect pause took {elapsed:.1f}s (expected ~3s)"

    async def test_poll_account_during_disconnect(self) -> None:
        """refresh_account while WS is down — does it complete?"""
        engine = _make_engine(n_pairs=20)
        engine._ws_connected = False
        t0 = time.monotonic()
        await engine.refresh_account()
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"refresh_account during disconnect took {elapsed:.1f}s"

    async def test_concurrent_polling_during_disconnect(self) -> None:
        """Multiple poll methods running concurrently — any deadlock?"""
        engine = _make_engine(n_pairs=20)
        engine._ws_connected = False

        t0 = time.monotonic()
        await asyncio.gather(
            engine.refresh_account(),
            engine.refresh_balance(),
            engine.refresh_queue_positions(),
            return_exceptions=True,
        )
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"Concurrent polls took {elapsed:.1f}s"


# ── Level 7: Slow REST responses (simulated network issues) ──


class TestLevel7SlowREST:
    """Simulate what happens when Kalshi API is slow/hanging.

    This is the missing ingredient — real network latency/timeouts
    that the mocked tests don't capture.
    """

    async def test_slow_balance_doesnt_block_app(self) -> None:
        """Balance poll takes 5s — does the app stay responsive?"""
        engine = _make_engine(n_pairs=5)

        async def slow_balance():
            await asyncio.sleep(5.0)
            return MagicMock(balance_bps=1_000_000, portfolio_value_bps=1_000_000)

        engine._rest.get_balance = slow_balance
        app = TalosApp(engine=engine)
        async with app.run_test(size=(120, 40)) as pilot:
            # Let timers start (balance poll fires at 10s, but call directly)
            t0 = time.monotonic()
            await pilot.pause(delay=1.0)
            elapsed = time.monotonic() - t0
            # The app should pause for ~1s, not 5s
            assert elapsed < 3.0, f"App blocked by slow balance: {elapsed:.1f}s"

    async def test_slow_orders_doesnt_block_app(self) -> None:
        """Orders fetch takes 10s — does the app stay responsive?"""
        engine = _make_engine(n_pairs=5)

        async def slow_orders(**kwargs):
            await asyncio.sleep(10.0)
            return []

        engine._rest.get_all_orders = slow_orders
        app = TalosApp(engine=engine)
        async with app.run_test(size=(120, 40)) as pilot:
            t0 = time.monotonic()
            await pilot.pause(delay=1.0)
            elapsed = time.monotonic() - t0
            assert elapsed < 3.0, f"App blocked by slow orders: {elapsed:.1f}s"

    async def test_stacked_slow_queue_polls(self) -> None:
        """Queue poll every 3s but each takes 10s — workers stack up."""
        engine = _make_engine(n_pairs=5)
        call_count = 0

        async def slow_queue(**kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(10.0)
            return {}

        engine._rest.get_queue_positions = slow_queue
        app = TalosApp(engine=engine)
        async with app.run_test(size=(120, 40)) as pilot:
            # Let queue polls stack up for 7 seconds (should start 2-3 polls)
            t0 = time.monotonic()
            await pilot.pause(delay=7.0)
            elapsed = time.monotonic() - t0
            # Should take ~7s, not blocked
            assert elapsed < 12.0, f"Stacked polls blocked: {elapsed:.1f}s"

    async def test_all_rest_slow_simultaneously(self) -> None:
        """ALL REST endpoints take 10s — worst case network failure."""
        engine = _make_engine(n_pairs=10)

        async def slow_any(*args, **kwargs):
            await asyncio.sleep(10.0)
            return MagicMock(balance_bps=0, portfolio_value_bps=0)

        async def slow_list(*args, **kwargs):
            await asyncio.sleep(10.0)
            return []

        async def slow_dict(*args, **kwargs):
            await asyncio.sleep(10.0)
            return {}

        engine._rest.get_balance = slow_any
        engine._rest.get_all_orders = slow_list
        engine._rest.get_positions = slow_list
        engine._rest.get_queue_positions = slow_dict
        engine._rest.get_event_positions = slow_list
        engine._rest.get_trades = slow_list

        app = TalosApp(engine=engine)
        async with app.run_test(size=(120, 40)) as pilot:
            # Simulate WS disconnect too
            engine._ws_connected = False
            engine._notify("WEBSOCKET DISCONNECTED: test", "error")

            # Let all the slow polls run for 5s — app should remain responsive
            t0 = time.monotonic()
            await pilot.pause(delay=5.0)
            elapsed = time.monotonic() - t0
            assert elapsed < 10.0, f"All-slow REST blocked: {elapsed:.1f}s"

    async def test_start_feed_reconnection_loop(self) -> None:
        """Exercise the REAL start_feed → disconnect → reconnect path.

        The mock WS connect succeeds, then listen raises immediately
        (simulating instant disconnect). The reconnect loop should retry
        without blocking the event loop.
        """
        engine = _make_engine(n_pairs=5)

        # Make feed.connect succeed but feed.start raise immediately
        connect_count = 0

        async def fake_connect():
            nonlocal connect_count
            connect_count += 1

        async def fake_start():
            raise ConnectionError("Simulated WS disconnect")

        async def fake_subscribe(*a, **kw):
            pass

        engine._feed.connect = AsyncMock(side_effect=fake_connect)
        engine._feed.start = AsyncMock(side_effect=fake_start)
        engine._feed.subscribe_bulk = AsyncMock(side_effect=fake_subscribe)

        # Mock portfolio/lifecycle/ticker/position feeds
        if engine._portfolio_feed is not None:
            engine._portfolio_feed.subscribe = AsyncMock()
        if engine._lifecycle_feed is not None:
            engine._lifecycle_feed.subscribe = AsyncMock()
        if engine._position_feed is not None:
            engine._position_feed.subscribe = AsyncMock()
        if engine._ticker_feed is not None:
            engine._ticker_feed.subscribe = AsyncMock()

        # Run start_feed in a task (it loops forever reconnecting)
        task = asyncio.create_task(engine.start_feed())

        # Wait 12 seconds — should see multiple reconnect cycles (5s each)
        t0 = time.monotonic()
        await asyncio.sleep(12)
        elapsed = time.monotonic() - t0

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Should have reconnected at least twice (12s / 5s = 2.4)
        assert connect_count >= 2, f"Only {connect_count} reconnects in {elapsed:.1f}s"
        # The event loop shouldn't have been blocked
        assert elapsed < 15.0, f"Reconnect loop blocked: {elapsed:.1f}s"

    async def test_start_feed_with_app_running(self) -> None:
        """start_feed reconnect loop running inside the Textual app.

        This is the closest to the real scenario — the reconnect loop
        runs as a @work worker while timers fire.
        """
        engine = _make_engine(n_pairs=10)

        call_log: list[tuple[str, float]] = []

        async def fake_connect():
            call_log.append(("connect", time.monotonic()))

        async def fake_start():
            call_log.append(("start", time.monotonic()))
            raise ConnectionError("Simulated disconnect")

        async def fake_sub(*a, **kw):
            pass

        engine._feed.connect = AsyncMock(side_effect=fake_connect)
        engine._feed.start = AsyncMock(side_effect=fake_start)
        engine._feed.subscribe_bulk = AsyncMock(side_effect=fake_sub)

        app = TalosApp(engine=engine)
        async with app.run_test(size=(120, 40)) as pilot:
            # Let the reconnect loop run for 12s alongside all timers
            t0 = time.monotonic()
            await pilot.pause(delay=12.0)
            elapsed = time.monotonic() - t0

            # Should have been ~12s, not much more
            assert elapsed < 18.0, f"App froze during reconnect: {elapsed:.1f}s"

            connects = [e for e in call_log if e[0] == "connect"]
            assert len(connects) >= 2, f"Only {len(connects)} connects"

    async def test_hanging_rest_with_table_rebuild(self) -> None:
        """Slow REST + table rebuild every 2s — combined load."""
        engine = _make_engine(n_pairs=20)

        async def slow_list(*args, **kwargs):
            await asyncio.sleep(15.0)
            return []

        engine._rest.get_all_orders = slow_list
        engine._rest.get_positions = slow_list

        app = TalosApp(engine=engine)
        async with app.run_test(size=(120, 40)) as pilot:
            engine._ws_connected = False
            # Let table rebuild + slow polls run together
            t0 = time.monotonic()
            await pilot.pause(delay=4.0)
            elapsed = time.monotonic() - t0
            assert elapsed < 8.0, f"Table + slow REST blocked: {elapsed:.1f}s"
