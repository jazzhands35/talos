"""Tests for extended auto-accept duration cap."""

from __future__ import annotations

from talos.auto_accept import AutoAcceptState


class TestAutoAcceptDuration:
    """AutoAcceptState handles durations beyond 24h."""

    def test_accepts_168h_duration(self) -> None:
        state = AutoAcceptState()
        state.start(hours=168.0)
        assert state.active
        assert state.duration is not None
        assert state.duration.total_seconds() == 168 * 3600

    def test_remaining_seconds_for_long_duration(self) -> None:
        state = AutoAcceptState()
        state.start(hours=100.0)
        assert state.remaining_seconds() > 99 * 3600

    def test_not_expired_within_168h(self) -> None:
        state = AutoAcceptState()
        state.start(hours=168.0)
        assert not state.is_expired()
