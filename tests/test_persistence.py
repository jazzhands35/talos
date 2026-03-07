"""Tests for game persistence."""

from __future__ import annotations

from pathlib import Path

from talos.persistence import load_saved_games, save_games


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
