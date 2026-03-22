"""Game lifecycle manager — sets up monitoring from Kalshi URLs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from urllib.parse import urlparse

import structlog

from talos.errors import KalshiAPIError
from talos.market_feed import MarketFeed
from talos.models.market import Event, Market
from talos.models.strategy import ArbPair
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner

logger = structlog.get_logger()

SPORTS_SERIES = [
    "KXNHLGAME",
    "KXNBAGAME",
    "KXMLBGAME",
    "KXNFLGAME",
    "KXWNBAGAME",
    "KXCFBGAME",
    "KXCBBGAME",
    "KXMLSGAME",
    "KXEPLGAME",
    "KXAHLGAME",
    "KXLOLGAME",
    "KXCS2GAME",
    "KXVALGAME",
    "KXDOTA2GAME",
    "KXCODGAME",
    "KXATPMATCH",
    "KXWTAMATCH",
    "KXATPCHALLENGERMATCH",
    "KXWTACHALLENGERMATCH",
    "KXATPDOUBLES",
    # Soccer — European leagues
    "KXLALIGAGAME",
    "KXBUNDESLIGAGAME",
    "KXSERIEAGAME",
    "KXLIGUE1GAME",
    "KXUCLGAME",
    "KXLIGAMXGAME",
    "KXKLEAGUEGAME",
    # Hockey — international
    "KXSHLGAME",
    "KXKHLGAME",
    # Basketball — international
    "KXEUROLEAGUEGAME",
    "KXNBLGAME",
    "KXBBLGAME",
    "KXCBAGAME",
    "KXKBLGAME",
    # MMA / Boxing
    "KXUFCFIGHT",
    "KXBOXING",
    # Cricket
    "KXT20MATCH",
    "KXIPL",
    "KXCRICKETODIMATCH",
    # Rugby
    "KXRUGBYNRLMATCH",
    # Aussie Rules
    "KXAFLGAME",
    # Lacrosse
    "KXNCAAMLAXGAME",
    # Darts (tournament — works at finals)
    "KXPREMDARTS",
    # Chess (tournament — works at finals)
    "KXCHESSWORLDCHAMPION",
    "KXCHESSCANDIDATES",
    # Motorsport (tournament — works at finals)
    "KXF1",
    "KXNASCARRACE",
    "KXINDYCARRACE",
    # Golf (tournament — works at finals)
    "KXPGATOUR",
    # Tournament winner (only shows when down to 2 active markets / finals)
    "KXIWMEN",
    "KXIWWMN",
]

NON_SPORTS_SERIES: list[str] = []

_SPORTS_SET = set(SPORTS_SERIES)

# Backward-compatible alias for external consumers
SCAN_SERIES = SPORTS_SERIES


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


def extract_leg_labels(sub_title: str) -> tuple[str, str]:
    """Extract per-leg team names from event sub_title.

    Handles formats like:
    - "Boston Bruins vs Washington Capitals (Mar 19)"
    - "Wake Forest at Virginia Tech (Mar 10)"

    Returns (team_a, team_b) tuple. Falls back to (full, full) if unparseable.
    """
    if not sub_title:
        return ("", "")
    label = sub_title
    if "(" in label:
        label = label[: label.rfind("(")].strip()
    for sep in (" vs ", " vs. ", " at "):
        if sep in label:
            parts = label.split(sep, 1)
            return (parts[0].strip(), parts[1].strip())
    return (label, label)


class GameManager:
    """Orchestrates game setup, teardown, and ties layers together.

    Async — owns REST calls and feed subscriptions.
    """

    def __init__(
        self,
        rest: KalshiRESTClient,
        feed: MarketFeed,
        scanner: ArbitrageScanner,
        *,
        sports_enabled: bool = True,
    ) -> None:
        self._rest = rest
        self._feed = feed
        self._scanner = scanner
        self._sports_enabled = sports_enabled
        self._games: dict[str, ArbPair] = {}
        self._labels: dict[str, str] = {}
        self._subtitles: dict[str, str] = {}
        self._leg_labels: dict[str, tuple[str, str]] = {}
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

        # Sports block check
        if not self._sports_enabled and event.series_ticker in _SPORTS_SET:
            raise ValueError(f"Sports markets blocked: {event.series_ticker}")

        # Filter to active markets only (tournament events have many finalized markets)
        active_markets = [m for m in event.markets if m.status == "active"]

        if event.series_ticker in _SPORTS_SET:
            # Sports path: exactly 2 markets (cross-NO arb)
            if len(active_markets) != 2:
                raise ValueError(
                    f"Event {ticker} has {len(active_markets)} active markets "
                    f"({len(event.markets)} total), expected exactly 2"
                )
        else:
            # Non-sports path
            if len(active_markets) == 0:
                raise ValueError(f"Event {ticker} has no active markets")
            if len(active_markets) == 1:
                # Auto-add single market as YES/NO pair
                return await self.add_market_as_pair(
                    event, active_markets[0], subscribe=subscribe,
                )
            # Multiple markets — caller needs to show market picker
            raise ValueError(
                f"Event {ticker} has {len(active_markets)} active markets — "
                f"use market picker to select specific markets"
            )

        ticker_a = active_markets[0].ticker
        ticker_b = active_markets[1].ticker

        # Extract earliest close_time from the active markets
        close_times = [m.close_time for m in active_markets if m.close_time]
        close_time = min(close_times) if close_times else None

        # Extract expected_expiration_time (same for both markets in an event)
        exp_times = [
            m.expected_expiration_time for m in active_markets if m.expected_expiration_time
        ]
        expected_expiration_time = exp_times[0] if exp_times else None

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
            expected_expiration_time=expected_expiration_time,
        )
        self._scanner.add_pair(
            event.event_ticker,
            ticker_a,
            ticker_b,
            fee_type=fee_type,
            fee_rate=fee_rate,
            close_time=close_time,
            expected_expiration_time=expected_expiration_time,
        )
        if subscribe:
            await self._feed.subscribe(ticker_a)
            await self._feed.subscribe(ticker_b)
        self._games[event.event_ticker] = pair

        # Store raw sub_title for game status resolver
        self._subtitles[event.event_ticker] = event.sub_title

        # Store 24h volume per active market ticker
        for m in active_markets:
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
        self._leg_labels[event.event_ticker] = extract_leg_labels(event.sub_title or event.title)

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

    async def add_market_as_pair(
        self, event: Event, market: Market, *, subscribe: bool = True,
    ) -> ArbPair:
        """Create a YES/NO arb pair from a single market within an event."""
        if market.ticker in self._games:
            return self._games[market.ticker]

        # Fetch series for fee metadata
        fee_type = "quadratic_with_maker_fees"
        fee_rate = 0.0175
        try:
            series = await self._rest.get_series(event.series_ticker)
            fee_type = series.fee_type
            fee_rate = series.fee_multiplier
        except Exception:
            logger.warning(
                "series_fee_fetch_failed", series=event.series_ticker, exc_info=True,
            )

        pair = ArbPair(
            event_ticker=market.ticker,  # market ticker as unique pair key
            ticker_a=market.ticker,
            ticker_b=market.ticker,
            side_a="yes",
            side_b="no",
            kalshi_event_ticker=event.event_ticker,
            fee_type=fee_type,
            fee_rate=fee_rate,
            close_time=market.close_time,
            expected_expiration_time=market.expected_expiration_time,
        )
        self._scanner.add_pair(
            market.ticker,
            market.ticker,
            market.ticker,
            side_a="yes",
            side_b="no",
            kalshi_event_ticker=event.event_ticker,
            fee_type=fee_type,
            fee_rate=fee_rate,
            close_time=market.close_time,
            expected_expiration_time=market.expected_expiration_time,
        )
        if subscribe:
            await self._feed.subscribe(market.ticker)
        self._games[market.ticker] = pair

        # Store metadata
        self._subtitles[market.ticker] = event.sub_title
        self._volumes_24h[market.ticker] = market.volume_24h or 0

        # Build YES/NO labels from market title
        short = (market.title or "").removeprefix("Will ").removesuffix("?").strip()
        if len(short) > 30:
            short = short[:27] + "..."
        self._labels[market.ticker] = short
        self._leg_labels[market.ticker] = (f"{short} - YES", f"{short} - NO")

        if self.on_change:
            self.on_change()
        logger.info(
            "market_pair_added",
            market_ticker=market.ticker,
            event_ticker=event.event_ticker,
        )
        return pair

    def restore_game(self, data: dict[str, str | float]) -> ArbPair | None:
        """Restore a game from cached data — no REST calls needed.

        Returns None when the pair is a sports pair and sports are disabled.
        """
        event_ticker = str(data["event_ticker"])

        # Sports block check
        if not self._sports_enabled:
            series_prefix = event_ticker.split("-")[0]
            if series_prefix in _SPORTS_SET:
                logger.info("restore_skipped_sports", event_ticker=event_ticker)
                return None

        if event_ticker in self._games:
            return self._games[event_ticker]

        ticker_a = str(data["ticker_a"])
        ticker_b = str(data["ticker_b"])

        # Read new fields with backward-compatible defaults
        side_a = str(data.get("side_a", "no"))
        side_b = str(data.get("side_b", "no"))
        kalshi_event_ticker = str(data.get("kalshi_event_ticker", ""))

        pair = ArbPair(
            event_ticker=event_ticker,
            ticker_a=ticker_a,
            ticker_b=ticker_b,
            side_a=side_a,
            side_b=side_b,
            kalshi_event_ticker=kalshi_event_ticker,
            fee_type=str(data.get("fee_type", "quadratic_with_maker_fees")),
            fee_rate=float(data.get("fee_rate", 0.0175)),
            close_time=str(data["close_time"]) if data.get("close_time") else None,
            expected_expiration_time=(
                str(data["expected_expiration_time"])
                if data.get("expected_expiration_time")
                else None
            ),
        )
        self._scanner.add_pair(
            event_ticker,
            ticker_a,
            ticker_b,
            side_a=side_a,
            side_b=side_b,
            kalshi_event_ticker=kalshi_event_ticker,
            fee_type=pair.fee_type,
            fee_rate=pair.fee_rate,
            close_time=pair.close_time,
            expected_expiration_time=pair.expected_expiration_time,
        )
        self._games[event_ticker] = pair
        if "sub_title" in data:
            self._subtitles[event_ticker] = str(data["sub_title"])
            self._leg_labels[event_ticker] = extract_leg_labels(str(data["sub_title"]))
        elif "label" in data:
            self._leg_labels[event_ticker] = (str(data["label"]), str(data["label"]))
        if "label" in data:
            self._labels[event_ticker] = str(data["label"])
        if self.on_change:
            self.on_change()
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
        self._leg_labels.pop(event_ticker, None)
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
            self._leg_labels.pop(ticker, None)
            self._volumes_24h.pop(pair.ticker_a, None)
            self._volumes_24h.pop(pair.ticker_b, None)
            self._scanner.remove_pair(ticker)
            await self._feed.unsubscribe(pair.ticker_a)
            await self._feed.unsubscribe(pair.ticker_b)
        if self.on_change:
            self.on_change()
        logger.info("all_games_cleared", count=len(tickers))

    async def refresh_volumes(self) -> None:
        """Re-fetch 24h volume for all monitored markets, batched by series."""
        # Group active games by series prefix
        series_tickers: set[str] = set()
        for pair in self.active_games:
            prefix = pair.event_ticker.split("-")[0]
            series_tickers.add(prefix)

        sem = asyncio.Semaphore(4)

        async def _fetch(series: str) -> list[Event]:
            async with sem:
                try:
                    return await self._rest.get_events(
                        series_ticker=series,
                        status="open",
                        with_nested_markets=True,
                        limit=200,
                    )
                except Exception:
                    return []

        results = await asyncio.gather(*(_fetch(s) for s in series_tickers))
        for batch in results:
            for event in batch:
                for m in event.markets:
                    if m.volume_24h is not None:
                        self._volumes_24h[m.ticker] = m.volume_24h

    async def scan_events(self) -> list[Event]:
        """Discover all open arb-eligible events not already monitored."""
        active_tickers = {p.event_ticker for p in self.active_games}
        sem = asyncio.Semaphore(4)

        # Use appropriate series list based on sports toggle
        series_list: list[str] = []
        if self._sports_enabled:
            series_list.extend(SPORTS_SERIES)
        series_list.extend(NON_SPORTS_SERIES)

        async def fetch_series(series: str) -> list[Event]:
            async with sem:
                try:
                    return await self._rest.get_events(
                        series_ticker=series,
                        status="open",
                        with_nested_markets=True,
                        limit=200,
                    )
                except Exception:
                    logger.warning("scan_series_failed", series=series, exc_info=True)
                    return []

        all_results = await asyncio.gather(*(fetch_series(s) for s in series_list))

        events: list[Event] = []
        for batch in all_results:
            for event in batch:
                if event.event_ticker in active_tickers:
                    continue
                active_mkts = [m for m in event.markets if m.status == "active"]
                # Sports: exactly 2 markets. Non-sports: any number of active markets.
                is_sports = event.series_ticker in _SPORTS_SET
                if is_sports and len(active_mkts) != 2:
                    continue
                if not is_sports and len(active_mkts) == 0:
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
    def leg_labels(self) -> dict[str, tuple[str, str]]:
        """Event ticker -> (team_a, team_b) display labels."""
        return dict(self._leg_labels)

    @property
    def volumes_24h(self) -> dict[str, int]:
        """Market ticker -> 24h volume in contracts."""
        return dict(self._volumes_24h)
