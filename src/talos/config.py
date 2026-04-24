"""Kalshi API environment configuration."""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

from pydantic import BaseModel


class KalshiEnvironment(Enum):
    """Kalshi API environment — demo by default, production opt-in."""

    DEMO = "demo"
    PRODUCTION = "production"


_URLS: dict[KalshiEnvironment, tuple[str, str]] = {
    KalshiEnvironment.DEMO: (
        "https://demo-api.kalshi.co/trade-api/v2",
        "wss://demo-api.kalshi.co/trade-api/ws/v2",
    ),
    KalshiEnvironment.PRODUCTION: (
        "https://api.elections.kalshi.com/trade-api/v2",
        "wss://api.elections.kalshi.com/trade-api/ws/v2",
    ),
}


class KalshiConfig(BaseModel):
    """Immutable Kalshi API configuration."""

    model_config = {"frozen": True}

    environment: KalshiEnvironment
    key_id: str
    private_key_path: Path
    rest_base_url: str
    ws_url: str

    @classmethod
    def from_env(cls) -> KalshiConfig:
        """Load configuration from environment variables.

        Required env vars:
            KALSHI_KEY_ID: API key identifier
            KALSHI_PRIVATE_KEY_PATH: Path to RSA private key PEM file

        Optional env vars:
            KALSHI_ENV: "demo" (default) or "production"
        """
        key_id = os.environ.get("KALSHI_KEY_ID")
        if not key_id:
            msg = "KALSHI_KEY_ID environment variable is required"
            raise ValueError(msg)

        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if not key_path:
            msg = "KALSHI_PRIVATE_KEY_PATH environment variable is required"
            raise ValueError(msg)

        env_str = os.environ.get("KALSHI_ENV", "demo")
        try:
            environment = KalshiEnvironment(env_str)
        except ValueError:
            msg = f"KALSHI_ENV must be 'demo' or 'production', got '{env_str}'"
            raise ValueError(msg) from None

        rest_url, ws_url = _URLS[environment]

        return cls(
            environment=environment,
            key_id=key_id,
            private_key_path=Path(key_path),
            rest_base_url=rest_url,
            ws_url=ws_url,
        )
