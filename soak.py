"""Headless soak test harness for Talos.

Runs the TradingEngine without the TUI, auto-discovers pairs via scanner,
and collects metrics for the soak protocol.

Usage:
    python soak.py [--pairs N] [--duration M] [--scan-mode sports|nonsports|both]

Defaults: 15 pairs, 30 minutes, sports scan mode.
structlog goes to stdout (default). Soak status goes to stderr.
Redirect structlog: python soak.py > soak_t1.log
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time

# Load .env before any talos imports
def _load_dotenv() -> None:
    from talos.persistence import get_data_dir
    env_file = get_data_dir() / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value

_load_dotenv()

import structlog

logger = structlog.get_logger()


def _status(msg: str) -> None:
    """Print soak status to stderr (visible even when stdout is redirected)."""
    ts = time.strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}", file=sys.stderr, flush=True)


class SoakMetrics:
    """Collects and summarizes soak test metrics."""

    def __init__(self) -> None:
        self.start_time = time.monotonic()
        self.pair_count_start = 0
        self.pair_count_end = 0
        self.task_samples: list[tuple[float, int]] = []  # (elapsed_s, count)
        self.refresh_account_count = 0
        self.refresh_account_errors = 0
        self.refresh_trades_timeouts = 0

    def sample_tasks(self) -> None:
        count = len(asyncio.all_tasks())
        elapsed = time.monotonic() - self.start_time
        self.task_samples.append((elapsed, count))
        _status(f"task_sample: elapsed={round(elapsed)}s tasks={count}")

    def summary(self, pair_count_end: int) -> str:
        self.pair_count_end = pair_count_end
        elapsed = time.monotonic() - self.start_time
        mins = elapsed / 60

        task_start = self.task_samples[0][1] if self.task_samples else -1
        task_end = self.task_samples[-1][1] if self.task_samples else -1
        task_all = [t[1] for t in self.task_samples]
        task_max = max(task_all) if task_all else -1

        lines = [
            "",
            "=" * 60,
            "  SOAK T1 SUMMARY",
            "=" * 60,
            f"  Duration:          {mins:.1f} min",
            f"  Pairs start:       {self.pair_count_start}",
            f"  Pairs end:         {self.pair_count_end}",
            f"  Task count start:  {task_start}",
            f"  Task count end:    {task_end}",
            f"  Task count max:    {task_max}",
            f"  refresh_account:   {self.refresh_account_count} cycles",
            f"  refresh_errors:    {self.refresh_account_errors}",
            "",
            "  (grep the log file for detailed signal counts —",
            "   see brain/plans/06-runtime-soak/protocol.md)",
            "=" * 60,
            "",
        ]
        return "\n".join(lines)


async def run_soak(target_pairs: int, duration_s: float, scan_mode: str) -> None:
    from talos.auth import KalshiAuth
    from talos.automation_config import DEFAULT_UNIT_SIZE, AutomationConfig
    from talos.bid_adjuster import BidAdjuster
    from talos.config import KalshiConfig
    from talos.engine import TradingEngine
    from talos.game_manager import DEFAULT_NONSPORTS_CATEGORIES, GameManager
    from talos.lifecycle_feed import LifecycleFeed
    from talos.market_feed import MarketFeed
    from talos.orderbook import OrderBookManager
    from talos.persistence import load_saved_games_full, load_settings
    from talos.portfolio_feed import PortfolioFeed
    from talos.position_feed import PositionFeed
    from talos.rest_client import KalshiRESTClient
    from talos.scanner import ArbitrageScanner
    from talos.ticker_feed import TickerFeed
    from talos.top_of_market import TopOfMarketTracker
    from talos.ws_client import KalshiWSClient

    config = KalshiConfig.from_env()
    print(f"  Environment: {config.environment.value}", file=sys.stderr)

    # ── Build subsystems (mirrors __main__.main) ──
    auth = KalshiAuth(config.key_id, config.private_key_path)
    rest = KalshiRESTClient(auth, config)
    ws = KalshiWSClient(auth, config)
    books = OrderBookManager()
    feed = MarketFeed(ws, books)
    scanner = ArbitrageScanner(books)
    tracker = TopOfMarketTracker(books)
    settings = load_settings()
    unit_size = int(settings.get("unit_size", DEFAULT_UNIT_SIZE))
    adjuster = BidAdjuster(books, [], unit_size=unit_size)
    portfolio_feed = PortfolioFeed(ws_client=ws)
    ticker_feed = TickerFeed(ws_client=ws)
    lifecycle_feed = LifecycleFeed(ws_client=ws)
    position_feed = PositionFeed(ws_client=ws)
    auto_config = AutomationConfig()
    nonsports_categories = settings.get("nonsports_categories", DEFAULT_NONSPORTS_CATEGORIES)
    nonsports_max_days = int(settings.get("nonsports_max_days", 7))
    ticker_blacklist = settings.get("ticker_blacklist", [])
    game_mgr = GameManager(
        rest, feed, scanner,
        sports_enabled=auto_config.sports_enabled,
        nonsports_categories=nonsports_categories,
        nonsports_max_days=nonsports_max_days,
        ticker_blacklist=ticker_blacklist,
    )

    # Wire scanner to book updates (no UI callback)
    def on_book_update(ticker: str) -> None:
        scanner.scan(ticker)
        for pair in scanner.pairs_for_ticker(ticker):
            for side_str in {pair.side_a, pair.side_b}:
                tracker.check(ticker, side=side_str)

    feed.on_book_update = on_book_update

    # Don't pass saved games to engine — we restore manually with a cap
    engine = TradingEngine(
        scanner=scanner,
        game_manager=game_mgr,
        rest_client=rest,
        market_feed=feed,
        tracker=tracker,
        adjuster=adjuster,
        initial_games=[],
        initial_games_full=None,
        automation_config=auto_config,
        portfolio_feed=portfolio_feed,
        ticker_feed=ticker_feed,
        lifecycle_feed=lifecycle_feed,
        position_feed=position_feed,
    )

    # Mark initial sync done so refresh_account fetches only resting orders
    # (not full history). We're not placing bids — just observing.
    engine._initial_sync_done = True

    metrics = SoakMetrics()
    shutdown = asyncio.Event()

    # Handle Ctrl+C
    def _signal_handler() -> None:
        logger.info("soak_shutdown_requested")
        shutdown.set()

    loop = asyncio.get_running_loop()
    # Windows doesn't support loop.add_signal_handler, use threading fallback
    if sys.platform == "win32":
        def _win_handler(sig: int, frame: object) -> None:
            loop.call_soon_threadsafe(shutdown.set)
        signal.signal(signal.SIGINT, _win_handler)
    else:
        loop.add_signal_handler(signal.SIGINT, _signal_handler)

    # ── Phase 1: Connect WS and restore/discover games ──
    _status("Connecting WebSocket...")
    await feed.connect()
    _status("WebSocket connected")

    # Restore saved games (capped at target_pairs)
    saved_games_full = load_saved_games_full()
    if saved_games_full:
        restored = 0
        for data in saved_games_full:
            if restored >= target_pairs:
                break
            try:
                pair = game_mgr.restore_game(data)
                if pair is None:
                    continue
                adjuster.add_event(pair)
                saved_ledger = data.get("ledger")
                if saved_ledger:
                    ledger = adjuster.get_ledger(pair.event_ticker)
                    ledger.seed_from_saved(saved_ledger)
                restored += 1
            except Exception:
                logger.warning("soak_restore_failed", event=data.get("event_ticker"))
        tickers = [t for p in game_mgr.active_games for t in (p.ticker_a, p.ticker_b)]
        if tickers:
            await feed.subscribe_bulk(tickers)
        _status(f"Restored {restored} games from cache")

    # ── Phase 2: Scan and add games up to target ──
    current = len(scanner.pairs)
    if current < target_pairs:
        _status(f"Have {current} pairs, scanning for more (target: {target_pairs})...")
        try:
            events = await game_mgr.scan_events(scan_mode=scan_mode)
            _status(f"Scan found {len(events)} events")

            to_add = []
            for event in events:
                if len(scanner.pairs) + len(to_add) >= target_pairs:
                    break
                to_add.append(event.event_ticker)

            if to_add:
                pairs = await game_mgr.add_games(to_add)
                for pair in pairs:
                    adjuster.add_event(pair)
                _status(f"Added {len(pairs)} games from scan")
        except Exception:
            logger.warning("soak_scan_failed", exc_info=True)

    metrics.pair_count_start = len(scanner.pairs)
    _status(f"Monitoring {metrics.pair_count_start} pairs")

    if metrics.pair_count_start == 0:
        _status("ERROR: No pairs to monitor. Exiting.")
        await rest.close()
        return

    # Subscribe to feeds
    await portfolio_feed.subscribe()
    await lifecycle_feed.subscribe()
    await position_feed.subscribe()
    market_tickers = [t for p in scanner.pairs for t in (p.ticker_a, p.ticker_b)]
    if market_tickers:
        await ticker_feed.subscribe(market_tickers)

    # ── Phase 3: Run polling loops ──
    metrics.sample_tasks()

    async def ws_listener() -> None:
        """Listen for WS messages until shutdown."""
        try:
            await feed.start()
        except Exception as e:
            logger.error("soak_ws_listener_error", error=str(e))

    async def refresh_account_loop() -> None:
        """30s REST backup sync — same as TUI."""
        await asyncio.sleep(5)  # let WS deliver initial snapshots
        while not shutdown.is_set():
            t0 = time.monotonic()
            try:
                await engine.refresh_account()
                elapsed = time.monotonic() - t0
                metrics.refresh_account_count += 1
                _status(f"refresh_account OK ({elapsed:.1f}s, {len(scanner.pairs)} pairs)")
            except Exception as e:
                elapsed = time.monotonic() - t0
                metrics.refresh_account_errors += 1
                _status(f"refresh_account ERROR ({elapsed:.1f}s): {type(e).__name__}: {e}")
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=30.0)
                return
            except TimeoutError:
                pass

    async def refresh_trades_loop() -> None:
        """30s trade fetch — same as TUI."""
        await asyncio.sleep(10)  # let refresh_account populate orders_cache first
        while not shutdown.is_set():
            t0 = time.monotonic()
            try:
                await engine.refresh_trades()
                elapsed = time.monotonic() - t0
                _status(f"refresh_trades OK ({elapsed:.1f}s)")
            except Exception as e:
                elapsed = time.monotonic() - t0
                _status(f"refresh_trades ERROR ({elapsed:.1f}s): {type(e).__name__}: {e}")
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=30.0)
                return
            except TimeoutError:
                pass

    async def task_sampler() -> None:
        """Sample task count every 60s."""
        while not shutdown.is_set():
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=60.0)
                return
            except TimeoutError:
                metrics.sample_tasks()

    async def duration_timer() -> None:
        """Shut down after the specified duration."""
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=duration_s)
        except TimeoutError:
            logger.info("soak_duration_reached", duration_s=duration_s)
            shutdown.set()

    _status(f"Running for {duration_s / 60:.0f} minutes. Ctrl+C to stop early.")
    _status("structlog → stdout. Redirect with: python soak.py > soak_t1.log")

    # Launch all loops
    tasks = [
        asyncio.create_task(ws_listener(), name="ws_listener"),
        asyncio.create_task(refresh_account_loop(), name="refresh_account"),
        asyncio.create_task(refresh_trades_loop(), name="refresh_trades"),
        asyncio.create_task(task_sampler(), name="task_sampler"),
        asyncio.create_task(duration_timer(), name="duration_timer"),
    ]

    # Wait for shutdown signal
    await shutdown.wait()

    # Final sample
    metrics.sample_tasks()

    # Cancel tasks
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # Print summary
    summary = metrics.summary(pair_count_end=len(scanner.pairs))
    print(summary)

    # Cleanup
    try:
        await rest.close()
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Talos headless soak test")
    parser.add_argument("--pairs", type=int, default=15, help="Target pair count (default: 15)")
    parser.add_argument("--duration", type=int, default=30, help="Duration in minutes (default: 30)")
    parser.add_argument("--scan-mode", default="sports", choices=["sports", "nonsports", "both"],
                        help="Scan mode for discovery (default: sports)")
    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  Talos Soak Test")
    print(f"  Target: {args.pairs} pairs, {args.duration} min, {args.scan_mode}")
    print("=" * 60)

    asyncio.run(run_soak(
        target_pairs=args.pairs,
        duration_s=args.duration * 60,
        scan_mode=args.scan_mode,
    ))


if __name__ == "__main__":
    main()
