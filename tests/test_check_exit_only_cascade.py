from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import MagicMock

from talos.engine import TradingEngine


def _engine_with_scanner(pairs):
    e = cast(Any, TradingEngine.__new__(TradingEngine))
    e._tree_metadata_store = MagicMock()
    e._milestone_resolver = MagicMock()
    e._game_status_resolver = MagicMock()
    e._exit_only_events = set()
    e._stale_candidates = set()
    e._game_started_events = set()
    e._log_once_keys = set()
    scanner = MagicMock()
    scanner.pairs = pairs
    e._scanner = scanner
    e._auto_config = MagicMock(exit_only_minutes=30.0, tree_mode=True)
    # Stub _flip_exit_only_for_key
    e._flip_exit_only_for_key = MagicMock(
        side_effect=lambda key, **kw: e._exit_only_events.add(key),
    )
    e._log_once = MagicMock()
    e._notify = MagicMock()
    e._display_name = MagicMock(return_value="fake-name")
    return e


class _Pair:
    def __init__(self, event_ticker, kalshi_event_ticker=""):
        self.event_ticker = event_ticker
        self.kalshi_event_ticker = kalshi_event_ticker or event_ticker


def test_manual_opt_out_prevents_flip():
    p = _Pair("K-1", "K")
    e = _engine_with_scanner([p])
    e._tree_metadata_store.manual_event_start.return_value = "none"
    e._check_exit_only_tree_mode()
    assert "K" not in e._exit_only_events


def test_milestone_within_lead_flips_all_sibling_pairs():
    p1 = _Pair("K-1", "K")
    p2 = _Pair("K-2", "K")
    e = _engine_with_scanner([p1, p2])
    e._tree_metadata_store.manual_event_start.return_value = None
    # Start time is now + 20 min → inside the 30-min lead window
    start = datetime.now(UTC) + timedelta(minutes=20)
    e._milestone_resolver.event_start.return_value = start
    e._check_exit_only_tree_mode()
    # _flip_exit_only_for_key should have been called once (dedupe by kalshi ticker)
    assert e._flip_exit_only_for_key.call_count == 1
    assert "K" in e._exit_only_events


def test_milestone_beyond_lead_does_not_flip():
    p = _Pair("K-1", "K")
    e = _engine_with_scanner([p])
    e._tree_metadata_store.manual_event_start.return_value = None
    # Start time is now + 45 min → outside the 30-min lead window
    start = datetime.now(UTC) + timedelta(minutes=45)
    e._milestone_resolver.event_start.return_value = start
    e._check_exit_only_tree_mode()
    e._flip_exit_only_for_key.assert_not_called()


def test_sports_gsr_live_state_flips_immediately():
    p = _Pair("KXNBAGAME-26APR20BOSNYR")
    e = _engine_with_scanner([p])
    e._tree_metadata_store.manual_event_start.return_value = None
    e._milestone_resolver.event_start.return_value = None
    gs = MagicMock()
    gs.state = "live"
    gs.scheduled_start = datetime.now(UTC) - timedelta(minutes=5)
    e._game_status_resolver.get.return_value = gs
    e._check_exit_only_tree_mode()
    e._flip_exit_only_for_key.assert_called_once()


def test_no_schedule_logs_once_and_skips_flip():
    p = _Pair("K-1", "K")
    e = _engine_with_scanner([p])
    e._tree_metadata_store.manual_event_start.return_value = None
    e._milestone_resolver.event_start.return_value = None
    e._game_status_resolver.get.return_value = None
    e._check_exit_only_tree_mode()
    e._log_once.assert_called_once()
    e._flip_exit_only_for_key.assert_not_called()
