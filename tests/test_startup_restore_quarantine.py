"""F32 + F37: startup-restore admission for Phase-0-incompatible markets.

Persisted pairs whose market became fractional/sub-cent while Talos was
offline must restore into a quarantined exit_only state, with the
quarantine durably persisted so it survives a subsequent crash."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _persisted_record(
    event_ticker: str,
    ticker_a: str,
    ticker_b: str,
    engine_state: str = "active",
) -> dict[str, Any]:
    """Build a persisted games_full.json-shape record."""
    return {
        "event_ticker": event_ticker,
        "ticker_a": ticker_a,
        "ticker_b": ticker_b,
        "engine_state": engine_state,
        "fee_type": "quadratic_with_maker_fees",
        "fee_rate": 0.0175,
        "close_time": "2026-12-31T00:00:00Z",
        "expected_expiration_time": None,
        "label": "",
        "sub_title": "",
        "side_a": "no",
        "side_b": "no",
        "kalshi_event_ticker": event_ticker,
        "series_ticker": event_ticker.split("-")[0],
        "talos_id": 1,
    }


def _prep_engine_for_setup(engine_fixture) -> Any:
    """Extend the shared engine_fixture with the minimal collaborators needed
    to run ``_setup_initial_games``.

    The base fixture targets ``add_pairs_from_selection``; startup-restore
    exercises more of the engine surface (feed.subscribe_bulk, notify,
    game_manager.active_games, event discovery, etc.) so stub them in here.
    """
    from talos.engine import TradingEngine
    from talos.models.strategy import ArbPair

    e = engine_fixture

    # The base fixture's restore_game builder ignores engine_state — honor it
    # so winding_down and exit_only records round-trip faithfully.
    def _restore(record):
        return ArbPair(
            event_ticker=record["event_ticker"],
            ticker_a=record["ticker_a"],
            ticker_b=record["ticker_b"],
            side_a=record.get("side_a", "no"),
            side_b=record.get("side_b", "no"),
            engine_state=record.get("engine_state", "active"),
            kalshi_event_ticker=record.get("kalshi_event_ticker", ""),
            source=record.get("source"),
        )

    e._game_manager.restore_game = MagicMock(side_effect=_restore)
    e._game_manager.active_games = []
    e._game_manager.subtitles = {}

    # get_game is used by the tests to assert engine_state; back it with a
    # dict the test can populate.
    _game_by_event: dict[str, ArbPair] = {}

    def _get_game(event_ticker: str):
        return _game_by_event.get(event_ticker)

    e._game_manager.get_game = MagicMock(side_effect=_get_game)

    # Feed stubs that _setup_initial_games touches.
    e._feed.subscribe_bulk = AsyncMock()

    # Discovery returns no active events — focus the test on the restore path.
    e._discover_active_events = AsyncMock(return_value=[])

    # Notify + persist stubs (spies set by individual tests as needed).
    e._notify = MagicMock()

    # Bind the real _apply_persisted_engine_state method so the quarantine
    # guard's `if pair.engine_state not in ("exit_only", "winding_down")`
    # branch interacts with real state transitions.
    e._apply_persisted_engine_state = TradingEngine._apply_persisted_engine_state.__get__(e)

    # Starting collections expected by the method.
    e._winding_down = set()
    e._exit_only_events = set()
    e._initial_games = []
    e._initial_games_full = []
    e._data_collector = None

    # Patch restore_game to also populate the get_game side-table so tests
    # can retrieve the pair post-restore.
    orig_restore = e._game_manager.restore_game.side_effect

    def _restore_and_index(record):
        pair = orig_restore(record)
        _game_by_event[pair.event_ticker] = pair
        return pair

    e._game_manager.restore_game = MagicMock(side_effect=_restore_and_index)

    return e


@pytest.mark.asyncio
async def test_restore_quarantines_pair_when_market_is_now_fractional(engine_fixture):
    """A persisted 'active' pair whose market is now fractional must be
    quarantined into exit_only and the quarantine must be durably persisted."""
    engine = _prep_engine_for_setup(engine_fixture)

    record = _persisted_record(
        event_ticker="KXF-26JAN01",
        ticker_a="KXF-26JAN01-A",
        ticker_b="KXF-26JAN01-B",
    )
    engine._initial_games_full = [record]
    engine._initial_games = []

    notifications: list[tuple[str, str]] = []
    engine._notify = lambda msg, sev="info", **_: notifications.append((msg, sev))

    persist_calls: list[int] = []
    engine._persist_active_games = lambda *a, **kw: persist_calls.append(1)

    await engine._setup_initial_games()

    assert "KXF-26JAN01" in engine._exit_only_events, (
        "expected KXF-26JAN01 in _exit_only_events after quarantine; "
        f"got {engine._exit_only_events}"
    )
    pair = engine._game_manager.get_game("KXF-26JAN01")
    assert pair is not None
    assert pair.engine_state == "exit_only", (
        f"expected engine_state='exit_only', got '{pair.engine_state}'"
    )

    quarantine_notifs = [m for m, sev in notifications if "exit_only" in m.lower()]
    assert quarantine_notifs, f"expected quarantine notification, got {notifications}"

    assert persist_calls, "expected _persist_active_games to fire for quarantine durability"


@pytest.mark.asyncio
async def test_restore_does_not_quarantine_clean_market(engine_fixture):
    """A persisted pair whose market is cent-only passes admission — no
    quarantine, no durable-persist fire."""
    engine = _prep_engine_for_setup(engine_fixture)

    record = _persisted_record(
        event_ticker="KXA-26JAN01",
        ticker_a="KXA-26JAN01-A",
        ticker_b="KXA-26JAN01-B",
    )
    engine._initial_games_full = [record]
    engine._initial_games = []

    persist_calls: list[int] = []
    engine._persist_active_games = lambda *a, **kw: persist_calls.append(1)

    await engine._setup_initial_games()

    assert "KXA-26JAN01" not in engine._exit_only_events, (
        "cent-market pair must not be quarantined"
    )
    assert not persist_calls, f"expected no force-persist, got {persist_calls} calls"


@pytest.mark.asyncio
async def test_restore_rest_failure_does_not_quarantine(engine_fixture):
    """If the REST call during admission check fails (network error, timeout),
    the pair is left in its persisted state — no false-positive quarantine
    on transient errors."""
    engine = _prep_engine_for_setup(engine_fixture)

    record = _persisted_record(
        event_ticker="KXA-26JAN01",
        ticker_a="KXA-26JAN01-A",
        ticker_b="KXA-26JAN01-B",
    )
    engine._initial_games_full = [record]
    engine._initial_games = []

    async def _fail(ticker: str):
        raise RuntimeError(f"simulated REST failure for {ticker}")

    engine._rest.get_market = _fail

    persist_calls: list[int] = []
    engine._persist_active_games = lambda *a, **kw: persist_calls.append(1)

    await engine._setup_initial_games()

    assert "KXA-26JAN01" not in engine._exit_only_events, (
        "REST failure must not trigger quarantine"
    )
    assert not persist_calls, "REST failure must not trigger durable persist"
