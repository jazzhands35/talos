from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.engine import TradingEngine
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
    pairs = await e.add_pairs_from_selection([r.model_dump()])
    assert len(pairs) == 1
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
