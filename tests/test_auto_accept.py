"""Tests for ExecutionMode state machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from talos.auto_accept import ExecutionMode, Mode


def test_default_is_automatic():
    em = ExecutionMode()
    assert em.mode is Mode.AUTOMATIC
    assert em.is_automatic is True
    assert em.auto_stop_at is None
    assert em.accepted_count == 0


def test_enter_automatic_indefinite():
    em = ExecutionMode()
    em.enter_manual()  # start from manual
    em.enter_automatic()
    assert em.mode is Mode.AUTOMATIC
    assert em.auto_stop_at is None
    assert em.accepted_count == 0
    assert em.started_at is not None


def test_enter_automatic_with_timer():
    em = ExecutionMode()
    em.enter_automatic(hours=2.0)
    assert em.mode is Mode.AUTOMATIC
    assert em.auto_stop_at is not None
    expected = em.started_at + timedelta(hours=2)
    assert abs((em.auto_stop_at - expected).total_seconds()) < 1


def test_enter_automatic_resets_accepted_count():
    em = ExecutionMode()
    em.accepted_count = 42
    em.enter_automatic()
    assert em.accepted_count == 0


def test_enter_manual():
    em = ExecutionMode()
    em.enter_automatic(hours=1.0)
    em.enter_manual()
    assert em.mode is Mode.MANUAL
    assert em.auto_stop_at is None


def test_is_expired_false_when_indefinite():
    em = ExecutionMode()
    em.enter_automatic()  # indefinite
    assert em.is_expired() is False


def test_is_expired_false_within_duration():
    em = ExecutionMode()
    em.enter_automatic(hours=2.0)
    assert em.is_expired() is False


def test_is_expired_true_after_duration():
    em = ExecutionMode()
    em.enter_automatic(hours=1.0)
    em.auto_stop_at = datetime.now(UTC) - timedelta(minutes=1)
    assert em.is_expired() is True


def test_is_expired_false_in_manual_mode():
    em = ExecutionMode()
    em.enter_manual()
    assert em.is_expired() is False


def test_remaining_str_with_timer():
    em = ExecutionMode()
    em.enter_automatic(hours=1.0)
    remaining = em.remaining_str()
    assert ":" in remaining


def test_remaining_str_indefinite_returns_empty():
    em = ExecutionMode()
    em.enter_automatic()  # indefinite
    assert em.remaining_str() == ""


def test_remaining_str_manual_returns_empty():
    em = ExecutionMode()
    em.enter_manual()
    assert em.remaining_str() == ""


def test_elapsed_str():
    em = ExecutionMode()
    em.enter_automatic()
    em.started_at = datetime.now(UTC) - timedelta(minutes=35)
    elapsed = em.elapsed_str()
    assert elapsed.startswith("0:35:")


def test_remaining_seconds_indefinite_returns_zero():
    em = ExecutionMode()
    em.enter_automatic()  # indefinite
    assert em.remaining_seconds() == 0.0


def test_remaining_seconds_with_timer():
    em = ExecutionMode()
    em.enter_automatic(hours=1.0)
    remaining = em.remaining_seconds()
    assert 3590 < remaining <= 3600
