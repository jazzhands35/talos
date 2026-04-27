"""Tests the low-level load_tree_metadata / save_tree_metadata JSON I/O: defaults when missing,
roundtrip, corrupt-file recovery, partial-file forward-compat backfilling.
"""

import json
from pathlib import Path

from talos.persistence import load_tree_metadata, save_tree_metadata


def test_load_returns_empty_default_when_missing(tmp_path: Path):
    data = load_tree_metadata(path=tmp_path / "tree_metadata.json")
    assert data == {
        "version": 1,
        "event_first_seen": {},
        "event_reviewed_at": {},
        "manual_event_start": {},
        "deliberately_unticked": [],
        "deliberately_unticked_pending": [],
    }


def test_save_and_load_roundtrip(tmp_path: Path):
    original: dict[str, object] = {
        "version": 1,
        "event_first_seen": {"K-1": "2026-04-16T18:32:00Z"},
        "event_reviewed_at": {"K-1": "2026-04-16T19:00:00Z"},
        "manual_event_start": {"K-2": "2026-04-22T20:00:00-04:00"},
        "deliberately_unticked": ["K-3"],
        "deliberately_unticked_pending": ["K-4"],
    }
    save_tree_metadata(original, path=tmp_path / "tree_metadata.json")
    loaded = load_tree_metadata(path=tmp_path / "tree_metadata.json")
    assert loaded == original


def test_load_corrupt_file_returns_defaults(tmp_path: Path):
    f = tmp_path / "tree_metadata.json"
    f.write_text("{broken json")
    data = load_tree_metadata(path=f)
    assert data["version"] == 1
    assert data["deliberately_unticked"] == []


def test_load_partial_file_backfills_missing_keys(tmp_path: Path):
    """Forward-compat: older files missing a key must still load cleanly
    with defaults backfilled."""
    f = tmp_path / "tree_metadata.json"
    f.write_text(
        json.dumps(
            {
                "version": 1,
                "event_first_seen": {"K-1": "2026-04-16T00:00:00Z"},
                # Missing: event_reviewed_at, manual_event_start, deliberately_unticked*
            }
        )
    )
    data = load_tree_metadata(path=f)
    assert data["event_first_seen"] == {"K-1": "2026-04-16T00:00:00Z"}
    assert data["event_reviewed_at"] == {}
    assert data["manual_event_start"] == {}
    assert data["deliberately_unticked"] == []
    assert data["deliberately_unticked_pending"] == []
