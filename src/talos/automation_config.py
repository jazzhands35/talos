from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AutomationConfig:
    """Settings for the proposal system. Off by default."""

    edge_threshold_cents: float = 1.0
    stability_seconds: float = 5.0
    staleness_grace_seconds: float = 5.0
    rejection_cooldown_seconds: float = 30.0
    unit_size: int = 10
    enabled: bool = True
