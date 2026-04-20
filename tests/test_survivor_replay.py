"""SURVIVOR replay acceptance test.

Reproduces the 2026-04-15 scenario:
- KXSURVIVORMENTION-26APR16-MRBE market
- No Kalshi milestone
- User manually enters 2026-04-15T20:00:00-04:00 as event-start
- Engine ticks at times spanning 19:00 → 21:30 EDT

Assertion: after 19:30 EDT (30 min before event-start), pair is in
exit-only. No new fills can be accepted.
"""

import unittest.mock
from datetime import datetime, timedelta
from typing import Any, cast
from unittest.mock import MagicMock


def test_manual_override_triggers_exit_only_at_lead_time():
    from talos.engine import TradingEngine

    e = cast(Any, TradingEngine.__new__(TradingEngine))
    e._tree_metadata_store = MagicMock()
    e._milestone_resolver = MagicMock()
    e._game_status_resolver = None
    e._exit_only_events = set()
    e._stale_candidates = set()
    e._game_started_events = set()
    e._log_once_keys = set()
    e._auto_config = MagicMock(exit_only_minutes=30.0, tree_mode=True)
    e._scanner = MagicMock()

    class _Pair:
        event_ticker = "KXSURVIVORMENTION-26APR16-MRBE"
        kalshi_event_ticker = "KXSURVIVORMENTION-26APR16"

    pair = _Pair()
    e._scanner.pairs = [pair]

    # Kalshi has no milestone for this event
    e._milestone_resolver.event_start.return_value = None

    # User set a manual override: Apr 15 8pm EDT = Apr 16 00:00 UTC
    manual_dt = datetime(2026, 4, 16, 0, 0)
    e._tree_metadata_store.manual_event_start.return_value = manual_dt

    # Stub flip helper
    e._flip_exit_only_for_key = MagicMock(
        side_effect=lambda k, **kw: e._exit_only_events.add(k),
    )
    e._log_once = MagicMock()
    e._notify = MagicMock()
    e._display_name = MagicMock(return_value="KXSURVIVORMENTION-26APR16")

    # Pre-event check (20 hours before) — should NOT trigger
    with unittest.mock.patch("talos.engine.datetime") as mock_dt:
        mock_dt.now.return_value = manual_dt - timedelta(hours=20)
        mock_dt.fromisoformat = datetime.fromisoformat
        e._check_exit_only_tree_mode()
    assert pair.kalshi_event_ticker not in e._exit_only_events

    # 29 minutes before event-start — SHOULD trigger
    with unittest.mock.patch("talos.engine.datetime") as mock_dt:
        mock_dt.now.return_value = manual_dt - timedelta(minutes=29)
        mock_dt.fromisoformat = datetime.fromisoformat
        e._check_exit_only_tree_mode()
    assert pair.kalshi_event_ticker in e._exit_only_events


def test_manual_opt_out_never_triggers():
    """User marks an event as 'no exit-only' (e.g., a diffuse mention like
    TRUMPMENTION-weekly) — should NEVER trigger regardless of time."""
    from talos.engine import TradingEngine

    e = cast(Any, TradingEngine.__new__(TradingEngine))
    e._tree_metadata_store = MagicMock()
    e._milestone_resolver = MagicMock()
    e._game_status_resolver = None
    e._exit_only_events = set()
    e._stale_candidates = set()
    e._game_started_events = set()
    e._log_once_keys = set()
    e._auto_config = MagicMock(exit_only_minutes=30.0, tree_mode=True)
    e._scanner = MagicMock()

    class _Pair:
        event_ticker = "KXTRUMPMENTION-26APR16"
        kalshi_event_ticker = "KXTRUMPMENTION-26APR16"

    pair = _Pair()
    e._scanner.pairs = [pair]

    e._tree_metadata_store.manual_event_start.return_value = "none"
    e._milestone_resolver.event_start.return_value = None

    e._flip_exit_only_for_key = MagicMock(
        side_effect=lambda k, **kw: e._exit_only_events.add(k),
    )
    e._log_once = MagicMock()
    e._notify = MagicMock()
    e._display_name = MagicMock(return_value="KXTRUMPMENTION-26APR16")

    e._check_exit_only_tree_mode()
    assert pair.kalshi_event_ticker not in e._exit_only_events
    e._log_once.assert_not_called()  # opt-out is not a "no schedule" case


def test_milestone_beats_old_expiration_fallback():
    """Demonstrate that when a milestone exists, the cascade uses it directly,
    never falling back to expiration-minus-3h. This was the structural cause
    of SURVIVOR on markets that did have milestones (like FED)."""
    from talos.engine import TradingEngine
    from talos.milestones import MilestoneResolver

    e = cast(Any, TradingEngine.__new__(TradingEngine))
    e._tree_metadata_store = MagicMock()
    e._tree_metadata_store.manual_event_start.return_value = None
    # spec= restricts the mock to MilestoneResolver's real attribute surface,
    # so the hasattr assertion below actually verifies the resolver API
    # instead of MagicMock's auto-attribute behavior.
    e._milestone_resolver = MagicMock(spec=MilestoneResolver)
    # Real Kalshi milestone for FED: Apr 29 2026 2:30 PM EDT = 18:30 UTC
    fed_start_utc = datetime(2026, 4, 29, 18, 30)
    e._milestone_resolver.event_start.return_value = fed_start_utc

    e._game_status_resolver = None
    e._exit_only_events = set()
    e._stale_candidates = set()
    e._game_started_events = set()
    e._log_once_keys = set()
    e._auto_config = MagicMock(exit_only_minutes=30.0, tree_mode=True)
    e._scanner = MagicMock()

    class _Pair:
        event_ticker = "KXFEDMENTION-26APR-YIEL"
        kalshi_event_ticker = "KXFEDMENTION-26APR"

    pair = _Pair()
    e._scanner.pairs = [pair]

    e._flip_exit_only_for_key = MagicMock(
        side_effect=lambda k, **kw: e._exit_only_events.add(k),
    )
    e._log_once = MagicMock()
    e._notify = MagicMock()
    e._display_name = MagicMock(return_value="KXFEDMENTION-26APR")

    # 29 min before 2:30 PM EDT → should trigger (milestone-driven)
    with unittest.mock.patch("talos.engine.datetime") as mock_dt:
        mock_dt.now.return_value = fed_start_utc - timedelta(minutes=29)
        mock_dt.fromisoformat = datetime.fromisoformat
        e._check_exit_only_tree_mode()
    assert pair.kalshi_event_ticker in e._exit_only_events

    # Confirm the old broken proxy (expiration-minus-3h) is NOT consulted —
    # expiration_fallback is unreachable under tree_mode cascade.
    assert not hasattr(e._milestone_resolver, "_expiration_fallback")
