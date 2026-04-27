"""Tests apply_persisted_engine_state on engine startup: restores winding_down / exit_only / active
states + tolerates missing attrs in older persisted blobs.
"""

from typing import Any, cast
from unittest.mock import MagicMock


def test_apply_persisted_engine_state_winding_down():
    from talos.engine import TradingEngine

    e = cast(Any, TradingEngine.__new__(TradingEngine))
    e._winding_down = set()
    e._exit_only_events = set()

    pair = MagicMock()
    pair.event_ticker = "K-1"
    pair.engine_state = "winding_down"

    e._apply_persisted_engine_state(pair)

    assert "K-1" in e._winding_down
    assert "K-1" in e._exit_only_events


def test_apply_persisted_engine_state_exit_only():
    from talos.engine import TradingEngine

    e = cast(Any, TradingEngine.__new__(TradingEngine))
    e._winding_down = set()
    e._exit_only_events = set()
    pair = MagicMock()
    pair.event_ticker = "K-1"
    pair.engine_state = "exit_only"
    e._apply_persisted_engine_state(pair)
    assert "K-1" in e._exit_only_events
    assert "K-1" not in e._winding_down


def test_apply_persisted_engine_state_active_is_noop():
    from talos.engine import TradingEngine

    e = cast(Any, TradingEngine.__new__(TradingEngine))
    e._winding_down = set()
    e._exit_only_events = set()
    pair = MagicMock()
    pair.event_ticker = "K-1"
    pair.engine_state = "active"
    e._apply_persisted_engine_state(pair)
    assert not e._winding_down
    assert not e._exit_only_events


def test_apply_persisted_engine_state_missing_attr_treated_as_active():
    """Older persisted records without engine_state should not trigger any state."""
    from talos.engine import TradingEngine

    e = cast(Any, TradingEngine.__new__(TradingEngine))
    e._winding_down = set()
    e._exit_only_events = set()

    # Plain object with no engine_state attr → getattr default is "active"
    class _MinimalPair:
        event_ticker = "K-1"

    e._apply_persisted_engine_state(_MinimalPair())
    assert not e._winding_down
    assert not e._exit_only_events
