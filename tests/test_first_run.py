"""Tests for first-run setup screen."""

from __future__ import annotations

import json
from pathlib import Path

from talos.ui.first_run import write_env_file, write_default_settings


class TestWriteEnvFile:
    """write_env_file creates a valid .env file."""

    def test_writes_production_env(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        write_env_file(
            env_path,
            key_id="abc-123",
            key_path=r"C:\Users\test\kalshi.key",
        )
        content = env_path.read_text()
        assert "KALSHI_KEY_ID=abc-123" in content
        assert r"KALSHI_PRIVATE_KEY_PATH=C:\Users\test\kalshi.key" in content
        assert "KALSHI_ENV=production" in content

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text("OLD=stuff\n")
        write_env_file(env_path, key_id="new", key_path="/new/path")
        content = env_path.read_text()
        assert "OLD" not in content
        assert "KALSHI_KEY_ID=new" in content


class TestWriteDefaultSettings:
    """write_default_settings creates settings.json with correct defaults."""

    def test_writes_unit_size_5(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        write_default_settings(settings_path)
        data = json.loads(settings_path.read_text())
        assert data["unit_size"] == 5
        assert data["ticker_blacklist"] == []
