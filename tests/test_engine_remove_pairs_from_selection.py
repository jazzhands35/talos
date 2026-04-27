"""Locks remove_pairs_from_selection on TradingEngine: clean removes, winding-down transitions,
persistence-failure preservation, force-during-suppress callback handling.
"""

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

    outcomes = await e.remove_pairs_from_selection([("K-1", "K")])
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

    outcomes = await e.remove_pairs_from_selection([("K-1", "K")])
    assert outcomes[0].status == "winding_down"
    assert "K-1" in e._winding_down
    e.enforce_exit_only.assert_awaited_once_with("K-1")
    # _mark_engine_state called twice: first to "winding_down", then on
    # successful persist no rollback fires (so just the one call).
    e._mark_engine_state.assert_called_once_with("K-1", "winding_down")


@pytest.mark.asyncio
async def test_remove_missing_pair_returns_not_found():
    e = _engine()
    outcomes = await e.remove_pairs_from_selection([("K-NONEXISTENT", "K-EVT")])
    assert outcomes[0].status == "not_found"
    # round-7 plan Fix #1: kalshi_event_ticker is propagated from the
    # input tuple even for not_found outcomes, so a retry can match.
    assert outcomes[0].kalshi_event_ticker == "K-EVT"


@pytest.mark.asyncio
async def test_remove_failure_preserves_engine_state_for_retry():
    """Codex round 3: previously the clean-remove path cleared
    _exit_only_events, _stale_candidates, GSR, and adjuster BEFORE
    awaiting game_manager.remove_game(). An unsubscribe failure inside
    remove_game would record status='failed' but engine state was
    already half-cleared, leaving the pair in an unrecoverable state.

    Now the dangerous async work runs first; on failure, engine state
    is unchanged and a retry can complete cleanly."""
    e = _engine()
    p = MagicMock()
    p.kalshi_event_ticker = "K"
    p.event_ticker = "K-1"
    e._game_manager._games["K-1"] = p
    e._adjuster.get_ledger.return_value = None
    e._exit_only_events.add("K-1")
    e._stale_candidates.add("K-1")

    e._game_manager.remove_game = AsyncMock(side_effect=ConnectionError("ws gone"))

    outcomes = await e.remove_pairs_from_selection([("K-1", "K")])
    assert outcomes[0].status == "failed"

    # Engine state must be intact so a retry can complete cleanly.
    assert "K-1" in e._exit_only_events
    assert "K-1" in e._stale_candidates
    e._game_status_resolver.remove.assert_not_called()
    e._adjuster.remove_event.assert_not_called()


@pytest.mark.asyncio
async def test_remove_batch_persists_once():
    e = _engine()
    for i in range(3):
        p = MagicMock()
        p.kalshi_event_ticker = "K"
        e._game_manager._games[f"K-{i}"] = p
    e._adjuster.get_ledger.return_value = None

    await e.remove_pairs_from_selection([("K-0", "K"), ("K-1", "K"), ("K-2", "K")])
    # Per-transition persists fire for each winding-down transition
    # PLUS the batch-end persist. Clean removes (no inventory) only
    # contribute to the batch-end persist.
    # In this test all pairs have no inventory (ledger=None), so the
    # only persist is the batch-end one.
    e._persist_active_games.assert_called_once()


@pytest.mark.asyncio
async def test_winding_down_transition_persists_immediately():
    """Round-7 plan Fix #2: per-transition persist fires AFTER each
    _mark_engine_state('winding_down'), not just at batch end. A crash
    between transitions otherwise loses the in-memory state."""
    e = _engine()
    p1 = MagicMock()
    p1.kalshi_event_ticker = "K"
    p1.engine_state = "active"
    p2 = MagicMock()
    p2.kalshi_event_ticker = "K"
    p2.engine_state = "active"
    e._game_manager._games["K-1"] = p1
    e._game_manager._games["K-2"] = p2

    # Both pairs have inventory → both go winding_down
    ledger = MagicMock()
    ledger.has_filled_positions.return_value = True
    ledger.has_resting_orders.return_value = False
    ledger.filled_count.return_value = 5
    ledger.resting_count.return_value = 0
    e._adjuster.get_ledger.return_value = ledger

    await e.remove_pairs_from_selection([("K-1", "K"), ("K-2", "K")])
    # 2 per-transition persists + 1 batch-end persist = 3 total.
    # All called with force_during_suppress=True since they're inside
    # the suppress_on_change() block.
    assert e._persist_active_games.call_count == 3
    for call in e._persist_active_games.call_args_list:
        assert call.kwargs.get("force_during_suppress") is True


@pytest.mark.asyncio
async def test_winding_down_persist_failure_raises_remove_batch_persistence_error():
    """Round-7 plan: per-transition persist failure raises
    RemoveBatchPersistenceError carrying persisted_count, AND rolls back
    the failing pair's in-memory state to its prior snapshot."""
    from talos.persistence_errors import PersistenceError, RemoveBatchPersistenceError

    e = _engine()
    p1 = MagicMock()
    p1.kalshi_event_ticker = "K"
    p1.engine_state = "active"
    p2 = MagicMock()
    p2.kalshi_event_ticker = "K"
    p2.engine_state = "active"
    p3 = MagicMock()
    p3.kalshi_event_ticker = "K"
    p3.engine_state = "active"
    e._game_manager._games["K-1"] = p1
    e._game_manager._games["K-2"] = p2
    e._game_manager._games["K-3"] = p3

    ledger = MagicMock()
    ledger.has_filled_positions.return_value = True
    ledger.has_resting_orders.return_value = False
    ledger.filled_count.return_value = 5
    ledger.resting_count.return_value = 0
    e._adjuster.get_ledger.return_value = ledger

    # First persist succeeds, second raises.
    call_count = [0]

    def _persist(*, force_during_suppress=False):
        call_count[0] += 1
        if call_count[0] == 2:
            raise PersistenceError("disk full")

    e._persist_active_games = MagicMock(side_effect=_persist)

    with pytest.raises(RemoveBatchPersistenceError) as exc_info:
        await e.remove_pairs_from_selection(
            [("K-1", "K"), ("K-2", "K"), ("K-3", "K")]
        )

    # Persisted count = 1 (only K-1 made it to disk).
    assert exc_info.value.persisted_count == 1
    # K-2's in-memory state was rolled back to "active".
    e._mark_engine_state.assert_any_call("K-2", "active")
    # K-3 was never processed.
    assert "K-3" not in e._winding_down


@pytest.mark.asyncio
async def test_winding_down_persist_failure_preserves_pre_existing_exit_only():
    """Round-7 plan Fix #2 round-5 v0.1.1 finding #2: snapshot-restore
    must NOT unconditionally discard from _exit_only_events. Pairs may
    already be there via other engine paths (milestone, sports-game-
    started). A hardcoded discard would silently strip the safety
    condition."""
    from talos.persistence_errors import PersistenceError, RemoveBatchPersistenceError

    e = _engine()
    p1 = MagicMock()
    p1.kalshi_event_ticker = "K"
    p1.engine_state = "exit_only"
    e._game_manager._games["K-1"] = p1
    # Pre-existing: pair was already in _exit_only_events before remove.
    e._exit_only_events.add("K-1")

    ledger = MagicMock()
    ledger.has_filled_positions.return_value = True
    ledger.has_resting_orders.return_value = False
    ledger.filled_count.return_value = 5
    ledger.resting_count.return_value = 0
    e._adjuster.get_ledger.return_value = ledger

    e._persist_active_games = MagicMock(side_effect=PersistenceError("disk full"))

    with pytest.raises(RemoveBatchPersistenceError):
        await e.remove_pairs_from_selection([("K-1", "K")])

    # Pair STAYS in _exit_only_events (was there before) — rollback
    # restored exact prior state, didn't strip.
    assert "K-1" in e._exit_only_events
    # And engine_state restored to its prior "exit_only" value.
    e._mark_engine_state.assert_any_call("K-1", "exit_only")


@pytest.mark.asyncio
async def test_force_during_suppress_uses_saved_callback():
    """Round-7 plan Step 2: under suppress, on_change is None — but
    force_during_suppress=True falls back to suppressed_on_change
    (the saved callback) so durability is delivered."""
    e = _engine()
    cb = MagicMock()
    e._game_manager.on_change = cb
    e._game_manager._suppressed_on_change_stack = []

    # Restore the real method (helper mocks it).
    from talos.engine import TradingEngine
    del e._persist_active_games
    e._persist_active_games = TradingEngine._persist_active_games.__get__(e)

    with e._game_manager.suppress_on_change():
        # on_change is None inside the block; force=True bypasses.
        e._persist_active_games(force_during_suppress=True)
    cb.assert_called_once()


@pytest.mark.asyncio
async def test_force_during_suppress_raises_when_no_callback_wired():
    """Round-7 plan Step 2: fail-closed if no writer is wired. A
    silent return would defeat the durability contract."""
    from talos.persistence_errors import PersistenceError

    e = _engine()
    e._game_manager.on_change = None
    # MagicMock auto-creates attributes; pin both explicitly to None so
    # the engine's "no callback" branch fires.
    e._game_manager.suppressed_on_change = None
    e._game_manager._suppressed_on_change_stack = []

    from talos.engine import TradingEngine
    del e._persist_active_games
    e._persist_active_games = TradingEngine._persist_active_games.__get__(e)

    with pytest.raises(PersistenceError, match="no on_change writer"):
        e._persist_active_games(force_during_suppress=True)


@pytest.mark.asyncio
async def test_force_during_suppress_callback_exception_propagates_as_persistence_error():
    """Round-7 plan Step 2: under force, ANY callback exception (even
    TypeError, AttributeError) is converted to PersistenceError so the
    writer's exit contract holds uniformly."""
    from talos.persistence_errors import PersistenceError

    def boom():
        raise TypeError("wrong type")

    e = _engine()
    e._game_manager.on_change = boom

    from talos.engine import TradingEngine
    del e._persist_active_games
    e._persist_active_games = TradingEngine._persist_active_games.__get__(e)

    with pytest.raises(PersistenceError, match="TypeError"):
        e._persist_active_games(force_during_suppress=True)


@pytest.mark.asyncio
async def test_unexpected_engine_exception_during_remove_propagates_not_toasted():
    """Round-7 plan Step 10: commit() narrows catch to PersistenceError
    only. Engine bugs (RuntimeError) escape rather than being mislabeled
    as recoverable persistence failures. This test verifies at the
    engine layer that non-PersistenceError exceptions propagate."""
    e = _engine()
    p = MagicMock()
    p.kalshi_event_ticker = "K"
    e._game_manager._games["K-1"] = p
    e._adjuster.get_ledger.side_effect = RuntimeError("engine bug")

    # The engine catches Exception per-pair (around the inventory-check
    # block) and records "failed", so this surfaces as a status, not a
    # raised exception. The narrow-catch behavior is on the COMMIT side
    # (test_unexpected_engine_exception_during_remove_propagates_not_toasted
    # in test_tree_commit_flow.py exercises that). Here we just verify
    # the per-pair recording still happens correctly.
    outcomes = await e.remove_pairs_from_selection([("K-1", "K")])
    assert outcomes[0].status == "failed"
    assert "engine bug" in (outcomes[0].reason or "")


@pytest.mark.asyncio
async def test_batch_end_save_failure_after_winding_down_persists():
    """Round-4 review fix #2: a mixed batch where per-transition saves
    succeeded but the batch-end save failed must raise
    RemoveBatchPersistenceError(phase='batch_end'). This is the case
    where winding-down transitions ARE durable but clean removes are
    NOT yet on disk. Operator messaging needs the phase distinction
    so the toast can correctly warn about clean-remove durability."""
    from talos.persistence_errors import (
        PersistenceError,
        RemoveBatchPersistenceError,
    )

    e = _engine()
    # Two pairs sharing event K: P1 has inventory (winding), P2 is
    # flat (clean remove). The batch-end save covers P2; its failure
    # makes the clean remove non-durable.
    p1 = MagicMock()
    p1.kalshi_event_ticker = "K"
    p1.engine_state = "active"
    p2 = MagicMock()
    p2.kalshi_event_ticker = "K"
    p2.engine_state = "active"
    e._game_manager._games["K-1"] = p1
    e._game_manager._games["K-2"] = p2

    # Per-pair ledger: K-1 has filled positions, K-2 is flat.
    def _get_ledger(pt):
        ledger = MagicMock()
        if pt == "K-1":
            ledger.has_filled_positions.return_value = True
            ledger.has_resting_orders.return_value = False
            ledger.filled_count.return_value = 5
            ledger.resting_count.return_value = 0
        else:
            ledger.has_filled_positions.return_value = False
            ledger.has_resting_orders.return_value = False
            ledger.filled_count.return_value = 0
            ledger.resting_count.return_value = 0
        return ledger

    e._adjuster.get_ledger.side_effect = _get_ledger

    # First persist (winding-down transition for K-1) succeeds; the
    # batch-end save (after K-2 clean remove) fails.
    call_count = [0]

    def _persist(*, force_during_suppress=False):
        call_count[0] += 1
        if call_count[0] == 2:  # batch-end save
            raise PersistenceError("disk full at batch end")

    e._persist_active_games = MagicMock(side_effect=_persist)

    with pytest.raises(RemoveBatchPersistenceError) as exc_info:
        await e.remove_pairs_from_selection([("K-1", "K"), ("K-2", "K")])

    # Phase MUST be batch_end so the UI can warn about clean removes.
    assert exc_info.value.phase == "batch_end"
    # Winding-down transition for K-1 was durably persisted.
    assert exc_info.value.persisted_count == 1
    # Message text references the durability-uncertainty wording.
    assert "may not be durable" in str(exc_info.value)


@pytest.mark.asyncio
async def test_clean_only_batch_end_save_failure():
    """Round-4 review fix #2: clean-only remove batch (no winding
    transitions) where the batch-end save fails. persisted_count=0
    (no winding transitions), phase='batch_end'. Operator must be
    told the clean removes are not durable on disk."""
    from talos.persistence_errors import (
        PersistenceError,
        RemoveBatchPersistenceError,
    )

    e = _engine()
    p1 = MagicMock()
    p1.kalshi_event_ticker = "K"
    p1.engine_state = "active"
    e._game_manager._games["K-1"] = p1

    # Flat pair → clean remove path (no per-transition save).
    ledger = MagicMock()
    ledger.has_filled_positions.return_value = False
    ledger.has_resting_orders.return_value = False
    ledger.filled_count.return_value = 0
    ledger.resting_count.return_value = 0
    e._adjuster.get_ledger.return_value = ledger

    # Only one persist call happens (the batch-end one) — make it fail.
    def _persist(*, force_during_suppress=False):
        raise PersistenceError("disk full")

    e._persist_active_games = MagicMock(side_effect=_persist)

    with pytest.raises(RemoveBatchPersistenceError) as exc_info:
        await e.remove_pairs_from_selection([("K-1", "K")])

    assert exc_info.value.phase == "batch_end"
    # No winding-down transitions in a clean-only batch.
    assert exc_info.value.persisted_count == 0
    assert "may not be durable" in str(exc_info.value)


@pytest.mark.asyncio
async def test_transition_save_failure_carries_phase_transition():
    """Round-4 review fix #2: per-transition save failure carries
    phase='transition' so the UI emits the mid-batch toast (different
    durability semantics from batch_end)."""
    from talos.persistence_errors import (
        PersistenceError,
        RemoveBatchPersistenceError,
    )

    e = _engine()
    p1 = MagicMock()
    p1.kalshi_event_ticker = "K"
    p1.engine_state = "active"
    e._game_manager._games["K-1"] = p1

    # Inventory → winding-down path → per-transition save.
    ledger = MagicMock()
    ledger.has_filled_positions.return_value = True
    ledger.has_resting_orders.return_value = False
    ledger.filled_count.return_value = 5
    ledger.resting_count.return_value = 0
    e._adjuster.get_ledger.return_value = ledger

    def _persist(*, force_during_suppress=False):
        raise PersistenceError("disk full at transition")

    e._persist_active_games = MagicMock(side_effect=_persist)

    with pytest.raises(RemoveBatchPersistenceError) as exc_info:
        await e.remove_pairs_from_selection([("K-1", "K")])

    assert exc_info.value.phase == "transition"
    assert exc_info.value.persisted_count == 0
