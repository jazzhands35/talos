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
