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
    outcome.pair_ticker = "K-1"
    outcome.kalshi_event_ticker = "K"
    e.remove_pairs_from_selection = AsyncMock(return_value=[outcome])

    emitted = []
    e._event_fully_removed_listeners = []

    def listener(kalshi_et: str):
        emitted.append(kalshi_et)

    e._event_fully_removed_listeners.append(listener)

    await e._reconcile_winding_down()

    assert "K-1" not in e._winding_down
    e.remove_pairs_from_selection.assert_awaited_once_with([("K-1", "K")])
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
    outcome.pair_ticker = "K-1"
    outcome.kalshi_event_ticker = "K"
    e.remove_pairs_from_selection = AsyncMock(return_value=[outcome])

    emitted = []
    e._event_fully_removed_listeners = [lambda k: emitted.append(k)]

    await e._reconcile_winding_down()

    assert emitted == []  # K-2 still present, so K not fully removed


@pytest.mark.asyncio
async def test_winding_down_failed_remove_stays_in_winding_down():
    """Round-3 review fix #2: when _reconcile_winding_down() calls
    remove_pairs_from_selection() and a pair comes back with
    status='failed' (e.g. unsubscribe raised), it must stay in
    _winding_down so the next reconciliation cycle retries the removal.
    Previously the unconditional `for pt in to_remove: discard(pt)`
    dropped failed pairs, leaving them in GameManager forever and any
    deferred-untick stuck pending until restart."""
    from talos.engine import TradingEngine

    e = cast(Any, TradingEngine.__new__(TradingEngine))

    # Ledger says clear → eligible for clean-remove attempt.
    ledger = MagicMock()
    ledger.has_filled_positions.return_value = False
    ledger.has_resting_orders.return_value = False
    e._adjuster = MagicMock()
    e._adjuster.get_ledger.return_value = ledger

    p = MagicMock()
    p.event_ticker = "K-1"
    p.kalshi_event_ticker = "K"
    gm = MagicMock()
    # Pair stays in _games because the remove failed.
    gm._games = {"K-1": p}
    gm.get_game.return_value = p
    e._game_manager = gm

    e._winding_down = {"K-1"}
    e._exit_only_events = {"K-1"}
    e._stale_candidates = set()
    e._event_fully_removed_listeners = []

    # remove_pairs_from_selection returns 'failed' for K-1.
    outcome = MagicMock()
    outcome.status = "failed"
    outcome.pair_ticker = "K-1"
    outcome.kalshi_event_ticker = "K"
    outcome.reason = "unsubscribe boom"
    e.remove_pairs_from_selection = AsyncMock(return_value=[outcome])

    await e._reconcile_winding_down()

    # CRITICAL: K-1 must STILL be in _winding_down so the next cycle retries.
    assert "K-1" in e._winding_down
    e.remove_pairs_from_selection.assert_awaited_once_with([("K-1", "K")])


@pytest.mark.asyncio
async def test_winding_down_partial_failure_does_not_emit_event_fully_removed():
    """Round-3 review fix #2 (paired): when event K has [P1=removed,
    P2=failed], event_fully_removed(K) must NOT fire because P2 is
    still alive in _games and _winding_down. Previously the
    just_removed_pts set included BOTH terminal and failed pairs, so
    the still-present check excluded P2 and erroneously emitted, which
    would promote a deferred untick prematurely."""
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
    # P1 was removed (popped) but P2 stays because remove failed.
    gm._games = {"K-2": p2}

    def _get_game(pt):
        return gm._games.get(pt) if pt != "K-1" else p1

    gm.get_game = MagicMock(side_effect=_get_game)
    e._game_manager = gm

    e._winding_down = {"K-1", "K-2"}
    e._exit_only_events = set()
    e._stale_candidates = set()
    e._event_fully_removed_listeners = []
    emitted: list[str] = []
    e._event_fully_removed_listeners.append(lambda k: emitted.append(k))

    o1 = MagicMock()
    o1.status = "removed"
    o1.pair_ticker = "K-1"
    o1.kalshi_event_ticker = "K"
    o2 = MagicMock()
    o2.status = "failed"
    o2.pair_ticker = "K-2"
    o2.kalshi_event_ticker = "K"
    o2.reason = "boom"
    e.remove_pairs_from_selection = AsyncMock(return_value=[o1, o2])

    await e._reconcile_winding_down()

    # K-2 still in _winding_down (failed); K-1 cleared (terminal).
    assert "K-1" not in e._winding_down
    assert "K-2" in e._winding_down
    # CRITICAL: event_fully_removed must NOT fire — K-2 is still alive.
    assert emitted == []
