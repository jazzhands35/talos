"""Tree-UI and discovery-layer data models.

Pure data containers. No behavior beyond Pydantic validation. Shared between
TreeScreen, SelectionStore, Engine, DiscoveryService, and MilestoneResolver.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# ── Commit-path DTOs ────────────────────────────────────────────────────


class ArbPairRecord(BaseModel):
    """What TreeScreen stages and hands to Engine.add_pairs_from_selection.

    Field shape intentionally matches games_full.json record shape so the
    same dict can feed GameManager.restore_game() directly.
    """

    # Pair identity — matches ArbPair
    event_ticker: str
    ticker_a: str
    ticker_b: str
    side_a: str = "yes"
    side_b: str = "no"

    # Event grouping
    kalshi_event_ticker: str
    series_ticker: str
    category: str

    # Fee metadata (hydrated from DiscoveryService at commit time)
    fee_type: str = "quadratic_with_maker_fees"
    fee_rate: float = 0.0175

    # Timing hints
    close_time: str | None = None
    expected_expiration_time: str | None = None

    # Display
    sub_title: str = ""
    label: str = ""

    # Tree-specific
    source: str = "tree"
    selected_at: str | None = None

    # For non-sports multi-market events: if null, all active markets selected;
    # otherwise, list of specific market tickers.
    markets: list[str] | None = None

    # 24h volume seeded from discovery cache — avoids zero-volume problem
    # described in Codex round 5 P2.
    volume_24h_a: int | None = None
    volume_24h_b: int | None = None


class RemoveOutcome(BaseModel):
    """Per-pair outcome from Engine.remove_pairs_from_selection."""

    pair_ticker: str
    kalshi_event_ticker: str
    status: Literal["removed", "winding_down", "not_found", "failed"]
    reason: str | None = None


class StagedChanges(BaseModel):
    """In-memory staged tree edits held by TreeScreen until commit."""

    to_add: list[ArbPairRecord] = Field(default_factory=list)
    to_remove: list[str] = Field(default_factory=list)
    to_set_unticked: list[str] = Field(default_factory=list)
    to_clear_unticked: list[str] = Field(default_factory=list)
    to_set_manual_start: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def empty(cls) -> StagedChanges:
        return cls()

    def is_empty(self) -> bool:
        return not (
            self.to_add
            or self.to_remove
            or self.to_set_unticked
            or self.to_clear_unticked
            or self.to_set_manual_start
        )


# ── Milestones ──────────────────────────────────────────────────────────


class Milestone(BaseModel):
    """Kalshi milestone record from /milestones endpoint."""

    id: str
    category: str
    type: str  # one_off_milestone, fomc_meeting, basketball_game, ...
    start_date: datetime
    end_date: datetime
    title: str
    related_event_tickers: list[str]
    notification_message: str = ""


# ── Discovery cache models ──────────────────────────────────────────────


class MarketNode(BaseModel):
    """A single Kalshi market (YES/NO instrument) — discovery cache entry."""

    ticker: str
    title: str
    yes_bid: int | None = None
    yes_ask: int | None = None
    volume_24h: int = 0
    open_interest: int = 0
    status: str = "active"
    close_time: datetime | None = None
    # Kalshi's projected settlement time. For continuous events (hurricane
    # counts, commodity close prices, annual outcomes), this is meaningful
    # timing data we can use as the safety-gate default at commit time,
    # since there is no discrete "event-start" for such markets.
    expected_expiration_time: datetime | None = None


class EventNode(BaseModel):
    """A single Kalshi event — contains one or more MarketNodes."""

    ticker: str
    series_ticker: str
    title: str
    sub_title: str = ""
    close_time: datetime | None = None
    # Mirrored from the first active market when the event payload omits it
    # (common for multi-market events where timing is per-market). Used as
    # the SchedulePopup default when no milestone is available.
    expected_expiration_time: datetime | None = None
    milestone: Milestone | None = None
    markets: list[MarketNode] = Field(default_factory=list)
    fetched_at: datetime | None = None


class SeriesNode(BaseModel):
    """A Kalshi series — container for its events."""

    ticker: str
    title: str
    category: str
    tags: list[str] = Field(default_factory=list)
    # Name of the first entry in /series.settlement_sources. 100% populated
    # across all ~9,700 series; used as a grouping fallback for the tree UI
    # when tag coverage is too sparse (e.g. Social, World).
    primary_source: str = ""
    frequency: str = "custom"
    fee_type: str = "quadratic_with_maker_fees"
    fee_multiplier: float = 1.0
    # Count of currently-open events in this series, populated at bootstrap
    # from a single bulk /events?status=open fetch. None until bootstrap has
    # populated it; 0 means known-empty (don't drill in); positive means
    # there's something to see.
    event_count: int | None = None
    # events: None means "not fetched yet"; {} means "fetched and empty"
    events: dict[str, EventNode] | None = None
    events_loaded_at: datetime | None = None


class CategoryNode(BaseModel):
    """A Kalshi category — top of the discovery tree."""

    name: str
    series_count: int
    series: dict[str, SeriesNode] = Field(default_factory=dict)
