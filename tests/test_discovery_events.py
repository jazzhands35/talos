from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from talos.discovery import DiscoveryService
from talos.models.tree import CategoryNode, SeriesNode

_EVENTS_SAMPLE = [
    {
        "event_ticker": "KXFEDMENTION-26APR",
        "series_ticker": "KXFEDMENTION",
        "title": "What will Powell say?",
        "sub_title": "On Apr 29, 2026",
        "category": "Mentions",
        "markets": [
            {
                "ticker": "KXFEDMENTION-26APR-YIEL",
                "title": "Will Powell say Yield Curve?",
                "status": "active",
                "volume_24h": 500,
                "open_interest_fp": "1200",
                "yes_bid_dollars": 0.20,
                "yes_ask_dollars": 0.25,
                "close_time": "2026-04-30T14:00:00Z",
            }
        ],
    },
]


def _preload_series(ds: DiscoveryService) -> None:
    s = SeriesNode(
        ticker="KXFEDMENTION",
        title="What will Powell say?",
        category="Mentions",
        tags=[],
        frequency="one_off",
    )
    ds.categories["Mentions"] = CategoryNode(
        name="Mentions",
        series_count=1,
        series={"KXFEDMENTION": s},
    )


@pytest.mark.asyncio
async def test_fetch_events_populates_series_and_markets():
    ds = DiscoveryService()
    _preload_series(ds)

    with patch.object(ds, "_fetch_events_for_series", new=AsyncMock(return_value=_EVENTS_SAMPLE)):
        events = await ds.get_events_for_series("KXFEDMENTION")

    assert "KXFEDMENTION-26APR" in events
    ev = events["KXFEDMENTION-26APR"]
    assert ev.title == "What will Powell say?"
    assert ev.sub_title == "On Apr 29, 2026"
    assert len(ev.markets) == 1
    assert ev.markets[0].volume_24h == 500


@pytest.mark.asyncio
async def test_fetch_events_caches_within_ttl():
    ds = DiscoveryService()
    _preload_series(ds)
    fetch_mock = AsyncMock(return_value=_EVENTS_SAMPLE)

    with patch.object(ds, "_fetch_events_for_series", new=fetch_mock):
        await ds.get_events_for_series("KXFEDMENTION")
        await ds.get_events_for_series("KXFEDMENTION")  # should hit cache

    assert fetch_mock.await_count == 1


@pytest.mark.asyncio
async def test_fetch_events_refetches_after_ttl_expires():
    ds = DiscoveryService()
    _preload_series(ds)
    fetch_mock = AsyncMock(return_value=_EVENTS_SAMPLE)

    with patch.object(ds, "_fetch_events_for_series", new=fetch_mock):
        await ds.get_events_for_series("KXFEDMENTION")

        # Manually age the cache past TTL
        s = ds.categories["Mentions"].series["KXFEDMENTION"]
        s.events_loaded_at = datetime.now(UTC) - timedelta(minutes=6)

        await ds.get_events_for_series("KXFEDMENTION")

    assert fetch_mock.await_count == 2


@pytest.mark.asyncio
async def test_fetch_events_unknown_series_returns_empty():
    ds = DiscoveryService()
    events = await ds.get_events_for_series("KXNONEXISTENT")
    assert events == {}
