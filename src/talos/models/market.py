"""Pydantic models for Kalshi market data."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, model_validator

from talos.models._converters import dollars_to_bps as _dollars_to_bps
from talos.models._converters import dollars_to_cents as _dollars_to_cents
from talos.models._converters import fp_to_fp100 as _fp_to_fp100
from talos.models._converters import fp_to_int as _fp_to_int


class OrderBookLevel(BaseModel):
    """A single price level in the orderbook.

    Dual-unit migration (bps/fp100): legacy integer-cents ``price`` and
    integer-contracts ``quantity`` are shipped alongside exact-precision
    siblings ``price_bps`` and ``quantity_fp100``. Wire-parsed levels
    (via :meth:`OrderBook._coerce_levels`) populate both; direct
    construction leaves the new fields at 0 until callers migrate in
    subsequent tasks. Legacy fields are deleted in Task 13 of
    ``docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md``.
    """

    price: int
    quantity: int
    # New bps / fp100 fields (exact precision — preferred).
    price_bps: int = 0
    quantity_fp100: int = 0


class PriceRange(BaseModel, extra="ignore"):
    """One row of Kalshi's market ``price_ranges`` metadata.

    Stored as raw dollar strings because the values may be sub-cent and
    we do not want to round at parse time. Consumers use Decimal math
    to compute bps.
    """

    min_price_dollars: str = "0.01"
    max_price_dollars: str = "0.99"
    tick_dollars: str = "0.01"


class Market(BaseModel):
    """A Kalshi market (contract).

    Dual-unit migration (bps/fp100): each money/count field has both a
    legacy integer-cents / integer-contracts attribute (``yes_bid``,
    ``volume``, ...) and a new exact-precision bps / fp100 sibling
    (``yes_bid_bps``, ``volume_fp100``, ...). Both populate from the
    same ``_dollars`` / ``_fp`` wire payload. Downstream callers migrate
    from the legacy names to the ``_bps`` / ``_fp100`` names
    incrementally; the legacy fields are deleted in Task 13 of
    ``docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md``
    once all callers have migrated.

    Post March 12, 2026: integer cents fields removed from API responses.
    The validator converts _dollars/_fp string fields to int cents/int counts.

    Phase 0 additions (2026-04-21): ``fractional_trading_enabled``,
    ``price_level_structure``, and ``price_ranges`` are shape metadata used
    by ``validate_market_for_admission`` to reject markets whose shape
    would trigger the fractional-count truncation or sub-cent-rounding
    bugs documented in the bps/fp100 migration spec.
    """

    ticker: str
    event_ticker: str
    title: str
    status: str
    # Legacy integer-cents / integer-contract fields (lossy for sub-cent
    # / fractional markets — deprecated, removed in Task 13).
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    volume: int | None = None
    volume_24h: int | None = None
    open_interest: int | None = None
    last_price: int | None = None
    settlement_ts: str | None = None
    close_time: str | None = None
    open_time: str | None = None
    result: str = ""
    market_type: str = "binary"
    expected_expiration_time: str | None = None
    # Phase 0 shape metadata (bps/fp100 migration admission guard).
    fractional_trading_enabled: bool = False
    price_level_structure: str = ""
    price_ranges: list[PriceRange] = []
    # New bps / fp100 fields (exact precision — preferred).
    yes_bid_bps: int | None = None
    yes_ask_bps: int | None = None
    no_bid_bps: int | None = None
    no_ask_bps: int | None = None
    last_price_bps: int | None = None
    volume_fp100: int | None = None
    volume_24h_fp100: int | None = None
    open_interest_fp100: int | None = None

    def tick_bps(self) -> int:
        """Return the market's minimum tick size in basis points.

        1 cent = 100 bps. A whole-cent-tick market returns 100. A market
        with a 0.1¢ (0.001 dollar) tick returns 10. Defaults to 100 bps
        when ``price_ranges`` is empty (typical cent-only markets).

        If the market declares multiple ranges with different ticks,
        returns the minimum — the admission guard needs to reject if ANY
        portion of the range would produce sub-cent prices.
        """
        if not self.price_ranges:
            return 100
        ticks_bps: list[int] = []
        for pr in self.price_ranges:
            try:
                d = Decimal(pr.tick_dollars)
            except InvalidOperation:
                continue
            ticks_bps.append(int((d * Decimal(10_000)).to_integral_value()))
        return min(ticks_bps) if ticks_bps else 100

    @model_validator(mode="before")
    @classmethod
    def _migrate_fp(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Dollars → cents (legacy, lossy) + bps (new, exact).
        for legacy, new_bps, wire in [
            ("yes_bid", "yes_bid_bps", "yes_bid_dollars"),
            ("yes_ask", "yes_ask_bps", "yes_ask_dollars"),
            ("no_bid", "no_bid_bps", "no_bid_dollars"),
            ("no_ask", "no_ask_bps", "no_ask_dollars"),
            ("last_price", "last_price_bps", "last_price_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _dollars_to_cents(data[wire])
                data[new_bps] = _dollars_to_bps(data[wire])
        # FP → int (legacy, floor) + fp100 (new, exact).
        for legacy, new_fp100, wire in [
            ("volume", "volume_fp100", "volume_fp"),
            ("volume_24h", "volume_24h_fp100", "volume_24h_fp"),
            ("open_interest", "open_interest_fp100", "open_interest_fp"),
        ]:
            if wire in data and data[wire] is not None:
                data[legacy] = _fp_to_int(data[wire])
                data[new_fp100] = _fp_to_fp100(data[wire])
        return data


class Event(BaseModel):
    """A Kalshi event containing one or more markets."""

    event_ticker: str
    series_ticker: str
    title: str
    sub_title: str = ""
    category: str
    status: str | None = None
    mutually_exclusive: bool | None = None
    markets: list[Market] = []


class Series(BaseModel):
    """A Kalshi series (template for events)."""

    series_ticker: str
    title: str
    category: str
    tags: list[str] = []
    fee_type: str = "quadratic_with_maker_fees"
    fee_multiplier: float = 0.0175
    frequency: str = ""
    settlement_sources: list[dict[str, Any]] = []

    @model_validator(mode="before")
    @classmethod
    def _coerce_nullable_lists(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if data.get("tags") is None:
            data["tags"] = []
        if data.get("settlement_sources") is None:
            data["settlement_sources"] = []
        return data


class OrderBook(BaseModel):
    """Orderbook snapshot for a market.

    Raw API returns [[price, qty], ...] arrays — we parse into OrderBookLevel.
    Post March 12: levels may be [["dollars_str", "fp_str"], ...] strings.
    """

    market_ticker: str
    yes: list[OrderBookLevel]
    no: list[OrderBookLevel]

    @classmethod
    def _parse_levels(cls, raw: list[list[int]]) -> list[OrderBookLevel]:
        return [OrderBookLevel(price=pair[0], quantity=pair[1]) for pair in raw]

    @model_validator(mode="before")
    @classmethod
    def _coerce_levels(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Handle orderbook_fp wrapper (new API may nest under this key)
            if "orderbook_fp" in data and "yes" not in data:
                inner = data["orderbook_fp"]
                if isinstance(inner, dict):
                    data.update(inner)
            # REST returns yes_dollars/no_dollars; WS returns yes_dollars_fp/no_dollars_fp.
            # Normalize to yes/no before parsing levels.
            for side, rest_key, ws_key in [
                ("yes", "yes_dollars", "yes_dollars_fp"),
                ("no", "no_dollars", "no_dollars_fp"),
            ]:
                if side not in data or not data[side]:
                    for alt in (rest_key, ws_key):
                        if alt in data and data[alt]:
                            data[side] = data[alt]
                            break
            for side in ("yes", "no"):
                levels = data.get(side)
                if levels and isinstance(levels, list) and levels and isinstance(levels[0], list):
                    coerced = []
                    for pair in levels:
                        # New format: ["0.52", "10.00"] (dollars str, fp str)
                        # Old format: [52, 10] (cents int, qty int)
                        p, q = pair[0], pair[1]
                        # Dual-populate: legacy cents/int alongside exact bps/fp100.
                        # Integer wire (pre-migration) has no _bps/_fp100 promotion —
                        # new fields stay at the OrderBookLevel default of 0.
                        level: dict[str, int] = {}
                        if isinstance(p, str):
                            level["price"] = _dollars_to_cents(p)
                            level["price_bps"] = _dollars_to_bps(p)
                        else:
                            level["price"] = p
                        if isinstance(q, str):
                            level["quantity"] = _fp_to_int(q)
                            level["quantity_fp100"] = _fp_to_fp100(q)
                        else:
                            level["quantity"] = q
                        coerced.append(level)
                    data[side] = coerced
        return data


class Trade(BaseModel):
    """A single trade execution.

    The Kalshi API returns ``taker_side`` (not ``side``) and ``price`` as a
    dollar float (not cents int).  The validator normalizes both so downstream
    code always sees ``side`` as a string and ``price`` as cents.

    Dual-unit migration (bps/fp100): each money/count field has a
    ``_bps`` / ``_fp100`` sibling alongside the legacy cents/int field.
    The float-price path populates ``price_bps`` via ``int(round(p *
    10_000))`` rather than the fail-closed Decimal parser, because
    IEEE-754 representation of dollar floats (e.g. ``0.53 * 10_000 =
    5299.9999...``) would otherwise trip sub-bps precision checks and
    raise. Whole-cent and sub-cent values both round correctly through
    this path. Legacy fields are deleted in Task 13 of
    ``docs/superpowers/specs/2026-04-17-bps-fp100-unit-migration-design.md``.

    Post March 12: _dollars/_fp fields replace integer fields.
    """

    ticker: str
    trade_id: str
    price: int
    count: int
    side: str
    created_time: str
    yes_price: int | None = None
    no_price: int | None = None
    # New bps / fp100 fields (exact precision — preferred).
    price_bps: int = 0
    yes_price_bps: int | None = None
    no_price_bps: int | None = None
    count_fp100: int = 0

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # API returns taker_side, normalize to side
            if "taker_side" in data and "side" not in data:
                data["side"] = data["taker_side"]
            # Dollars → cents (legacy) + bps (new, exact).
            for legacy, new_bps, wire in [
                ("yes_price", "yes_price_bps", "yes_price_dollars"),
                ("no_price", "no_price_bps", "no_price_dollars"),
            ]:
                if wire in data and data[wire] is not None:
                    data[legacy] = _dollars_to_cents(data[wire])
                    data[new_bps] = _dollars_to_bps(data[wire])
            # Count fp → int (legacy, floor) + fp100 (new, exact).
            if "count_fp" in data and data["count_fp"] is not None:
                data["count"] = _fp_to_int(data["count_fp"])
                data["count_fp100"] = _fp_to_fp100(data["count_fp"])
            # API returns price as float (dollars), normalize to cents + bps.
            # NOTE: float path uses int(round(p * 10_000)) — NOT the Decimal
            # parser — because IEEE-754 artifacts (e.g. 0.53 * 10_000 =
            # 5299.9999...) would otherwise trip the fail-closed sub-bps
            # check in dollars_str_to_bps.
            if "price" in data:
                p = data["price"]
                if isinstance(p, float) and p <= 1.0:
                    data["price"] = round(p * 100)
                    data["price_bps"] = int(round(p * 10_000))
            # If price missing but yes_price present, derive it (both legacy
            # and bps, after the _dollars wire promotion above).
            if "price" not in data and "yes_price" in data:
                data["price"] = data["yes_price"]
                if "yes_price_bps" in data:
                    data["price_bps"] = data["yes_price_bps"]
        return data
