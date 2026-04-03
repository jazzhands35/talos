"""Execution mode state management.

Two modes: Automatic (proposals auto-approve) and Manual (operator approves).
Optional auto_stop_at on automatic mode for timed sessions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum


class Mode(Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


@dataclass
class ExecutionMode:
    """Tracks current execution mode and optional auto-stop timer.

    This is runtime state, not persisted. Startup defaults come from
    settings.json — see persistence.py.
    """

    mode: Mode = Mode.AUTOMATIC
    auto_stop_at: datetime | None = None
    accepted_count: int = 0
    started_at: datetime | None = None

    def enter_automatic(self, hours: float | None = None) -> None:
        """Enter automatic mode. hours=None means indefinite."""
        self.mode = Mode.AUTOMATIC
        self.started_at = datetime.now(UTC)
        self.accepted_count = 0
        if hours is not None:
            self.auto_stop_at = self.started_at + timedelta(hours=hours)
        else:
            self.auto_stop_at = None

    def enter_manual(self) -> None:
        """Enter manual mode."""
        self.mode = Mode.MANUAL
        self.auto_stop_at = None

    @property
    def is_automatic(self) -> bool:
        return self.mode is Mode.AUTOMATIC

    def is_expired(self) -> bool:
        """True if auto_stop_at has passed. Always False if indefinite or manual."""
        if self.auto_stop_at is None:
            return False
        return datetime.now(UTC) >= self.auto_stop_at

    def remaining_seconds(self) -> float:
        """Seconds until auto_stop_at, or 0.0 if indefinite/manual/expired."""
        if self.auto_stop_at is None:
            return 0.0
        remaining = (self.auto_stop_at - datetime.now(UTC)).total_seconds()
        return max(0.0, remaining)

    def remaining_str(self) -> str:
        """Human-readable remaining time. Empty string if indefinite/manual."""
        if self.auto_stop_at is None:
            return ""
        secs = int(self.remaining_seconds())
        if secs <= 0:
            return ""
        h, remainder = divmod(secs, 3600)
        m, s = divmod(remainder, 60)
        return f"{h}:{m:02d}:{s:02d}"

    def elapsed_str(self) -> str:
        """Human-readable elapsed time since entering automatic mode."""
        if self.started_at is None:
            return "0:00:00"
        elapsed = (datetime.now(UTC) - self.started_at).total_seconds()
        secs = int(max(0, elapsed))
        h, remainder = divmod(secs, 3600)
        m, s = divmod(remainder, 60)
        return f"{h}:{m:02d}:{s:02d}"
