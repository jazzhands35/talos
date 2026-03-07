"""Entry point: python -m talos."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _load_dotenv() -> None:
    """Load .env file from project root if it exists."""
    env_file = Path(__file__).resolve().parents[2] / ".env"
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


def main() -> None:
    """Launch the Talos dashboard."""
    _load_dotenv()

    try:
        from talos.config import KalshiConfig

        config = KalshiConfig.from_env()
    except ValueError as e:
        print(f"Configuration error: {e}")
        print()
        print("Create a .env file in the project root (see .env.example):")
        print("  KALSHI_KEY_ID=your-key-id")
        print("  KALSHI_PRIVATE_KEY_PATH=/path/to/private-key.pem")
        print("  KALSHI_ENV=demo")
        sys.exit(1)

    from talos.auth import KalshiAuth
    from talos.game_manager import GameManager
    from talos.market_feed import MarketFeed
    from talos.orderbook import OrderBookManager
    from talos.persistence import load_saved_games, save_games
    from talos.rest_client import KalshiRESTClient
    from talos.scanner import ArbitrageScanner
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
    game_mgr = GameManager(rest, feed, scanner)

    # Wire scanner + tracker to book updates
    def on_book_update(ticker: str) -> None:
        scanner.scan(ticker)
        tracker.check(ticker)

    feed.on_book_update = on_book_update

    # Wire game persistence
    saved_games = load_saved_games()
    game_mgr.on_change = lambda: save_games(
        [p.event_ticker for p in game_mgr.active_games]
    )

    app = TalosApp(
        scanner=scanner,
        game_manager=game_mgr,
        rest_client=rest,
        market_feed=feed,
        tracker=tracker,
        initial_games=saved_games,
    )
    app.run()


if __name__ == "__main__":
    main()
