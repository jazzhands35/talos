from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_winding_down_pair_removed_when_flat():
    from talos.engine import TradingEngine

    e = cast(Any, TradingEngine.__new__(TradingEngine))

    ledger = MagicMock()
    ledger.has_filled_positions.return_value = False
    ledger.has_resting_orders.return_value = False
    e._adjuster = MagicMock()
    e._adjuster.get_ledger.return_value = ledger

    p = MagicMock()
    p.event_ticker = "K-1"
    p.kalshi_event_ticker = "K"
    gm = MagicMock()
    gm._games = {"K-1": p}
    gm.get_game.return_value = p

    # After remove: gm._games empty
    def _remove_side_effect(pt):
        gm._games.pop(pt, None)

    gm.remove_game = AsyncMock(side_effect=_remove_side_effect)
    e._game_manager = gm

    e._winding_down = {"K-1"}
    e._exit_only_events = {"K-1"}
    e._stale_candidates = set()

    outcome = MagicMock()
    outcome.status = "removed"
    outcome.kalshi_event_ticker = "K"
    e.remove_pairs_from_selection = AsyncMock(return_value=[outcome])

    emitted = []
    e._event_fully_removed_listeners = []

    def listener(kalshi_et: str):
        emitted.append(kalshi_et)

    e._event_fully_removed_listeners.append(listener)

    await e._reconcile_winding_down()

    assert "K-1" not in e._winding_down
    e.remove_pairs_from_selection.assert_awaited_once_with(["K-1"])
    assert emitted == ["K"]


@pytest.mark.asyncio
async def test_winding_down_pair_with_inventory_stays():
    from talos.engine import TradingEngine

    e = cast(Any, TradingEngine.__new__(TradingEngine))

    ledger = MagicMock()
    ledger.has_filled_positions.return_value = True
    ledger.has_resting_orders.return_value = False
    e._adjuster = MagicMock()
    e._adjuster.get_ledger.return_value = ledger

    p = MagicMock()
    p.event_ticker = "K-1"
    p.kalshi_event_ticker = "K"
    gm = MagicMock()
    gm._games = {"K-1": p}
    gm.get_game.return_value = p
    e._game_manager = gm

    e._winding_down = {"K-1"}
    e._event_fully_removed_listeners = []
    e.remove_pairs_from_selection = AsyncMock()

    await e._reconcile_winding_down()

    assert "K-1" in e._winding_down  # still waiting
    e.remove_pairs_from_selection.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_not_fully_removed_if_siblings_remain():
    """Event K has 2 pairs K-1 (winding) and K-2 (still active).
    When K-1's ledger clears, event_fully_removed must NOT fire (K-2 remains)."""
    from talos.engine import TradingEngine

    e = cast(Any, TradingEngine.__new__(TradingEngine))

    ledger = MagicMock()
    ledger.has_filled_positions.return_value = False
    ledger.has_resting_orders.return_value = False
    e._adjuster = MagicMock()
    e._adjuster.get_ledger.return_value = ledger

    p1 = MagicMock()
    p1.event_ticker = "K-1"
    p1.kalshi_event_ticker = "K"
    p2 = MagicMock()
    p2.event_ticker = "K-2"
    p2.kalshi_event_ticker = "K"
    gm = MagicMock()
    # After K-1 removed, K-2 still remains
    gm._games = {"K-1": p1, "K-2": p2}

    def _get_game(pt):
        return gm._games.get(pt)

    gm.get_game = MagicMock(side_effect=_get_game)

    def _remove_side_effect(pt):
        gm._games.pop(pt, None)

    gm.remove_game = AsyncMock(side_effect=_remove_side_effect)
    e._game_manager = gm

    e._winding_down = {"K-1"}
    e._exit_only_events = {"K-1"}
    e._stale_candidates = set()

    outcome = MagicMock()
    outcome.status = "removed"
    outcome.kalshi_event_ticker = "K"
    e.remove_pairs_from_selection = AsyncMock(return_value=[outcome])

    emitted = []
    e._event_fully_removed_listeners = [lambda k: emitted.append(k)]

    await e._reconcile_winding_down()

    assert emitted == []  # K-2 still present, so K not fully removed
