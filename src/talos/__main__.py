"""Entry point: python -m talos."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _load_dotenv() -> None:
    """Load .env file from data directory if it exists."""
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


def _run_first_time_setup() -> None:
    """Launch the first-run setup screen to collect credentials."""
    from talos.ui.first_run import FirstRunApp

    app = FirstRunApp()
    app.run()


def main() -> None:
    """Launch the Talos dashboard."""
    # Frozen mode (PyInstaller): set data dir to exe's directory
    if getattr(sys, "frozen", False):
        from talos.persistence import set_data_dir

        set_data_dir(Path(sys.executable).parent)

    _load_dotenv()

    # Production-only guard for frozen builds
    if getattr(sys, "frozen", False) and os.environ.get("KALSHI_ENV") != "production":
        os.environ["KALSHI_ENV"] = "production"

    try:
        from talos.config import KalshiConfig

        config = KalshiConfig.from_env()
    except ValueError:
        # No .env yet — launch first-run setup if frozen, else error out
        if getattr(sys, "frozen", False):
            _run_first_time_setup()
            # Reload .env and retry — exit if still broken
            _load_dotenv()
            try:
                config = KalshiConfig.from_env()
            except ValueError:
                print("Setup did not complete — exiting.")
                sys.exit(1)
        else:
            print("Configuration error — create a .env file (see .env.example)")
            sys.exit(1)

    from talos.auth import KalshiAuth
    from talos.automation_config import AutomationConfig
    from talos.bid_adjuster import BidAdjuster
    from talos.data_collector import DataCollector
    from talos.engine import TradingEngine
    from talos.game_manager import DEFAULT_NONSPORTS_CATEGORIES, GameManager
    from talos.game_status import GameStatusResolver
    from talos.lifecycle_feed import LifecycleFeed
    from talos.market_feed import MarketFeed
    from talos.orderbook import OrderBookManager
    from talos.persistence import (
        load_saved_games,
        load_saved_games_full,
        load_settings,
        save_games,
        save_games_full,
        save_settings,
    )
    from talos.portfolio_feed import PortfolioFeed
    from talos.position_feed import PositionFeed
    from talos.rest_client import KalshiRESTClient
    from talos.scanner import ArbitrageScanner
    from talos.settlement_tracker import SettlementCache
    from talos.suggestion_log import SuggestionLog
    from talos.ticker_feed import TickerFeed
    from talos.top_of_market import TopOfMarketTracker
    from talos.ui.app import TalosApp
    from talos.ws_client import KalshiWSClient

    auth = KalshiAuth(config.key_id, config.private_key_path)
    rest = KalshiRESTClient(auth, config)
    ws = KalshiWSClient(auth, config)
    books = OrderBookManager()
    feed = MarketFeed(ws, books)
    scanner = ArbitrageScanner(books)
    tracker = TopOfMarketTracker(books)
    settings = load_settings()
    unit_size = int(settings.get("unit_size", 5))  # type: ignore[arg-type]
    adjuster = BidAdjuster(books, [], unit_size=unit_size)
    portfolio_feed = PortfolioFeed(ws_client=ws)
    ticker_feed = TickerFeed(ws_client=ws)
    lifecycle_feed = LifecycleFeed(ws_client=ws)
    position_feed = PositionFeed(ws_client=ws)
    auto_config = AutomationConfig()
    nonsports_categories = settings.get("nonsports_categories", DEFAULT_NONSPORTS_CATEGORIES)
    nonsports_max_days = int(settings.get("nonsports_max_days", 7))  # type: ignore[arg-type]
    ticker_blacklist = settings.get("ticker_blacklist", [])
    game_mgr = GameManager(
        rest, feed, scanner,
        sports_enabled=auto_config.sports_enabled,
        nonsports_categories=nonsports_categories,  # type: ignore[arg-type]
        nonsports_max_days=nonsports_max_days,
        ticker_blacklist=ticker_blacklist,  # type: ignore[arg-type]
    )
    game_status_resolver = GameStatusResolver()
    from talos.persistence import get_data_dir

    db_dir = get_data_dir()
    data_collector = DataCollector(db_dir / "talos_data.db")
    settlement_cache = SettlementCache(db_dir / "talos_data.db")

    # Wire scanner + tracker to book updates
    _app_ref: list[TalosApp] = []  # populated after app creation

    def on_book_update(ticker: str) -> None:
        scanner.scan(ticker)
        # Check both sides for each pair using this ticker
        for pair in scanner._pairs_by_ticker.get(ticker, []):
            for side_str in {pair.side_a, pair.side_b}:
                tracker.check(ticker, side=side_str)
        # Mark affected events dirty for table refresh
        if _app_ref:
            for pair in scanner._pairs_by_ticker.get(ticker, []):
                _app_ref[0].mark_event_dirty(pair.event_ticker)

    feed.on_book_update = on_book_update

    # Wire game persistence — save full pair data for instant startup
    saved_games_full = load_saved_games_full()
    saved_games = load_saved_games() if saved_games_full is None else []

    def _persist_games() -> None:
        save_games([p.event_ticker for p in game_mgr.active_games])
        save_games_full([
            {
                "event_ticker": p.event_ticker,
                "ticker_a": p.ticker_a,
                "ticker_b": p.ticker_b,
                "fee_type": p.fee_type,
                "fee_rate": p.fee_rate,
                "close_time": p.close_time,
                "expected_expiration_time": p.expected_expiration_time,
                "label": game_mgr.labels.get(p.event_ticker, ""),
                "sub_title": game_mgr.subtitles.get(p.event_ticker, ""),
                "side_a": p.side_a,
                "side_b": p.side_b,
                "kalshi_event_ticker": p.kalshi_event_ticker,
                "series_ticker": p.series_ticker,
            }
            for p in game_mgr.active_games
        ])

    game_mgr.on_change = _persist_games

    engine = TradingEngine(
        scanner=scanner,
        game_manager=game_mgr,
        rest_client=rest,
        market_feed=feed,
        tracker=tracker,
        adjuster=adjuster,
        initial_games=saved_games,
        initial_games_full=saved_games_full,
        automation_config=auto_config,
        portfolio_feed=portfolio_feed,
        ticker_feed=ticker_feed,
        lifecycle_feed=lifecycle_feed,
        position_feed=position_feed,
        game_status_resolver=game_status_resolver,
        data_collector=data_collector,
        settlement_cache=settlement_cache,
    )

    # Wire unit size persistence
    def _persist_unit_size(size: int) -> None:
        s = load_settings()
        s["unit_size"] = size
        save_settings(s)

    engine.on_unit_size_change = _persist_unit_size

    # Wire blacklist persistence
    def _persist_blacklist(blacklist: list[str]) -> None:
        s = load_settings()
        s["ticker_blacklist"] = blacklist
        save_settings(s)

    engine.on_blacklist_change = _persist_blacklist

    # Wire suggestion audit log
    log_path = get_data_dir() / "suggestions.log"
    suggestion_log = SuggestionLog(log_path)
    engine.proposal_queue.on_lifecycle = suggestion_log.log

    app = TalosApp(engine=engine)
    _app_ref.append(app)
    app.run()


if __name__ == "__main__":
    main()
