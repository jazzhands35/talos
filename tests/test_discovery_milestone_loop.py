"""Tests the milestone-refresh loop: calls the resolver repeatedly and survives transient refresh
exceptions without dying.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from talos.discovery import DiscoveryService
from talos.milestones import MilestoneResolver


@pytest.mark.asyncio
async def test_milestone_loop_calls_resolver_repeatedly():
    ds = DiscoveryService()
    resolver = MilestoneResolver()
    resolver.refresh = AsyncMock()  # type: ignore[method-assign]

    task = asyncio.create_task(
        ds.run_milestone_loop(resolver, interval_seconds=0.01),
    )
    await asyncio.sleep(0.05)
    ds.stop()
    await task

    assert resolver.refresh.await_count >= 3


@pytest.mark.asyncio
async def test_milestone_loop_survives_refresh_exception():
    ds = DiscoveryService()
    resolver = MilestoneResolver()
    resolver.refresh = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    task = asyncio.create_task(
        ds.run_milestone_loop(resolver, interval_seconds=0.01),
    )
    # Longer sleep here than the happy-path test — structlog renders a full
    # rich traceback per iteration when exc_info=True, which adds real latency.
    await asyncio.sleep(0.5)
    ds.stop()
    await task

    # Exceptions should not terminate the loop
    assert resolver.refresh.await_count >= 3
