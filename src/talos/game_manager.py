"""Game lifecycle manager — sets up monitoring from Kalshi URLs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from urllib.parse import urlparse

import structlog

from talos.errors import KalshiAPIError
from talos.market_feed import MarketFeed
from talos.models.market import Event
from talos.models.strategy import ArbPair
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner

logger = structlog.get_logger()

SCAN_SERIES = [
    "KXNHLGAME", "KXNBAGAME", "KXMLBGAME", "KXNFLGAME", "KXWNBAGAME",
    "KXCFBGAME", "KXCBBGAME", "KXMLSGAME", "KXEPLGAME",
    "KXAHLGAME",
    "KXLOLGAME", "KXCS2GAME", "KXVALGAME", "KXDOTA2GAME", "KXCODGAME",
    "KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH",
    "KXATPDOUBLES",
]


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
        # Kalshi website uses lowercase URLs but API tickers are uppercase
        return path.rsplit("/", 1)[-1].upper()

    return url_or_ticker.strip().upper()


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
        self._labels: dict[str, str] = {}
        self._subtitles: dict[str, str] = {}
        self._volumes_24h: dict[str, int] = {}  # market_ticker -> 24h volume
        self.on_change: Callable[[], None] | None = None

    async def add_game(self, url_or_ticker: str, *, subscribe: bool = True) -> ArbPair:
        """Set up monitoring for a game from a URL or event ticker."""
        ticker = parse_kalshi_url(url_or_ticker)

        if ticker in self._games:
            return self._games[ticker]

        try:
            event = await self._rest.get_event(ticker, with_nested_markets=True)
        except KalshiAPIError as e:
            if e.status_code != 404:
                raise
            # Might be a market ticker — resolve to event ticker
            logger.debug("event_not_found_trying_market", ticker=ticker)
            market = await self._rest.get_market(ticker)
            event = await self._rest.get_event(market.event_ticker, with_nested_markets=True)

        if len(event.markets) != 2:
            raise ValueError(f"Event {ticker} has {len(event.markets)} markets, expected exactly 2")

        ticker_a = event.markets[0].ticker
        ticker_b = event.markets[1].ticker

        # Extract earliest close_time from the two markets
        close_times = [m.close_time for m in event.markets if m.close_time]
        close_time = min(close_times) if close_times else None

        # Fetch series for fee metadata (non-critical — default if it fails)
        fee_type = "quadratic_with_maker_fees"
        fee_rate = 0.0175
        try:
            series = await self._rest.get_series(event.series_ticker)
            fee_type = series.fee_type
            fee_rate = series.fee_multiplier
            logger.info(
                "series_fee_info",
                series=event.series_ticker,
                fee_type=fee_type,
                fee_rate=fee_rate,
            )
        except Exception:
            logger.warning(
                "series_fee_fetch_failed",
                series=event.series_ticker,
                exc_info=True,
            )

        pair = ArbPair(
            event_ticker=event.event_ticker,
            ticker_a=ticker_a,
            ticker_b=ticker_b,
            fee_type=fee_type,
            fee_rate=fee_rate,
            close_time=close_time,
        )
        self._scanner.add_pair(
            event.event_ticker,
            ticker_a,
            ticker_b,
            fee_type=fee_type,
            fee_rate=fee_rate,
            close_time=close_time,
        )
        if subscribe:
            await self._feed.subscribe(ticker_a)
            await self._feed.subscribe(ticker_b)
        self._games[event.event_ticker] = pair

        # Store raw sub_title for game status resolver
        self._subtitles[event.event_ticker] = event.sub_title

        # Store 24h volume per market ticker
        for m in event.markets:
            self._volumes_24h[m.ticker] = m.volume_24h or 0

        # Build short display label from sub_title
        label = event.sub_title or event.title
        # sub_title is like "WAKE at VT (Mar 10)" — strip date suffix
        if "(" in label:
            label = label[: label.rfind("(")].strip()
        # Compact separators
        for sep in (" vs ", " at ", " vs. "):
            label = label.replace(sep, "-")
        self._labels[event.event_ticker] = label

        if self.on_change:
            self.on_change()

        logger.info(
            "game_added",
            event_ticker=event.event_ticker,
            a=ticker_a,
            b=ticker_b,
            title=event.title,
        )
        return pair

    async def add_games(self, urls: list[str]) -> list[ArbPair]:
        """Set up monitoring for multiple games concurrently.

        Defers feed subscriptions and does a single bulk subscribe at the end,
        reducing WS roundtrips from 2N to 1. Semaphore-limited to stay under
        Kalshi's 20 reads/sec rate limit.
        """
        sem = asyncio.Semaphore(10)

        async def _add(url: str) -> ArbPair | None:
            async with sem:
                try:
                    return await self.add_game(url, subscribe=False)
                except Exception:
                    logger.warning("add_game_failed", url=url, exc_info=True)
                    return None

        results = await asyncio.gather(*(_add(url) for url in urls))
        pairs = [p for p in results if p is not None]
        tickers = [t for p in pairs for t in (p.ticker_a, p.ticker_b)]
        if tickers:
            await self._feed.subscribe_bulk(tickers)
        return pairs

    async def remove_game(self, event_ticker: str) -> None:
        """Remove a game from monitoring."""
        pair = self._games.pop(event_ticker, None)
        if pair is None:
            return
        self._labels.pop(event_ticker, None)
        self._subtitles.pop(event_ticker, None)
        self._volumes_24h.pop(pair.ticker_a, None)
        self._volumes_24h.pop(pair.ticker_b, None)
        self._scanner.remove_pair(event_ticker)
        await self._feed.unsubscribe(pair.ticker_a)
        await self._feed.unsubscribe(pair.ticker_b)
        if self.on_change:
            self.on_change()
        logger.info("game_removed", event_ticker=event_ticker)

    async def clear_all_games(self) -> None:
        """Remove all games from monitoring."""
        tickers = list(self._games.keys())
        for ticker in tickers:
            pair = self._games.pop(ticker)
            self._labels.pop(ticker, None)
            self._subtitles.pop(ticker, None)
            self._volumes_24h.pop(pair.ticker_a, None)
            self._volumes_24h.pop(pair.ticker_b, None)
            self._scanner.remove_pair(ticker)
            await self._feed.unsubscribe(pair.ticker_a)
            await self._feed.unsubscribe(pair.ticker_b)
        if self.on_change:
            self.on_change()
        logger.info("all_games_cleared", count=len(tickers))

    async def scan_events(self) -> list[Event]:
        """Discover all open arb-eligible events not already monitored."""
        active_tickers = {p.event_ticker for p in self.active_games}
        sem = asyncio.Semaphore(4)

        async def fetch_series(series: str) -> list[Event]:
            async with sem:
                try:
                    return await self._rest.get_events(
                        series_ticker=series, status="open",
                        with_nested_markets=True, limit=200,
                    )
                except Exception:
                    logger.warning("scan_series_failed", series=series, exc_info=True)
                    return []

        all_results = await asyncio.gather(*(fetch_series(s) for s in SCAN_SERIES))

        events: list[Event] = []
        for batch in all_results:
            for event in batch:
                if event.event_ticker in active_tickers:
                    continue
                if len(event.markets) != 2:
                    continue
                if all(m.status in ("settled", "determined") for m in event.markets):
                    continue
                events.append(event)
        return events

    @property
    def active_games(self) -> list[ArbPair]:
        """Currently monitored games."""
        return list(self._games.values())

    @property
    def labels(self) -> dict[str, str]:
        """Event ticker -> short display label."""
        return dict(self._labels)

    @property
    def subtitles(self) -> dict[str, str]:
        """Event ticker -> raw sub_title from Kalshi."""
        return dict(self._subtitles)

    @property
    def volumes_24h(self) -> dict[str, int]:
        """Market ticker -> 24h volume in contracts."""
        return dict(self._volumes_24h)
