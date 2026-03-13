"""Tests for auto-accept state management."""

from datetime import UTC, datetime, timedelta

from talos.auto_accept import AutoAcceptState


def test_initial_state_inactive():
    state = AutoAcceptState()
    assert state.active is False
    assert state.started_at is None
    assert state.duration is None
    assert state.accepted_count == 0


def test_start_sets_active():
    state = AutoAcceptState()
    state.start(hours=2.0)
    assert state.active is True
    assert state.started_at is not None
    assert state.duration == timedelta(hours=2)
    assert state.accepted_count == 0


def test_stop_clears_active():
    state = AutoAcceptState()
    state.start(hours=1.0)
    state.stop()
    assert state.active is False


def test_is_expired_false_within_duration():
    state = AutoAcceptState()
    state.start(hours=2.0)
    assert state.is_expired() is False


def test_is_expired_true_after_duration():
    state = AutoAcceptState()
    state.start(hours=1.0)
    state.started_at = datetime.now(UTC) - timedelta(hours=1, minutes=1)
    assert state.is_expired() is True


def test_remaining_seconds():
    state = AutoAcceptState()
    state.start(hours=1.0)
    remaining = state.remaining_seconds()
    assert 3590 < remaining <= 3600


def test_remaining_seconds_inactive_returns_zero():
    state = AutoAcceptState()
    assert state.remaining_seconds() == 0.0


def test_elapsed_str_format():
    state = AutoAcceptState()
    state.start(hours=1.0)
    state.started_at = datetime.now(UTC) - timedelta(minutes=35)
    elapsed = state.elapsed_str()
    assert elapsed.startswith("0:35:")


def test_remaining_str_format():
    state = AutoAcceptState()
    state.start(hours=1.0)
    remaining = state.remaining_str()
    assert ":" in remaining
