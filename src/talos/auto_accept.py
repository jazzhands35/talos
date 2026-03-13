"""Auto-accept mode state management."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass
class AutoAcceptState:
    """Tracks whether auto-accept is active, for how long, and how many accepted."""

    active: bool = False
    started_at: datetime | None = None
    duration: timedelta | None = None
    accepted_count: int = 0

    def start(self, hours: float) -> None:
        """Activate auto-accept for the given duration."""
        self.active = True
        self.started_at = datetime.now(UTC)
        self.duration = timedelta(hours=hours)
        self.accepted_count = 0

    def stop(self) -> None:
        """Deactivate auto-accept."""
        self.active = False

    def is_expired(self) -> bool:
        """True if the duration has elapsed."""
        if not self.active or self.started_at is None or self.duration is None:
            return False
        return datetime.now(UTC) >= self.started_at + self.duration

    def remaining_seconds(self) -> float:
        """Seconds remaining, or 0.0 if inactive/expired."""
        if not self.active or self.started_at is None or self.duration is None:
            return 0.0
        end = self.started_at + self.duration
        remaining = (end - datetime.now(UTC)).total_seconds()
        return max(0.0, remaining)

    def remaining_str(self) -> str:
        """Human-readable remaining time, e.g. '1:23:45'."""
        secs = int(self.remaining_seconds())
        h, remainder = divmod(secs, 3600)
        m, s = divmod(remainder, 60)
        return f"{h}:{m:02d}:{s:02d}"

    def elapsed_str(self) -> str:
        """Human-readable elapsed time since start."""
        if self.started_at is None:
            return "0:00:00"
        elapsed = (datetime.now(UTC) - self.started_at).total_seconds()
        secs = int(max(0, elapsed))
        h, remainder = divmod(secs, 3600)
        m, s = divmod(remainder, 60)
        return f"{h}:{m:02d}:{s:02d}"
