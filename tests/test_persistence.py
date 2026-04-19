"""Tests for game persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from talos.persistence import (
    GAMES_FULL_SCHEMA_VERSION,
    GamesFullCorruptError,
    load_saved_games,
    load_saved_games_full,
    save_games,
    save_games_full,
)


class TestSaveGames:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "games.json"
        save_games(["EVT-1", "EVT-2"], path=path)
        assert load_saved_games(path=path) == ["EVT-1", "EVT-2"]

    def test_save_empty_list(self, tmp_path: Path) -> None:
        path = tmp_path / "games.json"
        save_games([], path=path)
        assert load_saved_games(path=path) == []

    def test_save_overwrites(self, tmp_path: Path) -> None:
        path = tmp_path / "games.json"
        save_games(["EVT-1"], path=path)
        save_games(["EVT-2", "EVT-3"], path=path)
        assert load_saved_games(path=path) == ["EVT-2", "EVT-3"]


class TestLoadGames:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "nonexistent.json"
        assert load_saved_games(path=path) == []

    def test_corrupt_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "games.json"
        path.write_text("not json{{{")
        assert load_saved_games(path=path) == []

    def test_non_list_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "games.json"
        path.write_text('{"key": "value"}')
        assert load_saved_games(path=path) == []

    def test_filters_non_string_items(self, tmp_path: Path) -> None:
        path = tmp_path / "games.json"
        path.write_text('["EVT-1", 42, null, "EVT-2"]')
        assert load_saved_games(path=path) == ["EVT-1", "EVT-2"]


class TestGamesFullSchemaAndDurability:
    """Round-4 fixes: atomic writes, schema versioning, fail-closed loader."""

    def _record(self, **overrides: object) -> dict[str, str | float | None]:
        base: dict[str, str | float | None] = {
            "event_ticker": "K",
            "ticker_a": "K",
            "ticker_b": "K",
            "side_a": "yes",
            "side_b": "no",
            "engine_state": "winding_down",
            "source": "tree",
        }
        base.update(overrides)  # type: ignore[arg-type]
        return base

    def test_save_writes_versioned_envelope(self, tmp_path: Path) -> None:
        path = tmp_path / "games_full.json"
        save_games_full([self._record()], path=path)
        raw = json.loads(path.read_text())
        assert isinstance(raw, dict)
        assert raw["schema_version"] == GAMES_FULL_SCHEMA_VERSION
        assert isinstance(raw["games"], list)
        assert raw["games"][0]["engine_state"] == "winding_down"

    def test_roundtrip_returns_games_list(self, tmp_path: Path) -> None:
        path = tmp_path / "games_full.json"
        save_games_full([self._record()], path=path)
        loaded = load_saved_games_full(path=path)
        assert loaded is not None
        assert len(loaded) == 1
        assert loaded[0]["engine_state"] == "winding_down"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_saved_games_full(path=tmp_path / "nope.json") is None

    def test_unparseable_raises_fail_closed(self, tmp_path: Path) -> None:
        """Garbage JSON must NOT silently fall back to legacy ticker file —
        callers need to see corruption to refuse start."""
        path = tmp_path / "games_full.json"
        path.write_text("not json{{{")
        with pytest.raises(GamesFullCorruptError):
            load_saved_games_full(path=path)

    def test_v1_missing_engine_state_raises(self, tmp_path: Path) -> None:
        """A v1 save without engine_state on a game entry is corrupt — we
        can't infer winding_down vs active and silently defaulting would
        resurrect a winding-down pair as freely tradable."""
        path = tmp_path / "games_full.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "games": [{"event_ticker": "K", "ticker_a": "K", "ticker_b": "K"}],
                }
            )
        )
        with pytest.raises(GamesFullCorruptError, match="engine_state"):
            load_saved_games_full(path=path)

    def test_future_schema_version_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "games_full.json"
        path.write_text(
            json.dumps({"schema_version": 999, "games": []})
        )
        with pytest.raises(GamesFullCorruptError, match="newer"):
            load_saved_games_full(path=path)

    def test_legacy_bare_list_auto_migrates(self, tmp_path: Path) -> None:
        """v0 saves predate engine_state; load must accept and stamp the
        field as 'active' (safe — v0 had no winding_down concept)."""
        path = tmp_path / "games_full.json"
        path.write_text(
            json.dumps([{"event_ticker": "K", "ticker_a": "K", "ticker_b": "K"}])
        )
        loaded = load_saved_games_full(path=path)
        assert loaded is not None
        assert loaded[0]["engine_state"] == "active"

    def test_atomic_write_does_not_corrupt_on_concurrent_read(
        self, tmp_path: Path
    ) -> None:
        """Direct write_text would let a concurrent reader see a half-written
        file. Atomic write via temp+os.replace eliminates that window — the
        file at the destination path is always the previous complete version
        until the replace, then atomically the new complete version."""
        path = tmp_path / "games_full.json"
        save_games_full([self._record(event_ticker="A")], path=path)
        first = path.read_text()
        save_games_full([self._record(event_ticker="B")], path=path)
        # No temp file lying around with .tmp suffix
        leftover_tmps = list(tmp_path.glob("games_full.json.*"))
        assert leftover_tmps == [], f"orphan temps: {leftover_tmps}"
        # Second save is fully present, not partial.
        second = json.loads(path.read_text())
        assert second["games"][0]["event_ticker"] == "B"
        assert first != path.read_text()
