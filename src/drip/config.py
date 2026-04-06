"""Drip run configuration."""

from __future__ import annotations

from pydantic import BaseModel


class DripConfig(BaseModel):
    """Immutable configuration for a single Drip arbitrage run."""

    model_config = {"frozen": True}

    event_ticker: str
    """Kalshi event ticker (e.g. 'KXNHLGAME-26MAR19WPGBOS')."""

    ticker_a: str
    """Market ticker for side A."""

    ticker_b: str
    """Market ticker for side B."""

    price_a: int
    """NO price in cents for side A."""

    price_b: int
    """NO price in cents for side B."""

    max_resting: int = 20
    """Maximum resting orders allowed per side."""

    stagger_delay: float = 5.0
    """Seconds to wait between contract deployments during initial stagger phase."""

    fee_rate: float = 0.0175
    """Maker fee rate (quadratic model)."""
