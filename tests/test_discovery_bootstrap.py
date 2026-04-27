"""Tests the DiscoveryService bootstrap path: populating categories + series metadata +
event-counts retry-on-transient-failure.
"""

from unittest.mock import AsyncMock, patch

import pytest

from talos.discovery import DiscoveryService

_SERIES_SAMPLE = {
    "series": [
        {
            "ticker": "KXFEDMENTION",
            "title": "What will Powell say?",
            "category": "Mentions",
            "tags": ["Politicians"],
            "frequency": "one_off",
            "fee_type": "quadratic_with_maker_fees",
            "fee_multiplier": 1.0,
        },
        {
            "ticker": "KXNBAGAME",
            "title": "NBA game",
            "category": "Sports",
            "tags": ["Basketball"],
            "frequency": "daily",
            "fee_type": "quadratic_with_maker_fees",
            "fee_multiplier": 1.0,
        },
    ]
}


@pytest.mark.asyncio
async def test_bootstrap_populates_categories_and_series():
    ds = DiscoveryService()
    with patch.object(
        ds, "_fetch_all_series", new=AsyncMock(return_value=_SERIES_SAMPLE["series"])
    ):
        await ds.bootstrap()

    assert "Mentions" in ds.categories
    assert "Sports" in ds.categories
    assert ds.categories["Mentions"].series_count == 1
    assert "KXFEDMENTION" in ds.categories["Mentions"].series


@pytest.mark.asyncio
async def test_bootstrap_fills_series_metadata():
    ds = DiscoveryService()
    with patch.object(
        ds, "_fetch_all_series", new=AsyncMock(return_value=_SERIES_SAMPLE["series"])
    ):
        await ds.bootstrap()

    s = ds.categories["Mentions"].series["KXFEDMENTION"]
    assert s.title == "What will Powell say?"
    assert s.tags == ["Politicians"]
    assert s.frequency == "one_off"
    assert s.events is None  # not loaded yet — lazy


@pytest.mark.asyncio
async def test_bootstrap_failure_leaves_empty_cache():
    ds = DiscoveryService()
    with patch.object(ds, "_fetch_all_series", new=AsyncMock(side_effect=RuntimeError("kaboom"))):
        await ds.bootstrap()
    assert ds.categories == {}


@pytest.mark.asyncio
async def test_event_counts_retry_on_transient_failure_eventually_succeeds():
    """2026-04-19 hurricane bug: when Kalshi rate-limits the bulk count
    fetch, the previous code logged once and gave up forever — leaving
    every series displayed as '?' and every empty series visible. The
    fix retries with backoff. Test: first attempt fails, second succeeds;
    counts populate and listener fires."""
    ds = DiscoveryService()
    # Skip the actual sleep delays so the test runs fast.
    ds._COUNT_FETCH_RETRY_DELAYS = (0.0, 0.0)  # type: ignore[attr-defined]

    # Pre-populate categories so the populate loop has something to update.
    from talos.models.tree import CategoryNode, SeriesNode

    s = SeriesNode(
        ticker="KXHUR",
        title="Hurricanes",
        category="Climate",
        tags=[],
        frequency="one_off",
    )
    ds.categories["Climate"] = CategoryNode(
        name="Climate",
        series_count=1,
        series={"KXHUR": s},
    )

    # First attempt raises (simulating 429); second returns counts.
    fetch_results = [RuntimeError("rate limited"), {"KXHUR": 7}]

    async def _fake_fetch():
        result = fetch_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    fired: list[bool] = []
    ds.add_counts_populated_listener(lambda: fired.append(True))

    with patch.object(ds, "_fetch_event_counts_per_series", new=_fake_fetch):
        await ds._populate_event_counts_background()

    assert s.event_count == 7
    assert fired == [True]


@pytest.mark.asyncio
async def test_event_counts_retry_exhaustion_does_not_fire_listener():
    """If every retry attempt fails, counts stay None and listeners
    must NOT fire (caller's contract: listener fires on success only)."""
    ds = DiscoveryService()
    ds._COUNT_FETCH_RETRY_DELAYS = (0.0, 0.0)  # type: ignore[attr-defined]

    from talos.models.tree import CategoryNode, SeriesNode

    s = SeriesNode(ticker="KXA", title="A", category="C", tags=[], frequency="one_off")
    ds.categories["C"] = CategoryNode(name="C", series_count=1, series={"KXA": s})

    fired: list[bool] = []
    ds.add_counts_populated_listener(lambda: fired.append(True))

    with patch.object(
        ds,
        "_fetch_event_counts_per_series",
        new=AsyncMock(side_effect=RuntimeError("perma-down")),
    ):
        await ds._populate_event_counts_background()

    assert s.event_count is None  # never populated
    assert fired == []
