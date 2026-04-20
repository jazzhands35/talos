from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

from talos.engine import TradingEngine


def _make_engine_with_collaborators() -> Any:
    """Build an engine instance with ONLY the collaborators the cascade needs."""
    engine = cast(Any, TradingEngine.__new__(TradingEngine))
    engine._tree_metadata_store = MagicMock()
    engine._milestone_resolver = MagicMock()
    engine._game_status_resolver = MagicMock()
    return engine


class _Pair:
    def __init__(self, event_ticker: str, kalshi_event_ticker: str = ""):
        self.event_ticker = event_ticker
        self.kalshi_event_ticker = kalshi_event_ticker or event_ticker


def test_manual_opt_out_wins():
    e = _make_engine_with_collaborators()
    e._tree_metadata_store.manual_event_start.return_value = "none"
    pair = _Pair("K-1", "K")

    start, source = e._resolve_event_start("K", pair)
    assert start is None
    assert source == "manual_opt_out"


def test_manual_override_wins_over_milestone():
    e = _make_engine_with_collaborators()
    manual_dt = datetime(2026, 4, 22, 20, 0, tzinfo=UTC)
    milestone_dt = datetime(2026, 4, 22, 20, 5, tzinfo=UTC)
    e._tree_metadata_store.manual_event_start.return_value = manual_dt
    e._milestone_resolver.event_start.return_value = milestone_dt
    pair = _Pair("K-1", "K")

    start, source = e._resolve_event_start("K", pair)
    assert start == manual_dt
    assert source == "manual"


def test_milestone_used_when_no_manual():
    e = _make_engine_with_collaborators()
    e._tree_metadata_store.manual_event_start.return_value = None
    ms = datetime(2026, 4, 22, 20, 0, tzinfo=UTC)
    e._milestone_resolver.event_start.return_value = ms
    pair = _Pair("K-1", "K")

    start, source = e._resolve_event_start("K", pair)
    assert start == ms
    assert source == "milestone"


def test_sports_gsr_used_as_third_fallback():
    e = _make_engine_with_collaborators()
    e._tree_metadata_store.manual_event_start.return_value = None
    e._milestone_resolver.event_start.return_value = None

    gsr_dt = datetime(2026, 4, 20, 18, 0, tzinfo=UTC)
    gs_stub = MagicMock()
    gs_stub.scheduled_start = gsr_dt
    e._game_status_resolver.get.return_value = gs_stub
    pair = _Pair("KXNBAGAME-26APR20BOSNYR")

    start, source = e._resolve_event_start("KXNBAGAME-26APR20BOSNYR", pair)
    assert start == gsr_dt
    assert source == "sports_gsr"


def test_no_source_available_returns_none_none():
    e = _make_engine_with_collaborators()
    e._tree_metadata_store.manual_event_start.return_value = None
    e._milestone_resolver.event_start.return_value = None
    e._game_status_resolver.get.return_value = None
    pair = _Pair("K-1", "K")

    start, source = e._resolve_event_start("K", pair)
    assert start is None
    assert source is None
