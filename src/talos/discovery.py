"""DiscoveryService — Kalshi discovery cache.

Two-level cache:
- Categories + series list: eagerly loaded at bootstrap, manually refreshed.
- Events per series: lazily fetched on tree-expand, TTL 5 min.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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

logger = structlog.get_logger()

_KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


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
    ) -> None:
        self._http = http
        self._owns_http = http is None
        self._sem = asyncio.Semaphore(concurrent_limit)
        self.categories: dict[str, CategoryNode] = {}
        self._stopped = False

    # ── Bootstrap ────────────────────────────────────────────────────

    async def bootstrap(self) -> None:
        """Pull full series catalog from /series and build the tree skeleton.

        On failure: log and leave the cache empty.

        The ~9,700-series Pydantic build loop runs in a background thread via
        asyncio.to_thread so it does not block the Textual event loop.
        """
        try:
            all_series = await self._fetch_all_series()
        except Exception:
            logger.warning("discovery_bootstrap_failed", exc_info=True)
            return

        # Offload the CPU-bound Pydantic construction loop to a thread so
        # the event loop stays responsive during the several-second build.
        categories = await asyncio.to_thread(self._build_categories, all_series)
        self.categories = categories

        # Populate per-series open-event counts. Uses one paginated /events
        # call with with_nested_markets=false so the payload is small (event
        # metadata only; ~300-500 bytes per event vs ~2KB with markets).
        try:
            counts = await self._fetch_event_counts_per_series()
            for cat in self.categories.values():
                for series in cat.series.values():
                    series.event_count = counts.get(series.ticker, 0)
        except Exception:
            logger.warning("discovery_event_counts_failed", exc_info=True)

        logger.info(
            "discovery_bootstrap_ok",
            category_count=len(categories),
            series_count=sum(c.series_count for c in categories.values()),
            series_with_events=sum(
                1
                for cat in categories.values()
                for s in cat.series.values()
                if (s.event_count or 0) > 0
            ),
        )

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
            series = SeriesNode(
                ticker=raw.get("ticker", ""),
                title=raw.get("title", ""),
                category=cat_name,
                tags=raw.get("tags") or [],
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
        """Paginated bulk fetch of open events, grouped by series_ticker.

        Uses with_nested_markets=false for a small payload — we only need the
        series_ticker field per event to count. One call instead of 9,700
        per-series calls.
        """
        async with self._sem:
            http = self._http or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
            close_http = self._owns_http
            try:
                counts: dict[str, int] = {}
                cursor: str | None = None
                now_ts = int(datetime.now(UTC).timestamp())
                # Safety cap: 40 pages × 200 = 8,000 events.
                for _ in range(40):
                    params: dict[str, str] = {
                        "status": "open",
                        "with_nested_markets": "false",
                        "limit": "200",
                        "min_close_ts": str(now_ts),
                    }
                    if cursor:
                        params["cursor"] = cursor
                    resp = await http.get(
                        f"{_KALSHI_API_BASE}/events",
                        params=params,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    for ev in data.get("events", []):
                        st = ev.get("series_ticker")
                        if st:
                            counts[st] = counts.get(st, 0) + 1
                    cursor = data.get("cursor")
                    if not cursor:
                        break
                return counts
            finally:
                if close_http:
                    await http.aclose()

    async def _fetch_all_series(self) -> list[dict[str, Any]]:
        async with self._sem:
            http = self._http or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
            try:
                resp = await http.get(f"{_KALSHI_API_BASE}/series")
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
        except Exception:
            logger.warning(
                "discovery_events_fetch_failed",
                series=series_ticker,
                exc_info=True,
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
        return events

    def _find_series(self, series_ticker: str) -> SeriesNode | None:
        for cat in self.categories.values():
            if series_ticker in cat.series:
                return cat.series[series_ticker]
        return None

    async def _fetch_events_for_series(self, series_ticker: str) -> list[dict[str, Any]]:
        async with self._sem:
            http = self._http or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
            try:
                resp = await http.get(
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
        close = raw.get("close_time")
        close_dt = None
        if close:
            with contextlib.suppress(ValueError):
                close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
        return EventNode(
            ticker=raw["event_ticker"],
            series_ticker=raw.get("series_ticker", ""),
            title=raw.get("title", ""),
            sub_title=raw.get("sub_title", ""),
            close_time=close_dt,
            markets=markets,
            fetched_at=datetime.now(UTC),
        )

    def _parse_market(self, raw: dict[str, Any]) -> MarketNode:
        close = raw.get("close_time")
        close_dt = None
        if close:
            with contextlib.suppress(ValueError):
                close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
        # open_interest may arrive as string in some responses
        oi_raw = raw.get("open_interest_fp") or raw.get("open_interest") or 0
        try:
            oi = int(float(oi_raw))
        except (ValueError, TypeError):
            oi = 0
        return MarketNode(
            ticker=raw.get("ticker", ""),
            title=raw.get("title", ""),
            yes_bid=_to_cents(raw.get("yes_bid_dollars")),
            yes_ask=_to_cents(raw.get("yes_ask_dollars")),
            volume_24h=int(raw.get("volume_24h") or 0),
            open_interest=oi,
            status=raw.get("status", "active"),
            close_time=close_dt,
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
        except Exception:
            logger.warning("milestone_loop_iteration_failed", exc_info=True)


def _to_cents(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(round(float(val) * 100))
    except (ValueError, TypeError):
        return None
