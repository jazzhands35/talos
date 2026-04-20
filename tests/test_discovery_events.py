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


# Post-March-12 API cutover: integer fields like volume_24h were removed
# and replaced by fixed-point string fields like volume_24h_fp. The Market
# Pydantic model handles this via its _migrate_fp validator (see
# src/talos/models/market.py lines 62-68), but discovery._parse_market
# constructs MarketNode directly from the raw dict and was reading bare
# `volume_24h`. Real API responses don't include that field anymore, so
# every parsed market got volume_24h=0 — visible in Talos as the "0 / 0"
# volume column for hurricane markets that clearly have volume on Kalshi.
#
# Same author handled the FP migration correctly for open_interest
# (open_interest_fp first, fallback to open_interest) but missed
# volume_24h. These tests pin the post-cutover behavior.

_EVENTS_SAMPLE_POST_FP_CUTOVER = [
    {
        "event_ticker": "KXHUR-26-T4",
        "series_ticker": "KXHUR",
        "title": "Number of hurricanes in 2026",
        "sub_title": "Above 4",
        "category": "Climate and Weather",
        "markets": [
            {
                "ticker": "KXHUR-26-T4-ABV4",
                "title": "Above 4",
                "status": "active",
                # Post-cutover: only volume_24h_fp is sent, not volume_24h.
                "volume_24h_fp": "479",
                "open_interest_fp": "524",
                "yes_bid_dollars": "0.58",
                "yes_ask_dollars": "0.60",
                "close_time": "2026-12-02T04:00:00Z",
            }
        ],
    },
]


@pytest.mark.asyncio
async def test_fetch_events_parses_volume_24h_fp_post_cutover():
    """Regression: post-March-12-2026 API only returns volume_24h_fp
    (fixed-point string), not volume_24h (integer). Discovery must read
    the _fp variant or every market displays 0 volume despite real
    Kalshi volume."""
    ds = DiscoveryService()
    s = SeriesNode(
        ticker="KXHUR",
        title="Hurricanes",
        category="Climate and Weather",
        tags=[],
        frequency="one_off",
    )
    ds.categories["Climate and Weather"] = CategoryNode(
        name="Climate and Weather",
        series_count=1,
        series={"KXHUR": s},
    )

    with patch.object(
        ds,
        "_fetch_events_for_series",
        new=AsyncMock(return_value=_EVENTS_SAMPLE_POST_FP_CUTOVER),
    ):
        events = await ds.get_events_for_series("KXHUR")

    ev = events["KXHUR-26-T4"]
    # 479 matches the Kalshi UI screenshot in the bug report.
    assert ev.markets[0].volume_24h == 479
    # Mirror open_interest assertion to lock the existing FP behavior.
    assert ev.markets[0].open_interest == 524
