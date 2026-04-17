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
    e._data_collector = None
    e._persist_active_games = MagicMock()
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
