from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from talos.milestones import MilestoneResolver


@pytest.fixture
def sample_milestone_response() -> dict:
    return {
        "milestones": [
            {
                "id": "c8bb4f46-eb47-4f84-9723-ad9b1961d2b5",
                "category": "mentions",
                "type": "one_off_milestone",
                # Far-future dates so stale-milestone filter (end_date < now)
                # doesn't skip these fixtures as the calendar advances.
                "start_date": "2099-04-16T23:00:00Z",
                "end_date": "2099-04-17T01:00:00Z",
                "title": "Trump holds a roundtable on No Tax on Tips",
                "notification_message": "What will Trump say?",
                "related_event_tickers": ["KXTRUMPMENTION-26APR16"],
                "primary_event_tickers": ["KXTRUMPMENTION-26APR16"],
                "last_updated_ts": "2026-04-16T14:40:36.610301Z",
                "details": {},
                "product_details": {},
                "source_ids": {},
            },
        ],
        "cursor": "",
    }


@pytest.mark.asyncio
async def test_empty_resolver_returns_none():
    r = MilestoneResolver()
    assert r.event_start("KX-ANY") is None


@pytest.mark.asyncio
async def test_refresh_builds_index(sample_milestone_response: dict):
    r = MilestoneResolver()
    with patch.object(
        r,
        "_paginated_fetch",
        new=AsyncMock(return_value=sample_milestone_response["milestones"]),
    ):
        await r.refresh()
    start = r.event_start("KXTRUMPMENTION-26APR16")
    assert start == datetime(2099, 4, 16, 23, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_refresh_replaces_index_atomically(sample_milestone_response: dict):
    r = MilestoneResolver()
    with patch.object(
        r,
        "_paginated_fetch",
        new=AsyncMock(return_value=sample_milestone_response["milestones"]),
    ):
        await r.refresh()
    # Simulate a subsequent refresh with an empty list
    with patch.object(r, "_paginated_fetch", new=AsyncMock(return_value=[])):
        await r.refresh()
    assert r.event_start("KXTRUMPMENTION-26APR16") is None


@pytest.mark.asyncio
async def test_refresh_failure_keeps_old_index(sample_milestone_response: dict):
    r = MilestoneResolver()
    with patch.object(
        r,
        "_paginated_fetch",
        new=AsyncMock(return_value=sample_milestone_response["milestones"]),
    ):
        await r.refresh()
    with patch.object(
        r,
        "_paginated_fetch",
        new=AsyncMock(side_effect=httpx.HTTPError("boom")),
    ):
        await r.refresh()  # must not raise
    # Old data still available
    assert r.event_start("KXTRUMPMENTION-26APR16") is not None


@pytest.mark.asyncio
async def test_multiple_events_in_one_milestone(sample_milestone_response: dict):
    ms = dict(sample_milestone_response["milestones"][0])
    ms["related_event_tickers"] = ["KXA-1", "KXA-2"]
    r = MilestoneResolver()
    with patch.object(r, "_paginated_fetch", new=AsyncMock(return_value=[ms])):
        await r.refresh()
    assert r.event_start("KXA-1") is not None
    assert r.event_start("KXA-2") is not None


def test_is_healthy_uses_configured_refresh_interval():
    """Codex round 3: the staleness window must scale with the configured
    milestone_refresh_seconds, not a hardcoded threshold. With a 30-min
    interval, a 10-min-old refresh should still be considered healthy
    (3x slack); with the prior hardcoded 15-min threshold it would not."""
    from datetime import timedelta

    r = MilestoneResolver(refresh_interval_seconds=1800.0)  # 30 min
    r._last_refresh = datetime.now(UTC) - timedelta(minutes=10)
    # Index needs to be non-empty for is_healthy
    r._by_event_ticker = {"any": object()}  # type: ignore[dict-item]
    assert r.is_healthy()


def test_is_healthy_false_past_slack_window():
    """At 3x the configured refresh interval, declare unhealthy."""
    from datetime import timedelta

    r = MilestoneResolver(refresh_interval_seconds=300.0)  # 5 min
    r._last_refresh = datetime.now(UTC) - timedelta(seconds=950)  # > 3 * 300
    r._by_event_ticker = {"any": object()}  # type: ignore[dict-item]
    assert not r.is_healthy()


def test_is_healthy_false_when_index_empty_even_if_recent():
    r = MilestoneResolver(refresh_interval_seconds=300.0)
    r._last_refresh = datetime.now(UTC)
    # _by_event_ticker stays empty
    assert not r.is_healthy()


@pytest.mark.asyncio
async def test_unknown_event_returns_none(sample_milestone_response: dict):
    r = MilestoneResolver()
    with patch.object(
        r,
        "_paginated_fetch",
        new=AsyncMock(return_value=sample_milestone_response["milestones"]),
    ):
        await r.refresh()
    assert r.event_start("KXOTHERMENTION-99") is None
