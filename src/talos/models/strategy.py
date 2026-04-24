"""Pydantic models for arbitrage strategy."""

from __future__ import annotations

from pydantic import BaseModel


class ArbPair(BaseModel):
    """Two mutually exclusive markets within a game event."""

    talos_id: int = 0  # Internal short ID for display/tracking
    event_ticker: str
    ticker_a: str
    ticker_b: str
    side_a: str = "no"  # "yes" or "no" — Kalshi side for leg A
    side_b: str = "no"  # "yes" or "no" — Kalshi side for leg B
    kalshi_event_ticker: str = ""  # Real Kalshi event ticker for API calls
    series_ticker: str = ""  # for volume refresh and category display
    fee_type: str = "quadratic_with_maker_fees"
    fee_rate: float = 0.0175
    close_time: str | None = None
    expected_expiration_time: str | None = None
    source: str | None = None  # "tree" | "manual_url" | "restore" | "migration"
    # Phase 0 admission-guard shape fields (bps/fp100 migration gate).
    # Defaults correspond to "safe cent-only non-fractional", so existing
    # callers that don't pass these fields are unaffected.
    fractional_trading_enabled: bool = False
    tick_bps: int = 100
    engine_state: str = "active"  # "active" | "winding_down" | "exit_only"

    @property
    def is_same_ticker(self) -> bool:
        """True when both legs trade the same market (YES/NO arb)."""
        return self.ticker_a == self.ticker_b

    @property
    def api_event_ticker(self) -> str:
        """Event ticker for Kalshi API calls."""
        return self.kalshi_event_ticker or self.event_ticker


class Opportunity(BaseModel):
    """A detected NO+NO arbitrage opportunity.

    Dual-unit migration (bps/fp100): legacy integer-cents / integer-contract
    fields (``no_a``, ``raw_edge``, ``qty_a``, ...) are shipped alongside
    exact-precision bps / fp100 siblings (``no_a_bps``, ``raw_edge_bps``,
    ``qty_a_fp100``, ...). Scanner populates both; legacy consumers
    (bid_adjuster, opportunity_proposer) still read the cents fields and
    see cents-rounded views of the exact edge. Legacy fields are deleted
    in Task 13 of ``docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md``
    once all callers have migrated to the ``_bps`` / ``_fp100`` siblings.
    """

    event_ticker: str
    ticker_a: str
    ticker_b: str
    # Legacy integer-cents / integer-contract fields (lossy for sub-cent).
    no_a: int
    no_b: int
    qty_a: int
    qty_b: int
    raw_edge: int
    fee_edge: float = 0.0
    tradeable_qty: int
    timestamp: str
    close_time: str | None = None
    fee_rate: float = 0.0175
    # New bps / fp100 fields (exact precision — preferred).
    no_a_bps: int = 0
    no_b_bps: int = 0
    qty_a_fp100: int = 0
    qty_b_fp100: int = 0
    raw_edge_bps: int = 0
    fee_edge_bps: int = 0
    tradeable_qty_fp100: int = 0

    @property
    def cost(self) -> int:
        """Total NO cost per contract in cents."""
        return self.no_a + self.no_b

    @property
    def cost_bps(self) -> int:
        """Total NO cost per contract in bps."""
        return self.no_a_bps + self.no_b_bps


class BidConfirmation(BaseModel):
    """Result from the bid confirmation modal."""

    ticker_a: str
    ticker_b: str
    no_a: int
    no_b: int
    qty: int
