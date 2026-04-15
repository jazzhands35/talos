"""Tests for JSONL auto-accept session logger."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import talos.auto_accept_log as auto_accept_log_module
from talos.auto_accept import ExecutionMode
from talos.auto_accept_log import AutoAcceptLogger
from talos.models.proposal import Proposal, ProposalKey, ProposedBid


def _make_proposal() -> Proposal:
    return Proposal(
        key=ProposalKey(event_ticker="TENN-A", side="", kind="bid"),
        kind="bid",
        summary="Bid TENN-A @ 45/55 NO",
        detail="2.5c edge, qty 10",
        created_at=datetime.now(UTC),
        bid=ProposedBid(
            event_ticker="TENN-A",
            ticker_a="TENN-A-T1",
            ticker_b="TENN-A-T2",
            no_a=45,
            no_b=55,
            qty=10,
            edge_cents=2.5,
            stable_for_seconds=5.0,
            reason="edge above threshold",
        ),
    )


def _fixed_log_file(monkeypatch, *, second: int) -> Path:
    fixed = datetime(2026, 4, 3, 17, 45, second, tzinfo=UTC)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(auto_accept_log_module, "datetime", _FixedDateTime)
    path = Path("tests") / fixed.strftime("%Y-%m-%d_%H%M%S.jsonl")
    path.unlink(missing_ok=True)
    return path


def test_session_start_writes_jsonl(monkeypatch) -> None:
    log_file = _fixed_log_file(monkeypatch, second=1)
    logger = AutoAcceptLogger(Path("tests"))
    state = ExecutionMode()
    state.enter_automatic(hours=2.0)
    config = {"edge_threshold_cents": 1.0, "unit_size": 10}

    try:
        logger.log_session_start(state, config)
        line = json.loads(log_file.read_text().strip())
        assert line["event"] == "session_start"
        assert line["config"]["unit_size"] == 10
        assert "duration_hours" in line
    finally:
        log_file.unlink(missing_ok=True)


def test_log_accepted_writes_state_snapshot(monkeypatch) -> None:
    log_file = _fixed_log_file(monkeypatch, second=2)
    logger = AutoAcceptLogger(Path("tests"))
    state = ExecutionMode()
    state.enter_automatic(hours=1.0)

    proposal = _make_proposal()
    snapshot = {
        "positions": {},
        "balance_cents": 50000,
        "resting_orders": [],
        "top_of_market": {},
    }

    try:
        logger.log_session_start(state, {})
        logger.log_accepted(proposal, snapshot, state)
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2
        entry = json.loads(lines[1])
        assert entry["event"] == "auto_accepted"
        assert entry["proposal"]["kind"] == "bid"
        assert entry["state_snapshot"]["balance_cents"] == 50000
        assert "session" in entry
    finally:
        log_file.unlink(missing_ok=True)


def test_log_session_end(monkeypatch) -> None:
    log_file = _fixed_log_file(monkeypatch, second=3)
    logger = AutoAcceptLogger(Path("tests"))
    state = ExecutionMode()
    state.enter_automatic(hours=1.0)
    state.accepted_count = 5

    try:
        logger.log_session_start(state, {})
        logger.log_session_end(state, final_positions={})
        lines = log_file.read_text().strip().split("\n")
        last = json.loads(lines[-1])
        assert last["event"] == "session_end"
        assert last["total_accepted"] == 5
    finally:
        log_file.unlink(missing_ok=True)


def test_log_error(monkeypatch) -> None:
    log_file = _fixed_log_file(monkeypatch, second=4)
    logger = AutoAcceptLogger(Path("tests"))
    state = ExecutionMode()
    state.enter_automatic(hours=1.0)

    proposal = _make_proposal()

    try:
        logger.log_session_start(state, {})
        logger.log_error(proposal, "API timeout", {"balance_cents": 50000}, state)
        lines = log_file.read_text().strip().split("\n")
        last = json.loads(lines[-1])
        assert last["event"] == "auto_accept_error"
        assert last["error"] == "API timeout"
    finally:
        log_file.unlink(missing_ok=True)
