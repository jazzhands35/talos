"""Entry point: python -m talos."""

from __future__ import annotations

from talos.auth import KalshiAuth
from talos.config import KalshiConfig
from talos.game_manager import GameManager
from talos.market_feed import MarketFeed
from talos.orderbook import OrderBookManager
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner
from talos.ui.app import TalosApp
from talos.ws_client import KalshiWSClient


def main() -> None:
    """Launch the Talos dashboard."""
    config = KalshiConfig.from_env()
    auth = KalshiAuth(config.key_id, config.private_key_path)
    rest = KalshiRESTClient(auth, config)
    ws = KalshiWSClient(auth, config)
    books = OrderBookManager()
    feed = MarketFeed(ws, books)
    scanner = ArbitrageScanner(books)
    game_mgr = GameManager(rest, feed, scanner)

    # Wire scanner to book updates
    feed.on_book_update = scanner.scan

    app = TalosApp(
        scanner=scanner,
        game_manager=game_mgr,
        rest_client=rest,
    )
    app.run()


if __name__ == "__main__":
    main()
