"""Tests the wait_for_ready_for_trading startup gate: ready fires when milestones signal, hard cap
fires after timeout, flag-off mode returns immediately.
"""

import asyncio
from typing import Any, cast
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_ready_fires_when_milestones_signal():
    from talos.engine import TradingEngine

    e = cast(Any, TradingEngine.__new__(TradingEngine))
    e._ready_for_trading = asyncio.Event()
    e._auto_config = MagicMock(
        tree_mode=True,
        startup_milestone_wait_seconds=5.0,
    )

    async def _delayed_signal():
        await asyncio.sleep(0.02)
        e._ready_for_trading.set()

    asyncio.create_task(_delayed_signal())
    await e.wait_for_ready_for_trading()
    assert e._ready_for_trading.is_set()


@pytest.mark.asyncio
async def test_ready_fires_after_hard_cap_even_without_signal():
    from talos.engine import TradingEngine

    e = cast(Any, TradingEngine.__new__(TradingEngine))
    e._ready_for_trading = asyncio.Event()
    e._auto_config = MagicMock(
        tree_mode=True,
        startup_milestone_wait_seconds=0.05,
    )

    start = asyncio.get_event_loop().time()
    await e.wait_for_ready_for_trading()
    elapsed = asyncio.get_event_loop().time() - start
    assert e._ready_for_trading.is_set()
    assert elapsed >= 0.05


@pytest.mark.asyncio
async def test_flag_off_wait_returns_immediately():
    from talos.engine import TradingEngine

    e = cast(Any, TradingEngine.__new__(TradingEngine))
    e._ready_for_trading = asyncio.Event()
    e._auto_config = MagicMock(tree_mode=False)
    # should not need to set the event
    await asyncio.wait_for(e.wait_for_ready_for_trading(), timeout=0.5)
