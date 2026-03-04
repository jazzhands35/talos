"""Tests for Kalshi environment configuration."""

from pathlib import Path

import pytest

from talos.config import KalshiConfig, KalshiEnvironment


class TestKalshiEnvironment:
    def test_demo_is_default(self) -> None:
        assert KalshiEnvironment.DEMO.value == "demo"

    def test_production_value(self) -> None:
        assert KalshiEnvironment.PRODUCTION.value == "production"


class TestKalshiConfig:
    def test_demo_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/tmp/test.pem")
        monkeypatch.delenv("KALSHI_ENV", raising=False)

        config = KalshiConfig.from_env()

        assert config.environment == KalshiEnvironment.DEMO
        assert config.key_id == "test-key-id"
        assert config.private_key_path == Path("/tmp/test.pem")
        assert "demo-api.kalshi.co" in config.rest_base_url
        assert "demo-api.kalshi.co" in config.ws_url

    def test_production_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KALSHI_KEY_ID", "prod-key")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/tmp/prod.pem")
        monkeypatch.setenv("KALSHI_ENV", "production")

        config = KalshiConfig.from_env()

        assert config.environment == KalshiEnvironment.PRODUCTION
        assert "api.elections.kalshi.com" in config.rest_base_url
        assert "api.elections.kalshi.com" in config.ws_url

    def test_missing_key_id_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KALSHI_KEY_ID", raising=False)
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/tmp/test.pem")

        with pytest.raises(ValueError, match="KALSHI_KEY_ID"):
            KalshiConfig.from_env()

    def test_missing_key_path_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KALSHI_KEY_ID", "test-key")
        monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)

        with pytest.raises(ValueError, match="KALSHI_PRIVATE_KEY_PATH"):
            KalshiConfig.from_env()

    def test_invalid_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KALSHI_KEY_ID", "test-key")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/tmp/test.pem")
        monkeypatch.setenv("KALSHI_ENV", "staging")

        with pytest.raises(ValueError, match="KALSHI_ENV"):
            KalshiConfig.from_env()
