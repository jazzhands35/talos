"""Pydantic models for Kalshi market data."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, model_validator

from talos.models._converters import dollars_to_bps as _dollars_to_bps
from talos.models._converters import fp_to_fp100 as _fp_to_fp100


class OrderBookLevel(BaseModel):
    """A single price level in the orderbook.

    Uses bps (basis points) and fp100 (1/100 of a contract) for exact
    precision. Wire-parsed levels populate ``price_bps`` / ``quantity_fp100``
    via :meth:`OrderBook._coerce_levels`.

    Task 13a-2b (2026-04-23): legacy integer-cents ``price`` and
    integer-contracts ``quantity`` fields removed.
    """

    price_bps: int
    quantity_fp100: int


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

    Money/count fields use bps and fp100 for exact precision. The validator
    converts ``_dollars`` / ``_fp`` wire strings into the ``_bps`` /
    ``_fp100`` fields via the Decimal parsers in :mod:`talos.units`.

    Task 13a-2b (2026-04-23): legacy integer-cents / integer-contracts
    fields (``yes_bid``, ``no_ask``, ``volume``, ``last_price``, ...)
    removed.

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
    # bps / fp100 fields (exact precision).
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
        # Dollars → bps (exact).
        for new_bps, wire in [
            ("yes_bid_bps", "yes_bid_dollars"),
            ("yes_ask_bps", "yes_ask_dollars"),
            ("no_bid_bps", "no_bid_dollars"),
            ("no_ask_bps", "no_ask_dollars"),
            ("last_price_bps", "last_price_dollars"),
        ]:
            if wire in data and data[wire] is not None:
                data[new_bps] = _dollars_to_bps(data[wire])
        # FP → fp100 (exact).
        for new_fp100, wire in [
            ("volume_fp100", "volume_fp"),
            ("volume_24h_fp100", "volume_24h_fp"),
            ("open_interest_fp100", "open_interest_fp"),
        ]:
            if wire in data and data[wire] is not None:
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
        # Integer wire: promote cents → bps, whole-contracts → fp100.
        return [
            OrderBookLevel(price_bps=pair[0] * 100, quantity_fp100=pair[1] * 100)
            for pair in raw
        ]

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
                        # Old format: [52, 10] (cents int, qty int) — promote ×100
                        p, q = pair[0], pair[1]
                        level: dict[str, int] = {}
                        if isinstance(p, str):
                            level["price_bps"] = _dollars_to_bps(p)
                        else:
                            level["price_bps"] = p * 100
                        if isinstance(q, str):
                            level["quantity_fp100"] = _fp_to_fp100(q)
                        else:
                            level["quantity_fp100"] = q * 100
                        coerced.append(level)
                    data[side] = coerced
        return data


class Trade(BaseModel):
    """A single trade execution.

    The Kalshi API returns ``taker_side`` (not ``side``) and ``price`` as a
    dollar float (not cents int). The validator normalizes ``side`` and
    converts the float price into ``price_bps`` via ``int(round(p *
    10_000))`` (not the fail-closed Decimal parser — IEEE-754 artifacts
    such as ``0.53 * 10_000 = 5299.9999...`` would otherwise trip sub-bps
    precision checks and raise).

    Task 13a-2b (2026-04-23): legacy integer cents / integer-contract
    fields (``price``, ``count``, ``yes_price``, ``no_price``) removed.
    """

    ticker: str
    trade_id: str
    side: str
    created_time: str
    # bps / fp100 fields (exact precision).
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
            # Dollars → bps (exact).
            for new_bps, wire in [
                ("yes_price_bps", "yes_price_dollars"),
                ("no_price_bps", "no_price_dollars"),
            ]:
                if wire in data and data[wire] is not None:
                    data[new_bps] = _dollars_to_bps(data[wire])
            # Count fp → fp100 (exact).
            if "count_fp" in data and data["count_fp"] is not None:
                data["count_fp100"] = _fp_to_fp100(data["count_fp"])
            # API returns price as float (dollars), normalize to bps.
            # NOTE: float path uses int(round(p * 10_000)) — NOT the Decimal
            # parser — because IEEE-754 artifacts (e.g. 0.53 * 10_000 =
            # 5299.9999...) would otherwise trip the fail-closed sub-bps
            # check in dollars_str_to_bps.
            if "price" in data:
                p = data["price"]
                if isinstance(p, float) and p <= 1.0:
                    data["price_bps"] = int(round(p * 10_000))
                elif isinstance(p, int):
                    # Integer cents wire — legacy path, promote to bps.
                    data["price_bps"] = p * 100
                # Remove the legacy-only 'price' key from the dict; Pydantic
                # would otherwise try to apply it to a non-existent field.
                del data["price"]
            # If price missing but yes_price derivation is needed for the bps.
            if "price_bps" not in data and "yes_price_bps" in data:
                data["price_bps"] = data["yes_price_bps"]
        return data
