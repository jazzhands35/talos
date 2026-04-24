from __future__ import annotations

from dataclasses import dataclass

# Single authoritative default for unit_size across the codebase.
# Startup reads from settings.json; this is the fallback when no setting exists.
# All constructor defaults in BidAdjuster, PositionLedger, etc. should use this.
DEFAULT_UNIT_SIZE: int = 5


@dataclass
class AutomationConfig:
    """Settings for the proposal system. Off by default."""

    edge_threshold_cents: float = 1.0
    stability_seconds: float = 5.0
    staleness_grace_seconds: float = 5.0
    rejection_cooldown_seconds: float = 30.0
    placement_failure_cooldown_seconds: float = 120.0
    enabled: bool = True
    exit_only_minutes: float = 30.0
    sports_enabled: bool = True

    # Tree-mode feature flag — all new behavior gated on this.
    # Defaults on: the feature has stabilized through the scanner-tree-redesign
    # branch and is the intended production experience. Tests that need
    # legacy behavior can still pass `tree_mode=False` explicitly.
    tree_mode: bool = True

    # Startup gate — max wait for milestones before engine begins tick loop.
    startup_milestone_wait_seconds: float = 30.0

    # Schedule conflict threshold — delta between manual override and Kalshi
    # milestone that triggers a user-resolved conflict prompt.
    schedule_conflict_threshold_minutes: float = 5.0

    # DiscoveryService semaphore — max concurrent discovery Kalshi calls.
    discovery_concurrent_limit: int = 5

    # Background milestone refresh interval.
    milestone_refresh_seconds: float = 300.0
