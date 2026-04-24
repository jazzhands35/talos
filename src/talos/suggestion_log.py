"""Append-only human-readable log of all proposal lifecycle events.

Every proposal that enters the system gets a timestamped entry when it is
proposed, approved, rejected, superseded, or expired. This is the audit
trail for automation decisions — distinct from structlog (debug-level,
noisy) and the UI (ephemeral).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from talos.models.proposal import Proposal


def format_entry(action: str, proposal: Proposal, timestamp: datetime | None = None) -> str:
    """Format a single log entry as human-readable text."""
    ts = timestamp or datetime.now(UTC)
    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")

    key = proposal.key
    side_str = f"  side {key.side}" if key.side else ""

    header = f"[{ts_str}] {action:<11} {key.kind:<12} {key.event_ticker}{side_str}"

    lines = [header]
    if action in ("PROPOSED", "SUPERSEDED"):
        lines.append(f"  {proposal.summary}")
        if proposal.detail:
            lines.append(f"  {proposal.detail}")
    elif action == "APPROVED":
        lines.append(f"  {proposal.summary}")

    return "\n".join(lines)


class SuggestionLog:
    """Appends proposal lifecycle events to a log file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def log(self, action: str, proposal: Proposal, timestamp: datetime | None = None) -> None:
        """Append a formatted entry to the log file."""
        entry = format_entry(action, proposal, timestamp)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(entry + "\n\n")
