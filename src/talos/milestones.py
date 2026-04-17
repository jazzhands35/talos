"""MilestoneResolver — Kalshi /milestones index.

Pulls upcoming milestones via paginated /milestones calls and maintains an
in-memory index keyed by event_ticker. Refresh is atomic-swap so readers
(Engine._check_exit_only) never see partial state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from talos.models.tree import Milestone

logger = structlog.get_logger()

_KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class MilestoneResolver:
    """In-memory milestone index with scheduled refresh."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http
        self._owns_http = http is None
        self._by_event_ticker: dict[str, Milestone] = {}
        self._last_refresh: datetime | None = None

    # ── Public API ───────────────────────────────────────────────────

    def event_start(self, event_ticker: str) -> datetime | None:
        """O(1) lookup of the curated event-start for this event, if any."""
        ms = self._by_event_ticker.get(event_ticker)
        return ms.start_date if ms else None

    def get_milestone(self, event_ticker: str) -> Milestone | None:
        return self._by_event_ticker.get(event_ticker)

    @property
    def last_refresh(self) -> datetime | None:
        return self._last_refresh

    @property
    def count(self) -> int:
        return len(self._by_event_ticker)

    # ── Refresh ──────────────────────────────────────────────────────

    async def refresh(self) -> None:
        """Pull upcoming milestones from /milestones; atomic-swap the index.

        On failure: keep the existing index. Log a warning. Never raise.
        """
        try:
            items = await self._paginated_fetch()
        except Exception:
            logger.warning("milestone_refresh_failed", exc_info=True)
            return

        new_index: dict[str, Milestone] = {}
        parse_failures = 0
        first_failure_id: str | None = None
        first_failure_exc: str | None = None
        for raw in items:
            try:
                ms = self._parse_milestone(raw)
            except Exception as exc:
                # Expected for records with malformed/missing optional fields
                # (e.g. sports milestones without end_date). Count + summarize
                # once at the end — logging each traceback rich-rendered to the
                # log file was blocking the event loop for tens of seconds
                # when Kalshi returns hundreds of such records.
                parse_failures += 1
                if first_failure_id is None:
                    first_failure_id = str(raw.get("id", "?"))
                    first_failure_exc = f"{type(exc).__name__}: {exc}"
                continue
            for et in ms.related_event_tickers:
                new_index[et] = ms

        self._by_event_ticker = new_index  # atomic swap
        self._last_refresh = datetime.now(UTC)
        logger.info(
            "milestone_refresh_ok",
            milestone_count=len(items),
            event_index_size=len(new_index),
            parse_failures=parse_failures,
            first_failure_id=first_failure_id,
            first_failure_exc=first_failure_exc,
        )

    # ── Internals ────────────────────────────────────────────────────

    async def _paginated_fetch(self) -> list[dict[str, Any]]:
        """Paginate /milestones?minimum_start_date=<now>&limit=200."""
        now_iso = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        http = self._http or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        try:
            out: list[dict[str, Any]] = []
            cursor: str | None = None
            for _ in range(40):  # safety cap — 40 * 200 = 8000 milestones
                params: dict[str, str] = {
                    "limit": "200",
                    "minimum_start_date": now_iso,
                }
                if cursor:
                    params["cursor"] = cursor
                resp = await http.get(f"{_KALSHI_API_BASE}/milestones", params=params)
                resp.raise_for_status()
                data = resp.json()
                out.extend(data.get("milestones", []))
                cursor = data.get("cursor")
                if not cursor:
                    break
            return out
        finally:
            if self._owns_http:
                await http.aclose()

    def _parse_milestone(self, raw: dict[str, Any]) -> Milestone:
        start_raw = raw["start_date"]
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        # end_date is optional — sports milestones routinely omit it.
        # Default to start_date so downstream consumers always have a
        # usable value and we don't spuriously fail ~900 records/refresh.
        end_raw = raw.get("end_date") or start_raw
        end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        return Milestone(
            id=raw["id"],
            category=raw.get("category", ""),
            type=raw.get("type", ""),
            start_date=start,
            end_date=end,
            title=raw.get("title", ""),
            notification_message=raw.get("notification_message", ""),
            related_event_tickers=raw.get("related_event_tickers", []),
        )
