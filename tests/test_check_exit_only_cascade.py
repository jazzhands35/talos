"""Locks the exit-only sibling-cascade logic: when one event flips to exit-only via milestone,
sports GSR, or manual opt-out, sibling pairs in the same event must follow.
"""

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
    # _flip_exit_only_for_key now also marks engine_state and best-effort
    # persists; stub the touchpoints so tests using the real method don't
    # depend on a fully-wired engine.
    e._game_manager = MagicMock()
    e._game_manager.get_game.return_value = None
    e._game_manager.on_change = None
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
    # Replace the MagicMock stub with the real bound method so the actual
    # multi-pair behavior is exercised.
    e._flip_exit_only_for_key = TradingEngine._flip_exit_only_for_key.__get__(e)
    e._check_exit_only_tree_mode()
    # All sibling pair event_tickers should be in _exit_only_events, NOT the
    # kalshi_event_ticker — _exit_only_events is a pair-level set.
    assert "K-1" in e._exit_only_events
    assert "K-2" in e._exit_only_events
    assert "K" not in e._exit_only_events


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
    # Use real method so the pair-keyed behavior is exercised.
    e._flip_exit_only_for_key = TradingEngine._flip_exit_only_for_key.__get__(e)
    e._check_exit_only_tree_mode()
    # For sports, event_ticker == kalshi_event_ticker, so the pair's
    # event_ticker is added to _exit_only_events.
    assert "KXNBAGAME-26APR20BOSNYR" in e._exit_only_events


def test_no_schedule_logs_once_and_skips_flip():
    p = _Pair("K-1", "K")
    e = _engine_with_scanner([p])
    e._tree_metadata_store.manual_event_start.return_value = None
    e._milestone_resolver.event_start.return_value = None
    # Pretend the resolver is healthy so the safety-degradation branch does
    # NOT fire, and the legacy log-and-skip path is exercised.
    e._milestone_resolver.is_healthy.return_value = True
    e._game_status_resolver.get.return_value = None
    e._check_exit_only_tree_mode()
    e._log_once.assert_called_once()
    e._flip_exit_only_for_key.assert_not_called()


def test_milestones_unavailable_forces_exit_only():
    """Safety degradation: if the resolver reports unhealthy (never loaded
    OR last refresh stale OR index empty) and the cascade has no other
    source, force exit-only rather than trade blind. Protects restored
    pairs across a tree-mode restart while milestones are still
    bootstrapping AND across silent empty-refresh periods."""
    p = _Pair("K-1", "K")
    e = _engine_with_scanner([p])
    e._tree_metadata_store.manual_event_start.return_value = None
    e._milestone_resolver.event_start.return_value = None
    e._milestone_resolver.is_healthy.return_value = False
    e._game_status_resolver.get.return_value = None
    e._check_exit_only_tree_mode()
    e._flip_exit_only_for_key.assert_called_once()
    args, kwargs = e._flip_exit_only_for_key.call_args
    assert kwargs.get("reason") == "milestones_unavailable"


def test_empty_index_after_refresh_still_forces_exit_only():
    """Regression: previously the guard checked `last_refresh is None` only,
    so a successful-but-empty refresh would mark last_refresh and let the
    cascade fall through to "no schedule" → tradable. is_healthy() now
    catches this case too."""
    from talos.milestones import MilestoneResolver

    real_resolver = MilestoneResolver()
    real_resolver._last_refresh = datetime.now(UTC)  # fresh
    # _by_event_ticker stays empty → count == 0 → not healthy
    assert not real_resolver.is_healthy()


def test_tree_mode_flip_adds_all_sibling_pair_event_tickers():
    """Non-sports multi-market: flip adds every market-pair's event_ticker
    (not the kalshi_event_ticker) so adjuster ledger lookups still work."""
    p1 = _Pair("KXFEDMENTION-26APR-YIEL", "KXFEDMENTION-26APR")
    p2 = _Pair("KXFEDMENTION-26APR-TRAD", "KXFEDMENTION-26APR")
    p3 = _Pair("KXFEDMENTION-26APR-RECE", "KXFEDMENTION-26APR")
    e = _engine_with_scanner([p1, p2, p3])
    e._tree_metadata_store.manual_event_start.return_value = None
    start = datetime.now(UTC) + timedelta(minutes=20)
    e._milestone_resolver.event_start.return_value = start
    # Replace the MagicMock _flip_exit_only_for_key stub with the real method
    e._flip_exit_only_for_key = TradingEngine._flip_exit_only_for_key.__get__(e)

    e._check_exit_only_tree_mode()

    # All three market-pairs should be in _exit_only_events by their
    # PAIR event_ticker, not by the kalshi_event_ticker
    assert "KXFEDMENTION-26APR-YIEL" in e._exit_only_events
    assert "KXFEDMENTION-26APR-TRAD" in e._exit_only_events
    assert "KXFEDMENTION-26APR-RECE" in e._exit_only_events
    assert "KXFEDMENTION-26APR" not in e._exit_only_events
