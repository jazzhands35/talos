"""DiscoveryService — Kalshi discovery cache.

Two-level cache:
- Categories + series list: eagerly loaded at bootstrap, manually refreshed.
- Events per series: lazily fetched on tree-expand, TTL 5 min.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from talos.models.tree import CategoryNode, SeriesNode

logger = structlog.get_logger()

_KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class DiscoveryService:
    """Discovery cache for categories, series, and events.

    Holds its own semaphore (default 5 slots) so discovery calls can't
    starve trading calls on the shared REST client pool.
    """

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

    # ── Bootstrap ────────────────────────────────────────────────────

    async def bootstrap(self) -> None:
        """Pull full series catalog from /series and build the tree skeleton.

        On failure: log and leave the cache empty.
        """
        try:
            all_series = await self._fetch_all_series()
        except Exception:
            logger.warning("discovery_bootstrap_failed", exc_info=True)
            return

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

        # Set counts
        for cat in categories.values():
            cat.series_count = len(cat.series)

        self.categories = categories
        logger.info(
            "discovery_bootstrap_ok",
            category_count=len(categories),
            series_count=sum(c.series_count for c in categories.values()),
        )

    # ── Internals ────────────────────────────────────────────────────

    async def _fetch_all_series(self) -> list[dict[str, Any]]:
        async with self._sem:
            http = self._http or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
            try:
                resp = await http.get(f"{_KALSHI_API_BASE}/series")
                resp.raise_for_status()
                data = resp.json()
                return data.get("series", [])
            finally:
                if self._owns_http:
                    await http.aclose()
