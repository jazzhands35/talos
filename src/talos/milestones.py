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

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        *,
        refresh_interval_seconds: float = 300.0,
    ) -> None:
        self._http = http
        self._owns_http = http is None
        # Health staleness threshold is derived from the configured refresh
        # cadence so the two never drift. If the operator raises the
        # interval to 30 min, the staleness window expands accordingly —
        # the previous hardcoded 15-min window guaranteed a false-unhealthy
        # state for any refresh interval > 15 min, which would force-flip
        # every unscheduled tree-mode pair to exit-only on every cycle.
        self._refresh_interval_seconds = refresh_interval_seconds
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

    # Resolver is "healthy" iff the most recent refresh succeeded with a
    # non-empty index AND that refresh isn't ancient. The empty-but-recent
    # case is a real failure mode: Kalshi sometimes returns 200 OK with
    # zero milestones during partial outages, which used to silently mark
    # last_refresh and let the engine treat the resolver as trustworthy.
    # Engine cascade gates exit-only safety on this — if it returns False,
    # non-sports tree-mode pairs without a manual override should be
    # forced into exit-only.
    #
    # Slack factor — we tolerate up to 3x the configured refresh interval
    # before declaring the data stale. Picks up "missed one refresh" as
    # transient (still healthy) but flags "missed two consecutive" as a
    # real outage. Tunable if needed; not exposed as config because it's
    # an implementation detail of the health check, not a knob operators
    # should turn.
    _STALE_SLACK_FACTOR = 3

    def is_healthy(self) -> bool:
        from datetime import timedelta

        if self._last_refresh is None or self.count == 0:
            return False
        max_age = timedelta(seconds=self._refresh_interval_seconds * self._STALE_SLACK_FACTOR)
        return datetime.now(UTC) - self._last_refresh <= max_age

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

        # Drop milestones whose end_date is already in the past — they have
        # no exit-only signal value left. The fetch lookback brings them in,
        # but they shouldn't pollute the live index.
        now = datetime.now(UTC)
        new_index: dict[str, Milestone] = {}
        parse_failures = 0
        dropped_stale = 0
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
            if ms.end_date < now:
                dropped_stale += 1
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
            dropped_stale=dropped_stale,
            first_failure_id=first_failure_id,
            first_failure_exc=first_failure_exc,
        )

    # ── Internals ────────────────────────────────────────────────────

    # Lookback for milestone fetch. With minimum_start_date=now, any event
    # whose milestone started before "now" is excluded from the index — so a
    # restart 5 minutes after a Trump speech began would lose the milestone
    # and let the engine think the event has no schedule. 30 days is a
    # generous superset of every short-lived event class we care about
    # (speeches, sports, scheduled releases) without bloating the payload.
    _FETCH_LOOKBACK_DAYS = 30

    async def _paginated_fetch(self) -> list[dict[str, Any]]:
        """Paginate /milestones with a backward lookback window so events
        already in progress remain in the index after a restart."""
        from datetime import timedelta

        lookback = datetime.now(UTC) - timedelta(days=self._FETCH_LOOKBACK_DAYS)
        since_iso = lookback.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        http = self._http or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        try:
            out: list[dict[str, Any]] = []
            cursor: str | None = None
            for _ in range(40):  # safety cap — 40 * 200 = 8000 milestones
                params: dict[str, str] = {
                    "limit": "200",
                    "minimum_start_date": since_iso,
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
