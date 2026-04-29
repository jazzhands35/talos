"""Test the migration logic with an in-memory sqlite + synthetic JSON."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.migrate_talos_ids import migrate

from talos.talos_id import ensure_counter_schema, peek_seq


def _seed_game_adds(conn: sqlite3.Connection, rows: list[tuple[str, str]]) -> None:
    conn.execute(
        "CREATE TABLE game_adds (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts TEXT NOT NULL, event_ticker TEXT)"
    )
    for ts, ticker in rows:
        conn.execute(
            "INSERT INTO game_adds(ts, event_ticker) VALUES (?, ?)", (ts, ticker)
        )
    conn.commit()


def test_migrate_assigns_chronological_ids(tmp_path: Path) -> None:
    db = sqlite3.connect(":memory:")
    ensure_counter_schema(db)
    _seed_game_adds(db, [
        ("2026-04-10T12:00:00+00:00", "EVT-A"),
        ("2026-04-15T12:00:00+00:00", "EVT-B"),
        ("2026-04-15T13:00:00+00:00", "EVT-A"),  # duplicate add — earlier wins
        ("2026-04-20T12:00:00+00:00", "EVT-C"),
    ])
    games_json = tmp_path / "games_full.json"
    games_json.write_text(json.dumps({
        "schema_version": 1,
        "games": [
            {"event_ticker": "EVT-A", "talos_id": 0, "ticker_a": "x", "ticker_b": "y"},
            {"event_ticker": "EVT-B", "talos_id": 0, "ticker_a": "x", "ticker_b": "y"},
            {"event_ticker": "EVT-C", "talos_id": 0, "ticker_a": "x", "ticker_b": "y"},
        ],
    }))

    migrate(db=db, games_path=games_json)

    after = json.loads(games_json.read_text())
    by_ticker = {g["event_ticker"]: g["talos_id"] for g in after["games"]}
    assert by_ticker["EVT-A"] == 2604001  # earliest add
    assert by_ticker["EVT-B"] == 2604002
    assert by_ticker["EVT-C"] == 2604003
    # Counter is bumped so post-migration adds start at 004
    assert peek_seq(db, year=2026, month=4) == 3


def test_migrate_skips_already_assigned(tmp_path: Path) -> None:
    db = sqlite3.connect(":memory:")
    ensure_counter_schema(db)
    _seed_game_adds(db, [("2026-04-10T12:00:00+00:00", "EVT-A")])
    games_json = tmp_path / "games_full.json"
    games_json.write_text(json.dumps({
        "schema_version": 1,
        "games": [
            {"event_ticker": "EVT-A", "talos_id": 2604042, "ticker_a": "x", "ticker_b": "y"},
        ],
    }))
    migrate(db=db, games_path=games_json)
    after = json.loads(games_json.read_text())
    assert after["games"][0]["talos_id"] == 2604042  # untouched


def test_migrate_handles_pair_not_in_game_adds(tmp_path: Path) -> None:
    """If an event_ticker has no row in game_adds, fall back to current local month."""
    db = sqlite3.connect(":memory:")
    ensure_counter_schema(db)
    _seed_game_adds(db, [])  # empty
    games_json = tmp_path / "games_full.json"
    games_json.write_text(json.dumps({
        "schema_version": 1,
        "games": [
            {"event_ticker": "ORPHAN", "talos_id": 0, "ticker_a": "x", "ticker_b": "y"},
        ],
    }))
    migrate(db=db, games_path=games_json)
    after = json.loads(games_json.read_text())
    assert after["games"][0]["talos_id"] != 0  # got *something* current-month-ish


def test_migrate_spans_multiple_months(tmp_path: Path) -> None:
    """Pairs added in different months get their respective month's seq starting from 001."""
    db = sqlite3.connect(":memory:")
    ensure_counter_schema(db)
    _seed_game_adds(db, [
        ("2026-03-15T12:00:00+00:00", "MAR-EVT"),
        ("2026-04-10T12:00:00+00:00", "APR-EVT-1"),
        ("2026-04-20T12:00:00+00:00", "APR-EVT-2"),
    ])
    games_json = tmp_path / "games_full.json"
    games_json.write_text(json.dumps({
        "schema_version": 1,
        "games": [
            {"event_ticker": "MAR-EVT", "talos_id": 0, "ticker_a": "x", "ticker_b": "y"},
            {"event_ticker": "APR-EVT-1", "talos_id": 0, "ticker_a": "x", "ticker_b": "y"},
            {"event_ticker": "APR-EVT-2", "talos_id": 0, "ticker_a": "x", "ticker_b": "y"},
        ],
    }))
    migrate(db=db, games_path=games_json)
    after = json.loads(games_json.read_text())
    by_ticker = {g["event_ticker"]: g["talos_id"] for g in after["games"]}
    assert by_ticker["MAR-EVT"] == 2603001
    assert by_ticker["APR-EVT-1"] == 2604001
    assert by_ticker["APR-EVT-2"] == 2604002
