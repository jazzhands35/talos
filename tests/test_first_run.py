"""Tests for first-run setup screen."""

from __future__ import annotations

import json
from pathlib import Path

from talos.ui.first_run import write_default_settings, write_env_file


class TestWriteEnvFile:
    """write_env_file creates a valid .env file."""

    def test_writes_production_env(self) -> None:
        env_path = Path("tests") / ".tmp_first_run_env"
        env_path.unlink(missing_ok=True)
        try:
            write_env_file(
                env_path,
                key_id="abc-123",
                key_path=r"C:\Users\test\kalshi.key",
            )
            content = env_path.read_text()
            assert "KALSHI_KEY_ID=abc-123" in content
            assert r"KALSHI_PRIVATE_KEY_PATH=C:\Users\test\kalshi.key" in content
            assert "KALSHI_ENV=production" in content
        finally:
            env_path.unlink(missing_ok=True)

    def test_overwrites_existing(self) -> None:
        env_path = Path("tests") / ".tmp_first_run_env"
        env_path.unlink(missing_ok=True)
        try:
            env_path.write_text("OLD=stuff\n")
            write_env_file(env_path, key_id="new", key_path="/new/path")
            content = env_path.read_text()
            assert "OLD" not in content
            assert "KALSHI_KEY_ID=new" in content
        finally:
            env_path.unlink(missing_ok=True)


class TestWriteDefaultSettings:
    """write_default_settings creates settings.json with correct defaults."""

    def test_writes_unit_size_5(self) -> None:
        settings_path = Path("tests") / ".tmp_first_run_settings.json"
        settings_path.unlink(missing_ok=True)
        try:
            write_default_settings(settings_path)
            data = json.loads(settings_path.read_text())
            assert data["unit_size"] == 5
            assert data["ticker_blacklist"] == []
            assert data["execution_mode"] == "automatic"
            assert data["auto_stop_hours"] is None
        finally:
            settings_path.unlink(missing_ok=True)
