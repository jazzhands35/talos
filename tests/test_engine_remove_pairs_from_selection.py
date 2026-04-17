from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.engine import TradingEngine


def _engine():
    e = cast(Any, TradingEngine.__new__(TradingEngine))
    gm = MagicMock()
    gm._games = {}

    def _get_game(pt):
        return gm._games.get(pt)

    gm.get_game = MagicMock(side_effect=_get_game)
    gm.remove_game = AsyncMock()

    @contextmanager
    def _suppress():
        yield

    gm.suppress_on_change = MagicMock(side_effect=_suppress)
    e._game_manager = gm

    e._adjuster = MagicMock()
    e._game_status_resolver = MagicMock()
    e._exit_only_events = set()
    e._stale_candidates = set()
    e._winding_down = set()
    e._persist_active_games = MagicMock()
    e.enforce_exit_only = AsyncMock()
    e._mark_engine_state = MagicMock()
    return e


@pytest.mark.asyncio
async def test_remove_clean_pair_returns_removed_outcome():
    e = _engine()
    p = MagicMock()
    p.kalshi_event_ticker = "K"
    e._game_manager._games["K-1"] = p
    e._adjuster.get_ledger.return_value = None

    outcomes = await e.remove_pairs_from_selection(["K-1"])
    assert len(outcomes) == 1
    assert outcomes[0].status == "removed"
    assert outcomes[0].kalshi_event_ticker == "K"
    e._game_manager.remove_game.assert_awaited_once_with("K-1")


@pytest.mark.asyncio
async def test_remove_pair_with_inventory_returns_winding_down():
    e = _engine()
    p = MagicMock()
    p.kalshi_event_ticker = "K"
    e._game_manager._games["K-1"] = p

    ledger = MagicMock()
    ledger.has_filled_positions.return_value = True
    ledger.has_resting_orders.return_value = False
    ledger.filled_count.side_effect = lambda side: 5
    ledger.resting_count.side_effect = lambda side: 0
    e._adjuster.get_ledger.return_value = ledger

    outcomes = await e.remove_pairs_from_selection(["K-1"])
    assert outcomes[0].status == "winding_down"
    assert "K-1" in e._winding_down
    e.enforce_exit_only.assert_awaited_once_with("K-1")
    e._mark_engine_state.assert_called_once_with("K-1", "winding_down")


@pytest.mark.asyncio
async def test_remove_missing_pair_returns_not_found():
    e = _engine()
    outcomes = await e.remove_pairs_from_selection(["K-NONEXISTENT"])
    assert outcomes[0].status == "not_found"


@pytest.mark.asyncio
async def test_remove_batch_persists_once():
    e = _engine()
    for i in range(3):
        p = MagicMock()
        p.kalshi_event_ticker = "K"
        e._game_manager._games[f"K-{i}"] = p
    e._adjuster.get_ledger.return_value = None

    await e.remove_pairs_from_selection(["K-0", "K-1", "K-2"])
    e._persist_active_games.assert_called_once()
