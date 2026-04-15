"""JSONL session logger for auto-accept mode."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from talos.auto_accept import ExecutionMode
    from talos.models.proposal import Proposal


class AutoAcceptLogger:
    """Writes JSONL logs -- one file per auto-accept session."""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._current_file: Path | None = None

    def log_session_start(self, state: ExecutionMode, config: dict[str, Any]) -> None:
        """Create session file and write the start event."""
        self._log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC)
        filename = ts.strftime("%Y-%m-%d_%H%M%S") + ".jsonl"
        self._current_file = self._log_dir / filename
        if state.auto_stop_at and state.started_at:
            elapsed = (state.auto_stop_at - state.started_at).total_seconds()
            duration_hours: float | None = elapsed / 3600
        else:
            duration_hours = None  # indefinite
        self._write(
            {
                "timestamp": ts.isoformat(),
                "event": "session_start",
                "config": config,
                "duration_hours": duration_hours,
                "mode": state.mode.value,
            }
        )

    def log_accepted(
        self,
        proposal: Proposal,
        state_snapshot: dict[str, Any],
        state: ExecutionMode,
    ) -> None:
        """Log an auto-accepted proposal with full state snapshot."""
        self._write(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": "auto_accepted",
                "proposal": {
                    "kind": proposal.kind,
                    "event_ticker": proposal.key.event_ticker,
                    "side": proposal.key.side,
                    "summary": proposal.summary,
                    "detail": proposal.detail,
                },
                "state_snapshot": state_snapshot,
                "session": {
                    "started_at": (state.started_at.isoformat() if state.started_at else None),
                    "elapsed": state.elapsed_str(),
                    "accepted_count": state.accepted_count,
                },
            }
        )

    def log_error(
        self,
        proposal: Proposal,
        error: str,
        state_snapshot: dict[str, Any],
        state: ExecutionMode,
    ) -> None:
        """Log an auto-accept failure."""
        self._write(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": "auto_accept_error",
                "proposal": {
                    "kind": proposal.kind,
                    "event_ticker": proposal.key.event_ticker,
                    "summary": proposal.summary,
                },
                "error": error,
                "state_snapshot": state_snapshot,
                "session": {
                    "elapsed": state.elapsed_str(),
                    "accepted_count": state.accepted_count,
                },
            }
        )

    def log_session_end(self, state: ExecutionMode, final_positions: dict[str, Any]) -> None:
        """Write the session end summary."""
        self._write(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": "session_end",
                "total_accepted": state.accepted_count,
                "elapsed": state.elapsed_str(),
                "final_positions": final_positions,
            }
        )

    def _write(self, data: dict[str, Any]) -> None:
        if self._current_file is None:
            return
        with open(self._current_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, default=str) + "\n")
