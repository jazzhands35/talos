"""Tests for configurable data directory in persistence module."""

from __future__ import annotations

from pathlib import Path

import pytest

from talos.persistence import get_data_dir, set_data_dir


class TestGetDataDir:
    """get_data_dir returns the correct path based on configuration."""

    def teardown_method(self) -> None:
        """Reset data dir after each test."""
        set_data_dir(None)

    def test_default_returns_project_root(self) -> None:
        set_data_dir(None)
        result = get_data_dir()
        assert result.name != ""
        assert result.is_dir()

    def test_set_data_dir_overrides(self, tmp_path: Path) -> None:
        set_data_dir(tmp_path)
        assert get_data_dir() == tmp_path

    def test_set_data_dir_none_resets(self, tmp_path: Path) -> None:
        set_data_dir(tmp_path)
        assert get_data_dir() == tmp_path
        set_data_dir(None)
        assert get_data_dir() != tmp_path


class TestPathFunctions:
    """File-path functions resolve against get_data_dir."""

    def teardown_method(self) -> None:
        set_data_dir(None)

    def test_load_settings_uses_data_dir(self, tmp_path: Path) -> None:
        import json
        set_data_dir(tmp_path)
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"unit_size": 42}))
        from talos.persistence import load_settings
        result = load_settings()
        assert result["unit_size"] == 42

    def test_save_settings_uses_data_dir(self, tmp_path: Path) -> None:
        import json
        set_data_dir(tmp_path)
        from talos.persistence import save_settings
        save_settings({"unit_size": 7})
        result = json.loads((tmp_path / "settings.json").read_text())
        assert result["unit_size"] == 7

    def test_load_saved_games_uses_data_dir(self, tmp_path: Path) -> None:
        import json
        set_data_dir(tmp_path)
        (tmp_path / "games.json").write_text(json.dumps(["EVT-1", "EVT-2"]))
        from talos.persistence import load_saved_games
        assert load_saved_games() == ["EVT-1", "EVT-2"]

    def test_save_games_uses_data_dir(self, tmp_path: Path) -> None:
        import json
        set_data_dir(tmp_path)
        from talos.persistence import save_games
        save_games(["A", "B"])
        result = json.loads((tmp_path / "games.json").read_text())
        assert result == ["A", "B"]
