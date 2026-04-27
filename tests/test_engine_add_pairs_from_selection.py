"""Locks the add_pairs_from_selection flow on TradingEngine, including rollback on
resolve/subscribe/persist failure, idempotent retry, and single-batch persistence at the end.
"""

from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.engine import TradingEngine
from talos.models.market import Market
from talos.models.strategy import ArbPair
from talos.models.tree import ArbPairRecord


def _engine_with_collaborators():
    e = cast(Any, TradingEngine.__new__(TradingEngine))

    # GameManager stub — returns a fake ArbPair from restore_game
    def _restore(record):
        return ArbPair(
            event_ticker=record["event_ticker"],
            ticker_a=record["ticker_a"],
            ticker_b=record["ticker_b"],
            side_a=record.get("side_a", "yes"),
            side_b=record.get("side_b", "no"),
            source=record.get("source"),
        )

    gm = MagicMock()
    gm.restore_game = MagicMock(side_effect=_restore)
    gm.subtitles = {}
    gm.volumes_24h = {}
    gm._volumes_24h = gm.volumes_24h

    @contextmanager
    def _suppress():
        yield

    gm.suppress_on_change = MagicMock(side_effect=_suppress)
    e._game_manager = gm

    e._adjuster = MagicMock()
    e._game_status_resolver = MagicMock()
    e._game_status_resolver.resolve_batch = AsyncMock(return_value={})
    e._game_status_resolver.get = MagicMock(return_value=None)
    e._feed = MagicMock()
    e._feed.subscribe = AsyncMock()
    e._feed.unsubscribe = AsyncMock()
    e._data_collector = None
    e._persist_active_games = MagicMock()
    gm.remove_game = AsyncMock()

    # REST stub — default to admittable cent-tick markets so the Phase 0
    # admission guard lets the record through. Tests that need rejection
    # can override e._rest.get_market.
    async def _get_market(ticker: str) -> Market:
        return Market(
            ticker=ticker,
            event_ticker=ticker,
            title=f"Market {ticker}",
            status="open",
        )

    e._rest = MagicMock()
    e._rest.get_market = AsyncMock(side_effect=_get_market)
    return e


@pytest.mark.asyncio
async def test_add_pairs_wires_adjuster_gsr_feeds_and_persists():
    e = _engine_with_collaborators()
    r = ArbPairRecord(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
        kalshi_event_ticker="KXFEDMENTION-26APR",
        series_ticker="KXFEDMENTION",
        category="Mentions",
    )
    result = await e.add_pairs_from_selection([r.model_dump()])
    assert len(result.admitted) == 1
    assert result.rejected == []
    e._adjuster.add_event.assert_called_once()
    e._game_status_resolver.resolve_batch.assert_awaited_once()
    e._feed.subscribe.assert_awaited()  # at least one subscribe call
    e._persist_active_games.assert_called_once()


@pytest.mark.asyncio
async def test_add_pairs_seeds_volume_from_record():
    e = _engine_with_collaborators()
    r = ArbPairRecord(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
        kalshi_event_ticker="KXFEDMENTION-26APR",
        series_ticker="KXFEDMENTION",
        category="Mentions",
        volume_24h_a=500,
        volume_24h_b=500,
    )
    await e.add_pairs_from_selection([r.model_dump()])
    assert e._game_manager._volumes_24h.get("KXFEDMENTION-26APR-YIEL") == 500


@pytest.mark.asyncio
async def test_add_pairs_sports_calls_resolve_batch_with_subtitles():
    e = _engine_with_collaborators()
    e._game_manager.subtitles = {"KXNBAGAME-26APR20BOSNYR": "BOS at NYR"}
    r = ArbPairRecord(
        event_ticker="KXNBAGAME-26APR20BOSNYR",
        ticker_a="KXNBAGAME-26APR20BOSNYR-BOS",
        ticker_b="KXNBAGAME-26APR20BOSNYR-NYR",
        kalshi_event_ticker="KXNBAGAME-26APR20BOSNYR",
        series_ticker="KXNBAGAME",
        category="Sports",
        side_a="no",
        side_b="no",
    )
    await e.add_pairs_from_selection([r.model_dump()])
    args, _ = e._game_status_resolver.resolve_batch.call_args
    batch = args[0]
    assert batch == [("KXNBAGAME-26APR20BOSNYR", "BOS at NYR")]


@pytest.mark.asyncio
async def test_add_pairs_rolls_back_on_resolve_batch_failure():
    """If resolve_batch raises mid-commit, every partially-applied side
    effect (game_manager restore, adjuster wiring) must be reverted before
    the exception propagates. Without rollback, retries would double-add."""
    e = _engine_with_collaborators()
    e._game_status_resolver.resolve_batch = AsyncMock(
        side_effect=RuntimeError("kalshi 5xx")
    )
    r = ArbPairRecord(
        event_ticker="KX-1",
        ticker_a="KX-1",
        ticker_b="KX-1",
        kalshi_event_ticker="KX-1",
        series_ticker="KX",
        category="Mentions",
    )
    with pytest.raises(RuntimeError, match="kalshi 5xx"):
        await e.add_pairs_from_selection([r.model_dump()])

    # Adjuster.add_event happened — must be reverted via remove_event.
    e._adjuster.add_event.assert_called_once()
    e._adjuster.remove_event.assert_called_once_with("KX-1")
    # Game was restored — must be removed from GameManager too.
    e._game_manager.remove_game.assert_awaited_once_with("KX-1")
    # No persist on failure — staging should remain in TreeScreen for retry.
    e._persist_active_games.assert_not_called()


@pytest.mark.asyncio
async def test_add_pairs_rolls_back_subscribed_tickers_on_subscribe_failure():
    """If a feed subscribe raises mid-batch, prior successful subscribes
    must be unsubscribed during rollback."""
    e = _engine_with_collaborators()

    # First subscribe succeeds; second raises.
    calls: list[str] = []

    async def _subscribe(ticker: str) -> None:
        calls.append(ticker)
        if len(calls) >= 2:
            raise ConnectionError("ws gone")

    e._feed.subscribe = AsyncMock(side_effect=_subscribe)
    records = [
        ArbPairRecord(
            event_ticker=f"KX-{i}",
            ticker_a=f"KX-{i}",
            ticker_b=f"KX-{i}",
            kalshi_event_ticker=f"KX-{i}",
            series_ticker="KX",
            category="Mentions",
        ).model_dump()
        for i in range(2)
    ]
    with pytest.raises(ConnectionError):
        await e.add_pairs_from_selection(records)

    # The first subscribed ticker (calls[0]) should be unsubscribed.
    unsub_args = [c.args[0] for c in e._feed.unsubscribe.await_args_list]
    assert calls[0] in unsub_args
    e._persist_active_games.assert_not_called()


@pytest.mark.asyncio
async def test_add_pairs_rollback_includes_game_status_resolver():
    """GSR.set_expiration was called during step 3 — on failure, rollback
    must call GSR.remove for each seeded ticker so the resolver doesn't
    keep stale entries pointing at non-existent pairs."""
    e = _engine_with_collaborators()
    e._game_status_resolver.resolve_batch = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    r = ArbPairRecord(
        event_ticker="KX-A",
        ticker_a="KX-A",
        ticker_b="KX-A",
        kalshi_event_ticker="KX-A",
        series_ticker="KX",
        category="Mentions",
    )
    with pytest.raises(RuntimeError):
        await e.add_pairs_from_selection([r.model_dump()])
    e._game_status_resolver.remove.assert_called_once_with("KX-A")


@pytest.mark.asyncio
async def test_add_pairs_rollback_runs_on_cancellation():
    """asyncio.CancelledError inherits from BaseException, not Exception.
    Rollback must use `except BaseException:` (or equivalent) so
    cancellation through a worker doesn't skip cleanup. Simulates the
    'mash c twice' scenario at the engine layer."""
    import asyncio

    cancel_event = asyncio.Event()

    async def _resolve(_batch):
        cancel_event.set()
        # Block forever; outer task will cancel us
        await asyncio.sleep(60)

    e = _engine_with_collaborators()
    e._game_status_resolver.resolve_batch = AsyncMock(side_effect=_resolve)
    r = ArbPairRecord(
        event_ticker="KX-CANCEL",
        ticker_a="KX-CANCEL",
        ticker_b="KX-CANCEL",
        kalshi_event_ticker="KX-CANCEL",
        series_ticker="KX",
        category="Mentions",
    )

    task = asyncio.create_task(e.add_pairs_from_selection([r.model_dump()]))
    await cancel_event.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Cancellation must have triggered rollback even though the rollback
    # path in code uses `except BaseException:`. All four side-effect
    # primitives that ran before the cancel point should have been undone.
    e._adjuster.remove_event.assert_called_with("KX-CANCEL")
    e._game_status_resolver.remove.assert_called_with("KX-CANCEL")
    e._game_manager.remove_game.assert_awaited_with("KX-CANCEL")
    e._persist_active_games.assert_not_called()


@pytest.mark.asyncio
async def test_add_pairs_rolls_back_on_persistence_failure():
    """Round 5: if step 6 (persist) fails, the in-memory engine state was
    mutated but the disk snapshot is stale. Rolling back avoids the
    failure mode where a restart resurrects a winding-down pair as
    freely tradable because engine_state never made it to disk."""
    from talos.persistence_errors import PersistenceError

    e = _engine_with_collaborators()
    e._persist_active_games = MagicMock(
        side_effect=PersistenceError("disk full")
    )
    r = ArbPairRecord(
        event_ticker="KX-PERSIST",
        ticker_a="KX-PERSIST",
        ticker_b="KX-PERSIST",
        kalshi_event_ticker="KX-PERSIST",
        series_ticker="KX",
        category="Mentions",
    )
    with pytest.raises(PersistenceError):
        await e.add_pairs_from_selection([r.model_dump()])

    # All four primitives that ran during steps 2-4 must be undone.
    e._adjuster.remove_event.assert_called_with("KX-PERSIST")
    e._game_status_resolver.remove.assert_called_with("KX-PERSIST")
    e._game_manager.remove_game.assert_awaited_with("KX-PERSIST")


@pytest.mark.asyncio
async def test_persist_active_games_propagates_persistence_error():
    """Round 6: previously _persist_active_games swallowed every exception
    at WARNING, including the new PersistenceError, defeating the
    round-5 plumbing. Now it lets PersistenceError through so callers
    can roll back.

    Note: _engine_with_collaborators mocks _persist_active_games to
    keep other tests file-system-free; we delete that override so the
    real class method runs.
    """
    from talos.persistence_errors import PersistenceError

    e = _engine_with_collaborators()
    del e._persist_active_games  # restore class method

    def _boom():
        raise PersistenceError("disk full")

    e._game_manager.on_change = _boom
    with pytest.raises(PersistenceError):
        e._persist_active_games()


@pytest.mark.asyncio
async def test_persist_active_games_swallows_other_exceptions():
    """Non-PersistenceError exceptions stay non-fatal — on_change is a
    fire-and-forget callback for non-safety-critical writers."""
    e = _engine_with_collaborators()
    del e._persist_active_games  # restore class method

    def _boom():
        raise RuntimeError("some other thing")

    e._game_manager.on_change = _boom
    # Must NOT raise — round 6 only escalates PersistenceError
    e._persist_active_games()


@pytest.mark.asyncio
async def test_add_pairs_persists_only_once_at_batch_end():
    e = _engine_with_collaborators()
    records = [
        ArbPairRecord(
            event_ticker=f"KX-{i}",
            ticker_a=f"KX-{i}",
            ticker_b=f"KX-{i}",
            kalshi_event_ticker=f"KX-{i}",
            series_ticker="KX",
            category="Mentions",
        ).model_dump()
        for i in range(5)
    ]
    await e.add_pairs_from_selection(records)
    e._persist_active_games.assert_called_once()


@pytest.mark.asyncio
async def test_add_pairs_retry_is_idempotent_for_already_present_pairs():
    """Round-3 review fix #1: when commit() preserves staging across a
    later failure (e.g. metadata write fails after engine add succeeded),
    re-running add_pairs_from_selection must NOT duplicate side effects:
    no second adjuster.add_event, no second feed.subscribe, no second
    GSR.set_expiration, no second log_game_add. Otherwise the round-1
    toast claim that "adds become no-ops on retry" is false.

    Test: stub _games with a real dict so duplicate-check logic actually
    works (the simpler MagicMock fixture would let restore_game return a
    fresh pair each time, masking the bug). Run add twice on the same
    record. Assert second-call side effects are zero."""
    e = _engine_with_collaborators()
    # Override restore_game to mimic real behavior: the first call adds
    # to _games and returns the pair; the second call finds it present
    # and returns the existing pair without re-adding.
    real_games: dict[str, ArbPair] = {}

    def _restore(record):
        et = record["event_ticker"]
        if et in real_games:
            return real_games[et]
        pair = ArbPair(
            event_ticker=et,
            ticker_a=record["ticker_a"],
            ticker_b=record["ticker_b"],
            side_a=record.get("side_a", "yes"),
            side_b=record.get("side_b", "no"),
            source=record.get("source"),
        )
        real_games[et] = pair
        return pair

    e._game_manager.restore_game = MagicMock(side_effect=_restore)
    e._game_manager._games = real_games

    r = ArbPairRecord(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
        kalshi_event_ticker="KXFEDMENTION-26APR",
        series_ticker="KXFEDMENTION",
        category="Mentions",
    ).model_dump()

    # First call — full wiring expected.
    pairs1 = await e.add_pairs_from_selection([r])
    assert len(pairs1.admitted) == 1
    add_event_after_first = e._adjuster.add_event.call_count
    subscribe_after_first = e._feed.subscribe.await_count
    set_expiration_after_first = e._game_status_resolver.set_expiration.call_count
    resolve_batch_after_first = e._game_status_resolver.resolve_batch.await_count

    assert add_event_after_first >= 1
    assert subscribe_after_first >= 1
    assert set_expiration_after_first >= 1

    # Second call (the retry) — the pair is already in _games so step-2/3/4
    # wiring MUST be skipped entirely.
    pairs2 = await e.add_pairs_from_selection([r])
    # Returned pair list still includes it (UI accounting), but no new wiring.
    assert len(pairs2.admitted) == 1
    assert e._adjuster.add_event.call_count == add_event_after_first
    assert e._feed.subscribe.await_count == subscribe_after_first
    assert e._game_status_resolver.set_expiration.call_count == (
        set_expiration_after_first
    )
    # resolve_batch is gated on `if ... and new_pairs`, so a second call
    # with no new_pairs must not invoke it.
    assert e._game_status_resolver.resolve_batch.await_count == (
        resolve_batch_after_first
    )


@pytest.mark.asyncio
async def test_add_pairs_retry_does_not_emit_duplicate_log_game_add():
    """Round-5 review fix #3: step 5 (data_collector.log_game_add) must
    iterate new_pairs, not pairs. The round-3 idempotency test never
    wired _data_collector and missed this — a successful duplicate
    retry was emitting a second audit row, contradicting the round-1
    toast claim that "retry has zero downstream side effects."

    The audit row has no companion log_game_remove, so a phantom
    duplicate would skew downstream analytics that key off this table."""
    e = _engine_with_collaborators()
    real_games: dict[str, ArbPair] = {}

    def _restore(record):
        et = record["event_ticker"]
        if et in real_games:
            return real_games[et]
        pair = ArbPair(
            event_ticker=et,
            ticker_a=record["ticker_a"],
            ticker_b=record["ticker_b"],
            side_a=record.get("side_a", "yes"),
            side_b=record.get("side_b", "no"),
            source=record.get("source"),
        )
        real_games[et] = pair
        return pair

    e._game_manager.restore_game = MagicMock(side_effect=_restore)
    e._game_manager._games = real_games
    # Wire a real-ish data_collector so log_game_add gets invoked.
    e._data_collector = MagicMock()
    e._data_collector.log_game_add = MagicMock()

    r = ArbPairRecord(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
        kalshi_event_ticker="KXFEDMENTION-26APR",
        series_ticker="KXFEDMENTION",
        category="Mentions",
    ).model_dump()

    # First call — exactly one audit row.
    await e.add_pairs_from_selection([r])
    assert e._data_collector.log_game_add.call_count == 1

    # Second call (retry) — pair is already in _games, so no new audit row.
    await e.add_pairs_from_selection([r])
    assert e._data_collector.log_game_add.call_count == 1


@pytest.mark.asyncio
async def test_add_pairs_retry_persist_failure_does_not_remove_existing_pair():
    """Round-5 review fix #2: when a retry of an already-monitored pair
    fails at the final persist step, _rollback_partial_add must NOT
    call game_manager.remove_game on the pre-existing pair. The pair
    was wired by a prior successful add and its presence is correct.
    Removing it would erase legitimate engine state the user did not
    ask to delete (and silently break any positions still open).

    Pre-fix, both rollback call sites passed `pairs=pairs` (which
    includes the pre-existing pair returned by restore_game). The fix
    is to pass `pairs=new_pairs` so rollback only undoes side effects
    of the current call."""
    from talos.persistence_errors import PersistenceError

    e = _engine_with_collaborators()
    real_games: dict[str, ArbPair] = {}
    remove_game_calls: list[str] = []

    def _restore(record):
        et = record["event_ticker"]
        if et in real_games:
            return real_games[et]
        pair = ArbPair(
            event_ticker=et,
            ticker_a=record["ticker_a"],
            ticker_b=record["ticker_b"],
            side_a=record.get("side_a", "yes"),
            side_b=record.get("side_b", "no"),
            source=record.get("source"),
        )
        real_games[et] = pair
        return pair

    async def _remove_game(pair_ticker):
        remove_game_calls.append(pair_ticker)
        real_games.pop(pair_ticker, None)

    e._game_manager.restore_game = MagicMock(side_effect=_restore)
    e._game_manager._games = real_games
    e._game_manager.remove_game = AsyncMock(side_effect=_remove_game)

    r = ArbPairRecord(
        event_ticker="KXFEDMENTION-26APR-YIEL",
        ticker_a="KXFEDMENTION-26APR-YIEL",
        ticker_b="KXFEDMENTION-26APR-YIEL",
        kalshi_event_ticker="KXFEDMENTION-26APR",
        series_ticker="KXFEDMENTION",
        category="Mentions",
    ).model_dump()

    # First call — succeeds, pair lands in _games.
    await e.add_pairs_from_selection([r])
    assert "KXFEDMENTION-26APR-YIEL" in real_games
    assert remove_game_calls == []  # nothing rolled back

    # Second call (retry) — make persist fail at the final step. The
    # rollback that follows must not remove the pre-existing pair.
    e._persist_active_games = MagicMock(
        side_effect=PersistenceError("disk full")
    )

    with pytest.raises(PersistenceError):
        await e.add_pairs_from_selection([r])

    # CRITICAL contract: rollback did NOT touch the pre-existing pair.
    # Pre-fix, remove_game_calls would contain the pair_ticker because
    # _rollback_partial_add iterated `pairs` (which included it).
    assert remove_game_calls == []
    # And the pair is still in _games (user's prior successful add is
    # still durable in memory).
    assert "KXFEDMENTION-26APR-YIEL" in real_games
