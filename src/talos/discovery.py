"""DiscoveryService — Kalshi discovery cache.

Two-level cache:
- Categories + series list: eagerly loaded at bootstrap, manually refreshed.
- Events per series: lazily fetched on tree-expand, TTL 5 min.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from talos.models.tree import (
    CategoryNode,
    EventNode,
    MarketNode,
    SeriesNode,
)

if TYPE_CHECKING:
    from talos.milestones import MilestoneResolver
    from talos.rest_client import KalshiRESTClient

logger = structlog.get_logger()

_KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Max retries for rate-limited (429) discovery calls. Kalshi's /events
# endpoint throttles around 10 req/s per IP; bulk count fetch can fire 40
# pages back-to-back, so we need to honor Retry-After and back off.
_MAX_429_RETRIES = 5
# Per-page sleep between bulk /events pages. The public /events bucket is
# shared with the main trading client at the IP level, so we have to crawl
# conservatively — 750ms keeps us under ~1.5 req/s for this endpoint. Over
# the full 40-page safety cap that's ~30s of added latency, but the fetch
# runs after app boot stabilizes and populates asynchronously, so the user
# never waits on it directly.
_BULK_PAGE_SLEEP_SEC = 0.75

# Hard wall-clock cap on the bulk event-count fetch. If Kalshi's /events
# bucket is deeply throttled (which happens when the trading client has
# been running for a while), retries can compound indefinitely. 45s is
# generous for the happy path (~30s spacing + one or two retries) and
# short enough that a stuck fetch doesn't keep a background task alive
# forever while the user is exploring.
_BULK_FETCH_TOTAL_TIMEOUT_SEC = 45.0


async def _get_with_429_retry(
    http: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """GET with exponential backoff on 429, honoring Retry-After header.

    Discovery calls bypass the main rate-limited Kalshi client by design
    (they use a dedicated semaphore so they can't starve trading), so this
    tiny retry wrapper is all we have for handling Kalshi throttling.
    """
    delay = 1.0
    for attempt in range(_MAX_429_RETRIES):
        resp = await http.get(url, params=params)
        if resp.status_code != 429:
            return resp
        retry_after_hdr = resp.headers.get("retry-after")
        try:
            wait = float(retry_after_hdr) if retry_after_hdr else delay
        except ValueError:
            wait = delay
        # Cap waits so one pathological header can't freeze us for an hour.
        wait = min(max(wait, 0.5), 10.0)
        logger.info(
            "discovery_429_backoff",
            url=url,
            attempt=attempt + 1,
            wait_seconds=wait,
        )
        await asyncio.sleep(wait)
        delay = min(delay * 2, 10.0)
    # Final attempt — whatever comes back, hand it up.
    return await http.get(url, params=params)


class DiscoveryService:
    """Discovery cache for categories, series, and events.

    Holds its own semaphore (default 5 slots) so discovery calls can't
    starve trading calls on the shared REST client pool.
    """

    EVENTS_TTL_SECONDS = 300  # 5 min

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        *,
        concurrent_limit: int = 5,
        rest_client: KalshiRESTClient | None = None,
    ) -> None:
        self._http = http
        self._owns_http = http is None
        self._sem = asyncio.Semaphore(concurrent_limit)
        # Authenticated REST client. When provided, discovery fetches go
        # through it so they share the trading client's auth headers (and
        # therefore the generous authenticated rate bucket) instead of
        # hitting Kalshi's public /events limits. Tests construct the
        # service without a REST client and fall back to raw httpx.
        self._rest = rest_client
        self.categories: dict[str, CategoryNode] = {}
        self._stopped = False
        # Listeners notified when the bulk event-count fetch successfully
        # populates `event_count` on every series. UI consumers (TreeScreen)
        # subscribe so they can rebuild after a late-arriving fetch — the
        # tree polling cap (~60s) used to expire silently when Kalshi rate-
        # limited the bulk fetch into a retry. With auto-retry + this
        # callback, a recovery 5 minutes later still re-renders the tree.
        self._counts_populated_listeners: list[
            Callable[[], None]
        ] = []

    # ── Bootstrap ────────────────────────────────────────────────────

    async def bootstrap(self) -> None:
        """Pull full series catalog from /series and build the tree skeleton.

        On failure: log and leave the cache empty.

        The ~9,700-series Pydantic build loop runs in a background thread via
        asyncio.to_thread so it does not block the Textual event loop.
        """
        try:
            all_series = await self._fetch_all_series()
        except Exception as exc:
            # Compact log — the full traceback dumps include ~11 MB of
            # `all_series` locals and drowns the logfile. Keep the type and
            # message; that's enough to diagnose 429s, timeouts, DNS, etc.
            logger.warning(
                "discovery_bootstrap_failed",
                exc_type=type(exc).__name__,
                exc_msg=str(exc),
            )
            return

        # Offload the CPU-bound Pydantic construction loop to a thread so
        # the event loop stays responsive during the several-second build.
        categories = await asyncio.to_thread(self._build_categories, all_series)
        self.categories = categories

        logger.info(
            "discovery_bootstrap_ok",
            category_count=len(categories),
            series_count=sum(c.series_count for c in categories.values()),
        )

        # Fire-and-forget the bulk event-count fetch. It shares an IP-level
        # rate bucket with the main trading client and frequently 429s for
        # minutes at a time; blocking bootstrap on it made tree-load take
        # 50-126s in field testing. Instead, bootstrap returns immediately
        # with categories populated and event_count=None on every series;
        # counts fill in asynchronously if the bulk fetch succeeds, and
        # lazily via drill-ins otherwise (see get_events_for_series).
        asyncio.create_task(self._populate_event_counts_background())

    # Backoff schedule (seconds) for retrying the bulk count fetch when
    # Kalshi rate-limits or times out. Spread far enough apart that the
    # rate bucket has time to refill, and capped at ~16 min total so a
    # genuinely-unhealthy day doesn't burn the whole session retrying.
    _COUNT_FETCH_RETRY_DELAYS = (30.0, 60.0, 120.0, 240.0, 480.0)

    async def _populate_event_counts_background(self) -> None:
        """Run the bulk /events count fetch with retry-on-failure.

        Cap protects us from cases where Kalshi's /events bucket is dry
        for an extended window and the paginated fetch makes no forward
        progress. Per-attempt failures (timeout, 429, network error)
        retry on the schedule above; only after exhausting all retries
        do we give up. Counts staying None means the tree shows '?' and
        every series is treated as visible — empty-series hiding only
        kicks in once counts populate.

        On final success, fire registered listeners so a late tree
        rebuild can happen without polling forever.
        """
        attempts = (0.0,) + self._COUNT_FETCH_RETRY_DELAYS
        for attempt, delay in enumerate(attempts):
            if delay > 0:
                logger.info(
                    "discovery_event_counts_retry_scheduled",
                    attempt=attempt,
                    delay_seconds=delay,
                )
                await asyncio.sleep(delay)
            if self._stopped:
                return
            try:
                counts = await asyncio.wait_for(
                    self._fetch_event_counts_per_series(),
                    timeout=_BULK_FETCH_TOTAL_TIMEOUT_SEC,
                )
            except TimeoutError:
                logger.warning(
                    "discovery_event_counts_timeout",
                    attempt=attempt,
                    timeout_seconds=_BULK_FETCH_TOTAL_TIMEOUT_SEC,
                    will_retry=attempt < len(self._COUNT_FETCH_RETRY_DELAYS),
                )
                continue
            except Exception as exc:
                logger.warning(
                    "discovery_event_counts_failed",
                    attempt=attempt,
                    exc_type=type(exc).__name__,
                    exc_msg=str(exc),
                    will_retry=attempt < len(self._COUNT_FETCH_RETRY_DELAYS),
                )
                continue

            for cat in self.categories.values():
                for series in cat.series.values():
                    series.event_count = counts.get(series.ticker, 0)
            logger.info(
                "discovery_event_counts_populated",
                attempt=attempt,
                series_with_events=sum(
                    1
                    for cat in self.categories.values()
                    for s in cat.series.values()
                    if (s.event_count or 0) > 0
                ),
            )
            # Notify listeners (e.g. TreeScreen) that counts arrived so
            # they can rebuild even if their own polling already stopped.
            for listener in list(self._counts_populated_listeners):
                try:
                    listener()
                except Exception:
                    logger.warning(
                        "counts_populated_listener_failed",
                        exc_info=True,
                    )
            return

        logger.warning(
            "discovery_event_counts_giving_up_after_retries",
            attempts=len(attempts),
        )

    def add_counts_populated_listener(
        self, listener: Callable[[], None]
    ) -> None:
        """Register a callback to fire after the bulk count fetch
        successfully populates event_count on every series. Safe to call
        before bootstrap. If the fetch already succeeded by the time the
        caller registers, the listener will NOT fire retroactively —
        callers should check `categories` first if they need a snapshot.
        """
        self._counts_populated_listeners.append(listener)

    # ── Internals ────────────────────────────────────────────────────

    @staticmethod
    def _build_categories(
        all_series: list[dict[str, Any]],
    ) -> dict[str, CategoryNode]:
        """Synchronous tree builder — run in a thread from bootstrap().

        Contains the only CPU-bound work in the discovery pipeline:
        ~9,700 Pydantic model instantiations (one SeriesNode per series).
        """
        categories: dict[str, CategoryNode] = {}
        for raw in all_series:
            cat_name = raw.get("category", "").strip() or "Uncategorized"
            sources = raw.get("settlement_sources") or []
            primary_source = (
                sources[0].get("name", "") if sources and isinstance(sources[0], dict) else ""
            )
            series = SeriesNode(
                ticker=raw.get("ticker", ""),
                title=raw.get("title", ""),
                category=cat_name,
                tags=raw.get("tags") or [],
                primary_source=primary_source,
                frequency=raw.get("frequency", "custom"),
                fee_type=raw.get("fee_type", "quadratic_with_maker_fees"),
                fee_multiplier=float(raw.get("fee_multiplier", 1.0)),
            )
            node = categories.setdefault(
                cat_name,
                CategoryNode(name=cat_name, series_count=0, series={}),
            )
            node.series[series.ticker] = series
        for cat in categories.values():
            cat.series_count = len(cat.series)
        return categories

    async def _fetch_event_counts_per_series(self) -> dict[str, int]:
        """Bulk fetch of open events, grouped by series_ticker.

        Prefers the authenticated KalshiRESTClient when available — its
        rate bucket is much more generous than the public /events bucket
        the raw httpx fallback hits. Payload is small (with_nested_markets
        stays off; we only need series_ticker to count).
        """
        now_ts = int(datetime.now(UTC).timestamp())
        if self._rest is not None:
            # Authenticated paginated sweep. Raw dicts so we don't drop
            # any fields through Pydantic validation.
            counts: dict[str, int] = {}
            cursor: str | None = None
            for _ in range(40):
                data = await self._rest.get_events_raw(
                    status="open",
                    with_nested_markets=False,
                    min_close_ts=now_ts,
                    limit=200,
                    cursor=cursor,
                )
                for ev in data.get("events", []):
                    st = ev.get("series_ticker")
                    if st:
                        counts[st] = counts.get(st, 0) + 1
                cursor = data.get("cursor")
                if not cursor:
                    break
            return counts

        # Unauthenticated fallback — used in tests and any callsite that
        # constructs DiscoveryService without a REST client. Hits the
        # public /events bucket, which 429s aggressively when the trading
        # client has been active. Kept for test-friendliness; production
        # always passes rest_client.
        async with self._sem:
            http = self._http or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
            close_http = self._owns_http
            try:
                counts_pub: dict[str, int] = {}
                cursor: str | None = None
                for _ in range(40):
                    params: dict[str, str] = {
                        "status": "open",
                        "with_nested_markets": "false",
                        "limit": "200",
                        "min_close_ts": str(now_ts),
                    }
                    if cursor:
                        params["cursor"] = cursor
                    resp = await _get_with_429_retry(
                        http,
                        f"{_KALSHI_API_BASE}/events",
                        params=params,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    for ev in data.get("events", []):
                        st = ev.get("series_ticker")
                        if st:
                            counts_pub[st] = counts_pub.get(st, 0) + 1
                    cursor = data.get("cursor")
                    if not cursor:
                        break
                    await asyncio.sleep(_BULK_PAGE_SLEEP_SEC)
                return counts_pub
            finally:
                if close_http:
                    await http.aclose()

    async def _fetch_all_series(self) -> list[dict[str, Any]]:
        async with self._sem:
            http = self._http or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
            try:
                resp = await _get_with_429_retry(http, f"{_KALSHI_API_BASE}/series")
                resp.raise_for_status()
                # JSON-parsing 11 MB synchronously would block the event loop
                # for 1-3 seconds. Offload to a thread.
                raw_text = resp.text
                data = await asyncio.to_thread(json.loads, raw_text)
                return data.get("series", [])
            finally:
                if self._owns_http:
                    await http.aclose()

    # ── Events (lazy, 5-min TTL) ─────────────────────────────────────

    async def get_events_for_series(self, series_ticker: str) -> dict[str, EventNode]:
        """Return events for a series, fetching lazily if not cached or stale.

        Returns {} for unknown series (not raised).
        """
        series = self._find_series(series_ticker)
        if series is None:
            return {}

        now = datetime.now(UTC)
        needs_fetch = (
            series.events is None
            or series.events_loaded_at is None
            or (now - series.events_loaded_at).total_seconds() > self.EVENTS_TTL_SECONDS
        )
        if not needs_fetch and series.events is not None:
            return series.events

        try:
            raw = await self._fetch_events_for_series(series_ticker)
        except Exception as exc:
            logger.warning(
                "discovery_events_fetch_failed",
                series=series_ticker,
                exc_type=type(exc).__name__,
                exc_msg=str(exc),
            )
            # Keep previous cache (if any), just don't update timestamp
            return series.events or {}

        events: dict[str, EventNode] = {}
        for raw_ev in raw:
            try:
                events[raw_ev["event_ticker"]] = self._parse_event(raw_ev)
            except Exception:
                logger.warning(
                    "discovery_event_parse_failed",
                    event_ticker=raw_ev.get("event_ticker"),
                    exc_info=True,
                )

        series.events = events
        series.events_loaded_at = now
        # Backfill count from the fetch so the tree label can update even
        # when the bulk bootstrap count fetch 429'd. Drill-in fetches per
        # series always succeed (or are a cached cache hit), so this is
        # the reliable path to known counts.
        series.event_count = len(events)
        return events

    def _find_series(self, series_ticker: str) -> SeriesNode | None:
        for cat in self.categories.values():
            if series_ticker in cat.series:
                return cat.series[series_ticker]
        return None

    async def _fetch_events_for_series(self, series_ticker: str) -> list[dict[str, Any]]:
        # Authenticated path — high rate bucket, proper 429 handling via
        # KalshiRESTClient. get_events_raw preserves all fields the
        # downstream _parse_event / _parse_market helpers rely on
        # (close_time, *_dollars price fields, etc.).
        if self._rest is not None:
            data = await self._rest.get_events_raw(
                status="open",
                series_ticker=series_ticker,
                with_nested_markets=True,
                limit=200,
            )
            return data.get("events", [])

        async with self._sem:
            http = self._http or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
            try:
                resp = await _get_with_429_retry(
                    http,
                    f"{_KALSHI_API_BASE}/events",
                    params={
                        "series_ticker": series_ticker,
                        "status": "open",
                        "with_nested_markets": "true",
                        "limit": "200",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("events", [])
            finally:
                if self._owns_http:
                    await http.aclose()

    def _parse_event(self, raw: dict[str, Any]) -> EventNode:
        markets = []
        for m in raw.get("markets", []):
            try:
                markets.append(self._parse_market(m))
            except Exception:
                logger.warning(
                    "market_parse_failed",
                    ticker=m.get("ticker"),
                    exc_info=True,
                )
        # Kalshi's event payload omits close_time / expected_expiration_time
        # for many event shapes (hurricane counts, commodity panels, etc.) —
        # the fields live on each market instead. Mirror up from the first
        # active market when the event-level value is missing, so downstream
        # consumers (SchedulePopup, safety gates) see a coherent timing for
        # the event as a whole.
        close_dt = _parse_iso(raw.get("close_time")) or _first_market_time(
            markets, "close_time"
        )
        exp_dt = _parse_iso(raw.get("expected_expiration_time")) or _first_market_time(
            markets, "expected_expiration_time"
        )
        return EventNode(
            ticker=raw["event_ticker"],
            series_ticker=raw.get("series_ticker", ""),
            title=raw.get("title", ""),
            sub_title=raw.get("sub_title", ""),
            close_time=close_dt,
            expected_expiration_time=exp_dt,
            markets=markets,
            fetched_at=datetime.now(UTC),
        )

    def _parse_market(self, raw: dict[str, Any]) -> MarketNode:
        close_dt = _parse_iso(raw.get("close_time"))
        exp_dt = _parse_iso(raw.get("expected_expiration_time"))
        # open_interest may arrive as string in some responses
        oi_raw = raw.get("open_interest_fp") or raw.get("open_interest") or 0
        try:
            oi = int(float(oi_raw))
        except (ValueError, TypeError):
            oi = 0
        # Post-March-12-2026 API cutover: integer fields were replaced by
        # fixed-point string fields. The Market Pydantic model's
        # _migrate_fp validator handles this for callers that go through
        # it, but discovery constructs MarketNode directly. Mirror the
        # open_interest_fp pattern above: prefer the _fp variant, fall
        # back to the legacy bare field for tests / cached old data.
        # Without this, every parsed market got volume_24h=0 — visible
        # in Talos as the "0 / 0" volume column for hurricane markets
        # despite real Kalshi volume.
        vol_raw = raw.get("volume_24h_fp") or raw.get("volume_24h") or 0
        try:
            vol_24h = int(float(vol_raw))
        except (ValueError, TypeError):
            vol_24h = 0
        return MarketNode(
            ticker=raw.get("ticker", ""),
            title=raw.get("title", ""),
            yes_bid=_to_cents(raw.get("yes_bid_dollars")),
            yes_ask=_to_cents(raw.get("yes_ask_dollars")),
            volume_24h=vol_24h,
            open_interest=oi,
            status=raw.get("status", "active"),
            close_time=close_dt,
            expected_expiration_time=exp_dt,
        )

    # ── Background milestone loop ────────────────────────────────────

    def stop(self) -> None:
        """Signal background loops to exit after current iteration."""
        self._stopped = True

    async def run_milestone_loop(
        self,
        resolver: MilestoneResolver,
        *,
        interval_seconds: float = 300.0,
    ) -> None:
        """Drive MilestoneResolver.refresh on a timer until stop() is called.

        Exceptions inside refresh are caught by the resolver itself (it logs
        and keeps old state); if something escapes, we still catch here so
        the loop never dies silently.
        """
        # Initial refresh ASAP
        await self._safe_refresh(resolver)
        while not self._stopped:
            await asyncio.sleep(interval_seconds)
            if self._stopped:
                break
            await self._safe_refresh(resolver)

    async def _safe_refresh(self, resolver: MilestoneResolver) -> None:
        try:
            async with self._sem:
                await resolver.refresh()
        except Exception as exc:
            # Compact log — rich-rendering the full traceback on every loop
            # iteration (which may be every 10ms in tests) blocks the loop
            # and drowns the logfile. Type+msg is enough to diagnose.
            logger.warning(
                "milestone_loop_iteration_failed",
                exc_type=type(exc).__name__,
                exc_msg=str(exc),
            )


def _to_cents(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(round(float(val) * 100))
    except (ValueError, TypeError):
        return None


def _parse_iso(raw: Any) -> datetime | None:
    """Parse a Kalshi ISO-8601 timestamp; return None on empty/invalid input.

    Kalshi uses a trailing 'Z' for UTC (`2026-12-02T04:59:00Z`), which
    datetime.fromisoformat accepts in 3.11+ but we normalize anyway to
    '+00:00' for portability.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _first_market_time(markets: list[Any], field: str) -> datetime | None:
    """Return the first active market's value for `field` (close_time or
    expected_expiration_time). Used to backfill event-level timing when the
    event payload omits it — true for many multi-market panels on Kalshi.
    """
    for m in markets:
        val = getattr(m, field, None)
        if val is not None:
            return val
    return None
