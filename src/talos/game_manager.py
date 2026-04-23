"""Game lifecycle manager — sets up monitoring from Kalshi URLs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import structlog

from talos.errors import KalshiAPIError
from talos.fees import coerce_persisted_fee_rate, effective_fee_rate
from talos.market_feed import MarketFeed
from talos.models.market import Event, Market
from talos.models.strategy import ArbPair
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner

logger = structlog.get_logger()


class MarketAdmissionError(Exception):
    """Raised when a market is rejected at admission because its shape
    violates the invariants the current trading path can safely handle.

    Phase 0 (historical, bps/fp100 migration gate): fractional_trading_enabled
    markets and sub-cent-tick markets. Both are now fully supported as of
    Task 12; see :func:`validate_market_for_admission` for the migration
    history and the post-migration contract. The exception type stays in
    place as the general-purpose admission rejection signal for any future
    shape invariants.
    """


ONE_CENT_BPS = 100  # 1 cent = 100 basis points


def validate_market_for_admission(market_a: Market, market_b: Market) -> None:
    """Raise ``MarketAdmissionError`` if either market has a shape Talos
    cannot currently handle safely. Enforced at EVERY ingress path (scanner,
    manual add, market-picker, tree commit, startup restore) — not just
    scanner. A scanner-only guard is insufficient because other paths
    bypass it.

    Phase 0 (historical): rejected ``fractional_trading_enabled`` markets
    and sub-cent-tick markets until the bps/fp100 migration landed.

    Post-migration (Task 12): those shape classes are now fully supported
    by the bps/fp100 trading path end-to-end (models dual-fields, bps-aware
    fees, bps/fp100 ledger + v2 persistence, conditional 2/4-decimal REST
    serialization, WS-wire bps threading into OrderBookLevel, and scanner
    exact-bps edge computation). The function signature, the
    ``MarketAdmissionError`` type, the 5 ingress-path integrations, and
    the F32+F37 startup-restore quarantine path all stay — they're the
    general-purpose admission gate for FUTURE shape invariants Talos may
    need to enforce. No checks remain today.

    See docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md
    for the full migration record.
    """
    # No Phase 0 checks remain. Future shape invariants go here.
    del market_a, market_b
    return None


@dataclass(slots=True)
class CommitResult:
    """Outcome of an ``add_pairs_from_selection`` call.

    ``admitted``: pairs that passed admission and were registered.
    ``rejected``: (original selection record, reason) pairs that failed
    admission. Callers (especially TreeScreen.commit) MUST handle rejected
    rows explicitly — leaving them staged and surfacing a partial-failure
    dialog rather than the ordinary success toast.
    """

    admitted: list[Any] = field(default_factory=list)
    rejected: list[tuple[dict[str, Any], MarketAdmissionError]] = field(default_factory=list)


class MarketPickerNeeded(Exception):
    """Raised when a non-sports event has multiple markets needing user selection."""

    def __init__(self, event: Event, markets: list[Market]) -> None:
        self.event = event
        self.markets = markets
        super().__init__(
            f"Event {event.event_ticker} has {len(markets)} active markets — "
            f"select via market picker"
        )


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

_SPORTS_SET = set(SPORTS_SERIES)

# Backward-compatible alias for external consumers
SCAN_SERIES = SPORTS_SERIES

DEFAULT_NONSPORTS_CATEGORIES: list[str] = [
    "Companies",
    "Politics",
    "Science and Technology",
    "Mentions",
    "Entertainment",
    "World",
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


def _market_closes_within(market: Market, max_days: int) -> bool:
    """Check if a single market closes within max_days from now."""
    if not market.close_time:
        return False
    try:
        cutoff = datetime.now(UTC) + timedelta(days=max_days)
        close_dt = datetime.fromisoformat(market.close_time.replace("Z", "+00:00"))
        return close_dt <= cutoff
    except (ValueError, TypeError):
        return False


def _has_market_closing_within(event: Event, max_days: int) -> bool:
    """Check if any active market on the event closes within max_days from now."""
    return any(
        _market_closes_within(m, max_days)
        for m in event.markets
        if m.status == "active"
    )


class GameManager:
    """Orchestrates game setup, teardown, and ties layers together.

    Async — owns REST calls and feed subscriptions.
    """

    # Class-level default so MagicMock(spec=GameManager) can see the
    # attribute. Each instance still rebinds its own callback.
    on_change: Callable[[], None] | None = None

    def __init__(
        self,
        rest: KalshiRESTClient,
        feed: MarketFeed,
        scanner: ArbitrageScanner,
        *,
        sports_enabled: bool = True,
        nonsports_categories: list[str] | None = None,
        nonsports_max_days: int = 7,
        ticker_blacklist: list[str] | None = None,
    ) -> None:
        self._rest = rest
        self._feed = feed
        self._scanner = scanner
        self._sports_enabled = sports_enabled
        self._nonsports_categories: set[str] = set(
            nonsports_categories if nonsports_categories is not None
            else DEFAULT_NONSPORTS_CATEGORIES
        )
        self._nonsports_max_days = nonsports_max_days
        self._ticker_blacklist: list[str] = list(ticker_blacklist or [])
        self._games: dict[str, ArbPair] = {}
        self._labels: dict[str, str] = {}
        self._subtitles: dict[str, str] = {}
        self._leg_labels: dict[str, tuple[str, str]] = {}
        self._volumes_24h: dict[str, int] = {}  # market_ticker -> 24h volume
        self.on_change: Callable[[], None] | None = None
        # Stack of saved on_change callbacks during nested suppression.
        # Stack-based (vs single-slot) so nested `with suppress_on_change()`
        # blocks correctly restore the OUTER callback on exit. Used by
        # engine._persist_active_games(force_during_suppress=True) to
        # bypass the suppression for safety-critical winding_down persist.
        self._suppressed_on_change_stack: list[Callable[[], None] | None] = []

    @contextmanager
    def suppress_on_change(self):
        """Pause on_change emission within a batch.

        Engine batch paths (add_pairs_from_selection, remove_pairs_from_selection)
        call this to prevent per-pair save_games_full writes during restore/
        remove loops. A single final persist runs in Engine._persist_active_games
        at batch end.

        Non-batch callers (URL-add via add_games, clear_all_games, UI
        re-renders) are unaffected — they keep firing on_change per-pair.

        Stack-based to support nesting: each enter pushes the current
        callback (which may itself be None inside a nested suppress);
        exit pops and restores. The bypass accessor `suppressed_on_change`
        walks the stack for the nearest non-None entry.
        """
        self._suppressed_on_change_stack.append(self.on_change)
        self.on_change = None
        try:
            yield
        finally:
            self.on_change = self._suppressed_on_change_stack.pop()

    @property
    def suppressed_on_change(self) -> Callable[[], None] | None:
        """Return the nearest saved non-None on_change callback during
        suppression, walking outward through the stack. Used by the
        engine's force_during_suppress path to bypass suppression for
        safety-critical persists.

        Round-3 (v0.1.1) of the planning loop: in nested suppression the
        inner stack entries are None (because the outer suppression
        already cleared on_change to None before the inner suppress
        pushed). Returning the top would falsely report "no writer wired"
        even though an outer callback is preserved deeper in the stack.
        """
        for entry in reversed(self._suppressed_on_change_stack):
            if entry is not None:
                return entry
        return None

    def is_blacklisted(self, ticker: str) -> bool:
        """Check if a ticker matches any blacklist entry (prefix or exact)."""
        return any(ticker.startswith(b) for b in self._ticker_blacklist)

    def add_to_blacklist(self, entry: str) -> None:
        """Add an entry to the blacklist (prefix or specific ticker)."""
        if entry not in self._ticker_blacklist:
            self._ticker_blacklist.append(entry)

    async def remove_blacklisted_games(self) -> list[str]:
        """Remove any currently monitored games that match the blacklist.

        Returns list of removed event tickers.
        """
        to_remove = [
            et for et, pair in self._games.items()
            if self.is_blacklisted(et)
            or self.is_blacklisted(pair.ticker_a)
            or self.is_blacklisted(pair.series_ticker)
            or (pair.kalshi_event_ticker and self.is_blacklisted(pair.kalshi_event_ticker))
        ]
        for et in to_remove:
            await self.remove_game(et)
        return to_remove

    @property
    def ticker_blacklist(self) -> list[str]:
        """Current blacklist entries."""
        return list(self._ticker_blacklist)

    def get_game(self, event_ticker: str) -> ArbPair | None:
        """Look up a monitored game by event ticker."""
        return self._games.get(event_ticker)

    async def replace_blacklist(self, entries: list[str]) -> list[str]:
        """Replace the blacklist and remove any now-blocked games."""
        self._ticker_blacklist = list(entries)
        return await self.remove_blacklisted_games()

    async def add_game(self, url_or_ticker: str, *, subscribe: bool = True) -> ArbPair:
        """Set up monitoring for a game from a URL or event ticker."""
        ticker = parse_kalshi_url(url_or_ticker)

        if self.is_blacklisted(ticker):
            raise ValueError(f"Ticker blacklisted: {ticker}")

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
            # Multiple markets — caller shows market picker
            raise MarketPickerNeeded(event, active_markets)

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
            fee_rate = effective_fee_rate(series.fee_type)
            logger.info(
                "series_fee_info",
                series=event.series_ticker,
                fee_type=fee_type,
                fee_rate=fee_rate,
                raw_multiplier=series.fee_multiplier,
            )
        except Exception:
            logger.warning(
                "series_fee_fetch_failed",
                series=event.series_ticker,
                exc_info=True,
            )

        # Shape metadata from the active markets — scanner uses these to
        # enforce the Phase 0 admission guard. Take the "worst" shape across
        # the two legs (fractional on either side, smallest tick) so the
        # guard rejects the pair if either leg would trip it.
        mkt_a, mkt_b = active_markets[0], active_markets[1]
        # Phase 0 admission guard — reject fractional / sub-cent markets at
        # the URL-add ingress path before any downstream state is touched.
        # Raises MarketAdmissionError; engine.add_games surfaces it as a
        # specific operator-visible toast.
        validate_market_for_admission(mkt_a, mkt_b)
        pair_fractional = (
            mkt_a.fractional_trading_enabled or mkt_b.fractional_trading_enabled
        )
        pair_tick_bps = min(mkt_a.tick_bps(), mkt_b.tick_bps())
        pair = ArbPair(
            event_ticker=event.event_ticker,
            ticker_a=ticker_a,
            ticker_b=ticker_b,
            series_ticker=event.series_ticker,
            fee_type=fee_type,
            fee_rate=fee_rate,
            close_time=close_time,
            expected_expiration_time=expected_expiration_time,
            fractional_trading_enabled=pair_fractional,
            tick_bps=pair_tick_bps,
        )
        self._scanner.add_pair(
            event.event_ticker,
            ticker_a,
            ticker_b,
            fee_type=fee_type,
            fee_rate=fee_rate,
            close_time=close_time,
            expected_expiration_time=expected_expiration_time,
            fractional_trading_enabled=pair_fractional,
            tick_bps=pair_tick_bps,
        )
        if subscribe:
            await self._feed.subscribe(ticker_a)
            await self._feed.subscribe(ticker_b)
        self._games[event.event_ticker] = pair

        # Store raw sub_title for game status resolver
        self._subtitles[event.event_ticker] = event.sub_title

        # Store 24h volume per active market ticker
        for m in active_markets:
            self._volumes_24h[m.ticker] = (m.volume_24h_fp100 or 0) // 100

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

        # Phase 0 admission guard — reject fractional / sub-cent markets at
        # the market-picker ingress path before any downstream state is
        # touched. Same market is used on both sides for a YES/NO arb.
        # Raises MarketAdmissionError; engine.add_market_pairs catches it
        # per-market and surfaces a consolidated rejection notification.
        validate_market_for_admission(market, market)

        # Fetch series for fee metadata
        fee_type = "quadratic_with_maker_fees"
        fee_rate = 0.0175
        try:
            series = await self._rest.get_series(event.series_ticker)
            fee_type = series.fee_type
            fee_rate = effective_fee_rate(series.fee_type)
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
            series_ticker=event.series_ticker,
            fee_type=fee_type,
            fee_rate=fee_rate,
            close_time=market.close_time,
            expected_expiration_time=market.expected_expiration_time,
            fractional_trading_enabled=market.fractional_trading_enabled,
            tick_bps=market.tick_bps(),
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
            fractional_trading_enabled=market.fractional_trading_enabled,
            tick_bps=market.tick_bps(),
        )
        if subscribe:
            await self._feed.subscribe(market.ticker)
        self._games[market.ticker] = pair

        # Store metadata
        self._subtitles[market.ticker] = event.sub_title
        self._volumes_24h[market.ticker] = (market.volume_24h_fp100 or 0) // 100

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
        series_ticker = str(data.get("series_ticker", ""))
        talos_id = int(data.get("talos_id", 0))

        # Plumb source / engine_state through from the persisted record.
        # Without this, a crash/restart while a pair was winding_down
        # silently restored it as engine_state="active" (ArbPair default),
        # the engine resumed normal trading, and the documented restart-
        # safety invariant of this branch would be FALSE. _setup_initial_games
        # calls _apply_persisted_engine_state on each restored pair to
        # re-arm _winding_down / _exit_only_events from these fields.
        persisted_source = data.get("source")
        persisted_engine_state = str(data.get("engine_state", "active"))
        pair = ArbPair(
            talos_id=talos_id,
            event_ticker=event_ticker,
            ticker_a=ticker_a,
            ticker_b=ticker_b,
            side_a=side_a,
            side_b=side_b,
            kalshi_event_ticker=kalshi_event_ticker,
            series_ticker=series_ticker,
            fee_type=str(data.get("fee_type", "quadratic_with_maker_fees")),
            fee_rate=coerce_persisted_fee_rate(
                str(data.get("fee_type", "quadratic_with_maker_fees")),
                float(data.get("fee_rate", 0.0175)),
            ),
            close_time=str(data["close_time"]) if data.get("close_time") else None,
            expected_expiration_time=(
                str(data["expected_expiration_time"])
                if data.get("expected_expiration_time")
                else None
            ),
            source=str(persisted_source) if persisted_source is not None else None,
            engine_state=persisted_engine_state,
        )
        # Shape defaults kept — Market objects are not in scope during
        # restore (we only have the cached dict). The bigger restore-path
        # admission story is handled by Task 8 (quarantined startup restore).
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
            talos_id=talos_id,
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

        # Single URL: propagate MarketPickerNeeded to UI for market picker.
        # Multiple URLs (batch): swallow it (discovery can't show picker).
        propagate_picker = len(urls) == 1

        async def _add(url: str) -> list[ArbPair]:
            async with sem:
                try:
                    pair = await self.add_game(url, subscribe=False)
                    return [pair]
                except MarketPickerNeeded as e:
                    if propagate_picker:
                        raise
                    # Batch mode: auto-add each market as its own YES/NO pair
                    # Only add markets that individually close within the time window
                    added: list[ArbPair] = []
                    for market in e.markets:
                        if market.status != "active":
                            continue
                        if (market.volume_24h_fp100 or 0) == 0:
                            continue
                        if self._nonsports_max_days and not _market_closes_within(market, self._nonsports_max_days):
                            continue
                        try:
                            p = await self.add_market_as_pair(
                                e.event, market, subscribe=False,
                            )
                            added.append(p)
                        except MarketAdmissionError as adm_exc:
                            # Phase 0 admission: fractional / sub-cent markets
                            # reach this batch auto-picker path when the
                            # operator adds a multi-market non-sports URL.
                            # Log at WARNING so it's visible in logs even
                            # though we can't bubble a per-market error out
                            # of the batch shape.
                            logger.warning(
                                "auto_add_market_admission_rejected",
                                ticker=market.ticker,
                                reason=str(adm_exc),
                            )
                        except Exception:
                            logger.debug("auto_add_market_failed", ticker=market.ticker)
                    return added
                except Exception:
                    logger.warning("add_game_failed", url=url, exc_info=True)
                    return []

        results = await asyncio.gather(*(_add(url) for url in urls))
        pairs = [p for batch in results for p in batch]
        tickers = [t for p in pairs for t in (p.ticker_a, p.ticker_b)]
        if tickers:
            await self._feed.subscribe_bulk(tickers)
        return pairs

    async def remove_game(self, event_ticker: str) -> None:
        """Remove a game from monitoring.

        Order matters: unsubscribe FIRST (the only step that can fail
        with a non-trivial cause — network/WS error), then mutate the
        local dicts. If unsubscribe raises, _games still contains the
        pair so a retry can complete the removal cleanly. The previous
        order popped the pair before unsubscribing, which on failure
        left the WS subscription orphaned and made the second remove
        attempt return early (not_found) — silently desynchronizing
        engine state from live subscriptions.
        """
        pair = self._games.get(event_ticker)
        if pair is None:
            return
        await self._feed.unsubscribe(pair.ticker_a)
        if pair.ticker_b != pair.ticker_a:
            await self._feed.unsubscribe(pair.ticker_b)

        # Unsubscribes succeeded — local state mutation below is sync and
        # cannot fail in any meaningful way (dict.pop is total).
        self._games.pop(event_ticker, None)
        self._labels.pop(event_ticker, None)
        self._subtitles.pop(event_ticker, None)
        self._leg_labels.pop(event_ticker, None)
        self._volumes_24h.pop(pair.ticker_a, None)
        self._volumes_24h.pop(pair.ticker_b, None)
        self._scanner.remove_pair(event_ticker)
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
        """Re-fetch 24h volume for all monitored markets, batched by series.

        Prioritizes series with missing volume data so the UI fills in fast.

        Uses /markets (not /events?with_nested_markets=true) because the
        Kalshi /events response strips volume_24h on nested markets for
        non-sports series. Discovered via the hurricane-series bug
        (2026-04-19): pairs added via the tree had _volumes_24h seeded
        with 0 (discovery's nested-markets parse), and refresh_volumes
        via /events also returned None for volume, so the UI showed 0
        volume forever.
        """
        # Group active games by series ticker, track which have missing data
        series_missing: set[str] = set()
        series_loaded: set[str] = set()
        for pair in self.active_games:
            st = pair.series_ticker or pair.event_ticker.split("-")[0]
            has_a = pair.ticker_a in self._volumes_24h
            has_b = pair.ticker_b in self._volumes_24h
            if has_a and has_b:
                series_loaded.add(st)
            else:
                series_missing.add(st)

        # Fetch missing-data series first, then the rest
        ordered = list(series_missing) + [s for s in series_loaded if s not in series_missing]

        sem = asyncio.Semaphore(4)

        async def _fetch(series: str) -> list[Market]:
            async with sem:
                try:
                    return await self._rest.get_markets(
                        series_ticker=series,
                        status="open",
                        limit=200,
                    )
                except Exception:
                    return []

        results = await asyncio.gather(*(_fetch(s) for s in ordered))
        for batch in results:
            for m in batch:
                if m.volume_24h_fp100 is not None:
                    self._volumes_24h[m.ticker] = m.volume_24h_fp100 // 100

    async def scan_events(self, scan_mode: str = "sports") -> list[Event]:
        """Discover all open arb-eligible events not already monitored."""
        active_tickers = {p.event_ticker for p in self.active_games}
        active_kalshi_tickers = {
            p.kalshi_event_ticker for p in self.active_games
            if p.kalshi_event_ticker
        }
        all_active = active_tickers | active_kalshi_tickers

        sem = asyncio.Semaphore(4)

        # --- Sports path (unchanged) ---
        sports_events: list[Event] = []
        if self._sports_enabled and scan_mode in ("sports", "both"):
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

            all_results = await asyncio.gather(*(fetch_series(s) for s in SPORTS_SERIES))
            for batch in all_results:
                for event in batch:
                    if event.event_ticker in all_active:
                        continue
                    if self.is_blacklisted(event.event_ticker) or self.is_blacklisted(event.series_ticker):
                        continue
                    active_mkts = [m for m in event.markets if m.status == "active"]
                    if len(active_mkts) != 2:
                        continue
                    if all(((m.volume_24h_fp100 or 0) // 100) == 0 for m in active_mkts):
                        continue
                    sports_events.append(event)

        # --- Non-sports path (new) ---
        nonsports_events: list[Event] = []
        if self._nonsports_categories and scan_mode in ("nonsports", "both"):
            min_close_ts = int(datetime.now(UTC).timestamp())
            try:
                raw_events = await self._rest.get_all_events(
                    status="open",
                    with_nested_markets=True,
                    min_close_ts=min_close_ts,
                )
            except Exception:
                logger.warning("nonsports_scan_failed", exc_info=True)
                raw_events = []

            for event in raw_events:
                if event.event_ticker in all_active:
                    continue
                if self.is_blacklisted(event.event_ticker) or self.is_blacklisted(event.series_ticker):
                    continue
                if event.series_ticker in _SPORTS_SET:
                    continue
                if event.category not in self._nonsports_categories:
                    continue
                active_mkts = [m for m in event.markets if m.status == "active"]
                if len(active_mkts) == 0:
                    continue
                # Per-market volume filter happens at batch-add time, not here
                if not _has_market_closing_within(event, self._nonsports_max_days):
                    continue
                nonsports_events.append(event)

        return sports_events + nonsports_events

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
