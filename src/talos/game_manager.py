"""Game lifecycle manager — sets up monitoring from Kalshi URLs."""

from __future__ import annotations

from urllib.parse import urlparse

import structlog

from talos.market_feed import MarketFeed
from talos.models.strategy import ArbPair
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner

logger = structlog.get_logger()


def parse_kalshi_url(url_or_ticker: str) -> str:
    """Extract event ticker from a Kalshi URL or return bare ticker.

    Accepted formats:
      - https://kalshi.com/markets/series/slug/EVENT-TICKER
      - EVENT-TICKER (bare)
    """
    if not url_or_ticker.strip():
        raise ValueError("URL or ticker is empty")

    parsed = urlparse(url_or_ticker)
    if parsed.scheme and parsed.netloc:
        if "kalshi.com" not in parsed.netloc:
            raise ValueError(f"Not a Kalshi URL: {parsed.netloc}")
        path = parsed.path.rstrip("/")
        return path.rsplit("/", 1)[-1]

    return url_or_ticker.strip()


class GameManager:
    """Orchestrates game setup, teardown, and ties layers together.

    Async — owns REST calls and feed subscriptions.
    """

    def __init__(
        self,
        rest: KalshiRESTClient,
        feed: MarketFeed,
        scanner: ArbitrageScanner,
    ) -> None:
        self._rest = rest
        self._feed = feed
        self._scanner = scanner
        self._games: dict[str, ArbPair] = {}

    async def add_game(self, url_or_ticker: str) -> ArbPair:
        """Set up monitoring for a game from a URL or event ticker."""
        ticker = parse_kalshi_url(url_or_ticker)

        if ticker in self._games:
            return self._games[ticker]

        event = await self._rest.get_event(ticker, with_nested_markets=True)

        if len(event.markets) != 2:
            raise ValueError(f"Event {ticker} has {len(event.markets)} markets, expected exactly 2")

        ticker_a = event.markets[0].ticker
        ticker_b = event.markets[1].ticker

        pair = ArbPair(event_ticker=event.event_ticker, ticker_a=ticker_a, ticker_b=ticker_b)
        self._scanner.add_pair(event.event_ticker, ticker_a, ticker_b)
        await self._feed.subscribe(ticker_a)
        await self._feed.subscribe(ticker_b)
        self._games[event.event_ticker] = pair

        logger.info(
            "game_added",
            event_ticker=event.event_ticker,
            a=ticker_a,
            b=ticker_b,
            title=event.title,
        )
        return pair

    async def add_games(self, urls: list[str]) -> list[ArbPair]:
        """Set up monitoring for multiple games."""
        pairs = []
        for url in urls:
            pair = await self.add_game(url)
            pairs.append(pair)
        return pairs

    async def remove_game(self, event_ticker: str) -> None:
        """Remove a game from monitoring."""
        pair = self._games.pop(event_ticker, None)
        if pair is None:
            return
        self._scanner.remove_pair(event_ticker)
        await self._feed.unsubscribe(pair.ticker_a)
        await self._feed.unsubscribe(pair.ticker_b)
        logger.info("game_removed", event_ticker=event_ticker)

    @property
    def active_games(self) -> list[ArbPair]:
        """Currently monitored games."""
        return list(self._games.values())
