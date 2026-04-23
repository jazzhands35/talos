"""Tests for market data Pydantic models."""

import pytest

from talos.models.market import Event, Market, OrderBook, OrderBookLevel, Series, Trade


class TestMarket:
    def test_parse_market_json(self) -> None:
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "event_ticker": "KXBTC-26MAR",
            "title": "BTC above 50000?",
            "status": "open",
            "yes_bid": 65,
            "yes_ask": 67,
            "no_bid": 33,
            "no_ask": 35,
            "volume": 15000,
            "open_interest": 3200,
            "last_price": 66,
        }
        m = Market.model_validate(data)
        assert m.ticker == "KXBTC-26MAR-T50000"
        assert m.yes_bid == 65
        assert m.volume == 15000

    def test_parse_market_dollars_format(self) -> None:
        """Post March 12: _dollars/_fp string fields → int cents/int counts."""
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "event_ticker": "KXBTC-26MAR",
            "title": "BTC above 50000?",
            "status": "open",
            "yes_bid_dollars": "0.65",
            "yes_ask_dollars": "0.67",
            "no_bid_dollars": "0.33",
            "no_ask_dollars": "0.35",
            "volume_fp": "15000",
            "open_interest_fp": "3200",
            "last_price_dollars": "0.66",
        }
        m = Market.model_validate(data)
        assert m.yes_bid == 65
        assert m.yes_ask == 67
        assert m.no_bid == 33
        assert m.no_ask == 35
        assert m.volume == 15000
        assert m.open_interest == 3200
        assert m.last_price == 66

    def test_market_optional_fields(self) -> None:
        data = {
            "ticker": "TEST-MKT",
            "event_ticker": "TEST-EVT",
            "title": "Test",
            "status": "open",
        }
        m = Market.model_validate(data)
        assert m.yes_bid is None
        assert m.volume is None


class TestEvent:
    def test_parse_event_json(self) -> None:
        data = {
            "event_ticker": "KXBTC-26MAR",
            "series_ticker": "KXBTC",
            "title": "Bitcoin March 2026",
            "category": "Crypto",
            "status": "open",
            "markets": [],
        }
        e = Event.model_validate(data)
        assert e.event_ticker == "KXBTC-26MAR"
        assert e.category == "Crypto"

    def test_event_with_nested_markets(self) -> None:
        data = {
            "event_ticker": "KXBTC-26MAR",
            "series_ticker": "KXBTC",
            "title": "Bitcoin March 2026",
            "category": "Crypto",
            "status": "open",
            "markets": [
                {
                    "ticker": "KXBTC-26MAR-T50000",
                    "event_ticker": "KXBTC-26MAR",
                    "title": "BTC above 50000?",
                    "status": "open",
                }
            ],
        }
        e = Event.model_validate(data)
        assert len(e.markets) == 1
        assert e.markets[0].ticker == "KXBTC-26MAR-T50000"


class TestMarketEnrichedFields:
    """Phase 11: Market model enrichment."""

    def test_settlement_ts_captured(self) -> None:
        data = {
            "ticker": "MKT-1",
            "event_ticker": "EVT-1",
            "title": "Test",
            "status": "open",
            "settlement_ts": "2026-03-15T12:00:00Z",
            "close_time": "2026-03-15T11:00:00Z",
        }
        m = Market.model_validate(data)
        assert m.settlement_ts == "2026-03-15T12:00:00Z"
        assert m.close_time == "2026-03-15T11:00:00Z"

    def test_result_and_market_type(self) -> None:
        data = {
            "ticker": "MKT-1",
            "event_ticker": "EVT-1",
            "title": "Test",
            "status": "determined",
            "result": "yes",
            "market_type": "binary",
        }
        m = Market.model_validate(data)
        assert m.result == "yes"
        assert m.market_type == "binary"

    def test_expected_expiration_time_captured(self) -> None:
        data = {
            "ticker": "MKT-1",
            "event_ticker": "EVT-1",
            "title": "Test",
            "status": "open",
            "expected_expiration_time": "2026-03-19T04:30:00Z",
        }
        m = Market.model_validate(data)
        assert m.expected_expiration_time == "2026-03-19T04:30:00Z"

    def test_expected_expiration_time_absent(self) -> None:
        data = {"ticker": "MKT-1", "event_ticker": "EVT-1", "title": "T", "status": "open"}
        m = Market.model_validate(data)
        assert m.expected_expiration_time is None

    def test_defaults(self) -> None:
        data = {"ticker": "MKT-1", "event_ticker": "EVT-1", "title": "T", "status": "open"}
        m = Market.model_validate(data)
        assert m.settlement_ts is None
        assert m.result == ""
        assert m.market_type == "binary"


class TestSeries:
    def test_parse_series_json(self) -> None:
        data = {
            "series_ticker": "KXBTC",
            "title": "Bitcoin Prices",
            "category": "Crypto",
            "tags": ["bitcoin", "crypto"],
        }
        s = Series.model_validate(data)
        assert s.series_ticker == "KXBTC"
        assert "bitcoin" in s.tags

    def test_fee_type_and_multiplier(self) -> None:
        data = {
            "series_ticker": "KXMLB",
            "title": "MLB Games",
            "category": "Sports",
            "fee_type": "flat",
            "fee_multiplier": 0.02,
        }
        s = Series.model_validate(data)
        assert s.fee_type == "flat"
        assert s.fee_multiplier == 0.02

    def test_fee_defaults(self) -> None:
        data = {"series_ticker": "SER-1", "title": "Test", "category": "sports"}
        s = Series.model_validate(data)
        assert s.fee_type == "quadratic_with_maker_fees"
        assert s.fee_multiplier == 0.0175


class TestOrderBook:
    def test_parse_orderbook_json(self) -> None:
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "yes": [[65, 100], [64, 200]],
            "no": [[35, 150], [34, 50]],
        }
        ob = OrderBook.model_validate(data)
        assert ob.market_ticker == "KXBTC-26MAR-T50000"
        assert len(ob.yes) == 2
        assert ob.yes[0].price == 65
        assert ob.yes[0].quantity == 100

    def test_parse_orderbook_dollars_fp_format(self) -> None:
        """Post March 12: string-pair levels [["0.65", "100"], ...]."""
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "yes": [["0.65", "100"], ["0.64", "200"]],
            "no": [["0.35", "150"], ["0.34", "50"]],
        }
        ob = OrderBook.model_validate(data)
        assert ob.yes[0].price == 65
        assert ob.yes[0].quantity == 100
        assert ob.no[0].price == 35
        assert ob.no[1].quantity == 50


class TestTrade:
    def test_parse_trade_json(self) -> None:
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "trade_id": "abc-123",
            "price": 65,
            "count": 10,
            "side": "yes",
            "created_time": "2026-03-03T12:00:00Z",
        }
        t = Trade.model_validate(data)
        assert t.ticker == "KXBTC-26MAR-T50000"
        assert t.price == 65
        assert t.side == "yes"

    def test_parse_trade_dollars_fp_format(self) -> None:
        """Post March 12: yes_price_dollars/no_price_dollars and count_fp."""
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "trade_id": "abc-456",
            "yes_price_dollars": "0.65",
            "count_fp": "10",
            "side": "yes",
            "created_time": "2026-03-03T12:00:00Z",
        }
        t = Trade.model_validate(data)
        assert t.price == 65
        assert t.count == 10


class TestMarketDualBpsFp100Fields:
    """Task 3b-Market: Market model bps/fp100 siblings alongside legacy fields."""

    def test_dual_population_whole_cents(self) -> None:
        """Wire '0.53' → yes_bid==53 (legacy) AND yes_bid_bps==5300 (new)."""
        data = {
            "ticker": "MKT-1",
            "event_ticker": "EVT-1",
            "title": "T",
            "status": "open",
            "yes_bid_dollars": "0.53",
            "yes_ask_dollars": "0.55",
            "no_bid_dollars": "0.45",
            "no_ask_dollars": "0.47",
            "last_price_dollars": "0.54",
        }
        m = Market.model_validate(data)
        assert m.yes_bid == 53 and m.yes_bid_bps == 5300
        assert m.yes_ask == 55 and m.yes_ask_bps == 5500
        assert m.no_bid == 45 and m.no_bid_bps == 4500
        assert m.no_ask == 47 and m.no_ask_bps == 4700
        assert m.last_price == 54 and m.last_price_bps == 5400

    def test_subcent_price_retained_in_bps(self) -> None:
        """Wire '0.0488' → yes_bid==5 (lossy) AND yes_bid_bps==488 (exact).

        This is the DJT-class sub-cent motivating case: the legacy cents
        field rounds to 5¢ and silently loses precision; the _bps field
        preserves the exact 488 bps (4.88¢).
        """
        data = {
            "ticker": "DJT-MKT",
            "event_ticker": "DJT-EVT",
            "title": "T",
            "status": "open",
            "yes_bid_dollars": "0.0488",
            "no_ask_dollars": "0.9512",
        }
        m = Market.model_validate(data)
        assert m.yes_bid == 5
        assert m.yes_bid_bps == 488
        assert m.no_ask == 95
        assert m.no_ask_bps == 9512

    def test_fractional_volume_retained_in_fp100(self) -> None:
        """Wire volume_fp='1.89' → volume==1 (legacy floor) AND volume_fp100==189 (exact)."""
        data = {
            "ticker": "MARJ-MKT",
            "event_ticker": "MARJ-EVT",
            "title": "T",
            "status": "open",
            "volume_fp": "1.89",
            "volume_24h_fp": "10.50",
            "open_interest_fp": "3.25",
        }
        m = Market.model_validate(data)
        assert m.volume == 1 and m.volume_fp100 == 189
        assert m.volume_24h == 10 and m.volume_24h_fp100 == 1050
        assert m.open_interest == 3 and m.open_interest_fp100 == 325

    def test_none_and_absent_wire_fields_leave_defaults(self) -> None:
        """Absent / None wire fields → legacy stays None, bps/fp100 stay None."""
        data = {
            "ticker": "MKT-1",
            "event_ticker": "EVT-1",
            "title": "T",
            "status": "open",
        }
        m = Market.model_validate(data)
        assert m.yes_bid is None and m.yes_bid_bps is None
        assert m.yes_ask is None and m.yes_ask_bps is None
        assert m.no_bid is None and m.no_bid_bps is None
        assert m.no_ask is None and m.no_ask_bps is None
        assert m.last_price is None and m.last_price_bps is None
        assert m.volume is None and m.volume_fp100 is None
        assert m.volume_24h is None and m.volume_24h_fp100 is None
        assert m.open_interest is None and m.open_interest_fp100 is None

    def test_integer_cents_legacy_shape_leaves_bps_none(self) -> None:
        """Pre-migration integer wire fields populate only the legacy names.

        The _bps siblings are populated by the _dollars/_fp wire path only;
        integer shapes are legacy-only and do NOT promote to bps (matching
        the Fill integer fee_cost passthrough contract).
        """
        data = {
            "ticker": "MKT-1",
            "event_ticker": "EVT-1",
            "title": "T",
            "status": "open",
            "yes_bid": 65,
            "volume": 15000,
        }
        m = Market.model_validate(data)
        assert m.yes_bid == 65
        assert m.yes_bid_bps is None
        assert m.volume == 15000
        assert m.volume_fp100 is None


class TestTradeDualBpsFp100Fields:
    """Task 3b-Market: Trade model bps/fp100 siblings."""

    def test_dual_population_from_dollars_wire(self) -> None:
        """yes_price_dollars='0.65' → yes_price==65 AND yes_price_bps==6500."""
        data = {
            "ticker": "MKT-1",
            "trade_id": "trd-1",
            "yes_price_dollars": "0.65",
            "no_price_dollars": "0.35",
            "count_fp": "10",
            "side": "yes",
            "created_time": "2026-03-12T12:00:00Z",
        }
        t = Trade.model_validate(data)
        assert t.yes_price == 65 and t.yes_price_bps == 6500
        assert t.no_price == 35 and t.no_price_bps == 3500
        assert t.count == 10 and t.count_fp100 == 1000

    def test_float_price_path_populates_bps_exact(self) -> None:
        """Float dollar price (Trade API quirk): price=0.53 → price==53 AND price_bps==5300.

        Uses int(round(p * 10_000)) — NOT the Decimal parser — to avoid
        fail-closed artifacts from IEEE-754 representation of 0.53.
        """
        data = {
            "ticker": "MKT-1",
            "trade_id": "trd-float",
            "price": 0.53,
            "count": 5,
            "side": "yes",
            "created_time": "2026-03-03T12:00:00Z",
        }
        t = Trade.model_validate(data)
        assert t.price == 53
        assert t.price_bps == 5_300

    def test_float_price_path_subcent_float(self) -> None:
        """Float path handles sub-cent rounding: 0.0488 → price==5 lossy, price_bps==488 exact."""
        data = {
            "ticker": "DJT-MKT",
            "trade_id": "trd-subcent",
            "price": 0.0488,
            "count": 1,
            "side": "yes",
            "created_time": "2026-03-03T12:00:00Z",
        }
        t = Trade.model_validate(data)
        # round(0.0488 * 100) == 5 (legacy lossy)
        assert t.price == 5
        # int(round(0.0488 * 10_000)) == 488 (exact bps)
        assert t.price_bps == 488

    def test_price_derivation_from_yes_price_wire(self) -> None:
        """yes_price_dollars='0.53' with no `price` key → both price and price_bps derived."""
        data = {
            "ticker": "MKT-1",
            "trade_id": "trd-derive",
            "yes_price_dollars": "0.53",
            "count_fp": "1",
            "side": "yes",
            "created_time": "2026-03-03T12:00:00Z",
        }
        t = Trade.model_validate(data)
        assert t.price == 53
        assert t.price_bps == 5_300
        assert t.yes_price == 53
        assert t.yes_price_bps == 5_300

    def test_integer_price_does_not_promote_bps(self) -> None:
        """Integer `price` is the legacy pre-migration path — price_bps stays at default 0."""
        data = {
            "ticker": "MKT-1",
            "trade_id": "trd-int",
            "price": 65,  # integer cents, pre-migration
            "count": 10,
            "side": "yes",
            "created_time": "2026-03-03T12:00:00Z",
        }
        t = Trade.model_validate(data)
        assert t.price == 65
        # Integer passthrough: no bps promotion (matches Fill.fee_cost int contract).
        assert t.price_bps == 0

    def test_defaults_when_absent(self) -> None:
        """Minimal Trade with count_fp absent — count_fp100 stays default 0."""
        data = {
            "ticker": "MKT-1",
            "trade_id": "trd-empty",
            "price": 0.01,  # float path to satisfy `price: int` after coercion
            "count": 0,
            "side": "yes",
            "created_time": "2026-03-03T12:00:00Z",
        }
        t = Trade.model_validate(data)
        assert t.count == 0
        assert t.count_fp100 == 0
        assert t.yes_price is None
        assert t.yes_price_bps is None
        assert t.no_price is None
        assert t.no_price_bps is None


class TestOrderBookLevelDualBpsFp100Fields:
    """Task 3b-Market: OrderBookLevel dual fields default to 0 for direct
    construction; are populated for wire-parsed levels via OrderBook."""

    def test_direct_construction_leaves_new_fields_zero(self) -> None:
        """OrderBookLevel(price=53, quantity=10) → price_bps==0, quantity_fp100==0.

        Callers that construct directly with legacy positional semantics
        keep working; their migration to dual-population lands in later
        tasks. The default-of-0 strategy unblocks this migration without
        a big-bang caller rewrite.
        """
        lvl = OrderBookLevel(price=53, quantity=10)
        assert lvl.price == 53
        assert lvl.quantity == 10
        assert lvl.price_bps == 0
        assert lvl.quantity_fp100 == 0

    def test_direct_construction_with_all_fields(self) -> None:
        """Explicit dual-population via direct construction is also supported."""
        lvl = OrderBookLevel(
            price=53, quantity=10, price_bps=5_300, quantity_fp100=1_000
        )
        assert lvl.price == 53
        assert lvl.price_bps == 5_300
        assert lvl.quantity == 10
        assert lvl.quantity_fp100 == 1_000

    def test_wire_parse_populates_both_legacy_and_new(self) -> None:
        """OrderBook._coerce_levels dual-populates when parsing [["0.65", "100"], ...]."""
        data = {
            "market_ticker": "MKT-1",
            "yes": [["0.65", "100"], ["0.64", "200"]],
            "no": [["0.35", "150"]],
        }
        ob = OrderBook.model_validate(data)
        assert ob.yes[0].price == 65 and ob.yes[0].price_bps == 6_500
        assert ob.yes[0].quantity == 100 and ob.yes[0].quantity_fp100 == 10_000
        assert ob.yes[1].price == 64 and ob.yes[1].price_bps == 6_400
        assert ob.yes[1].quantity == 200 and ob.yes[1].quantity_fp100 == 20_000
        assert ob.no[0].price == 35 and ob.no[0].price_bps == 3_500
        assert ob.no[0].quantity == 150 and ob.no[0].quantity_fp100 == 15_000

    def test_wire_parse_subcent_level(self) -> None:
        """['0.0488', '1.89'] → price==5 lossy, price_bps==488 exact; qty==1, fp100==189."""
        data = {
            "market_ticker": "MARJ-MKT",
            "yes": [["0.0488", "1.89"]],
            "no": [["0.9512", "10.00"]],
        }
        ob = OrderBook.model_validate(data)
        assert ob.yes[0].price == 5
        assert ob.yes[0].price_bps == 488
        assert ob.yes[0].quantity == 1
        assert ob.yes[0].quantity_fp100 == 189
        assert ob.no[0].price == 95
        assert ob.no[0].price_bps == 9_512
        assert ob.no[0].quantity == 10
        assert ob.no[0].quantity_fp100 == 1_000

    def test_integer_wire_shape_leaves_new_fields_zero(self) -> None:
        """Pre-migration integer levels [[65, 100], ...] populate legacy only."""
        data = {
            "market_ticker": "MKT-1",
            "yes": [[65, 100]],
            "no": [[35, 150]],
        }
        ob = OrderBook.model_validate(data)
        assert ob.yes[0].price == 65
        # Integer wire is pre-migration — no bps promotion.
        assert ob.yes[0].price_bps == 0
        assert ob.yes[0].quantity == 100
        assert ob.yes[0].quantity_fp100 == 0


class TestMarketDualFieldInvariants:
    """Parallel-field contract: for any whole-cent wire value, the _bps
    field equals the legacy cents field × 100 exactly. Mirrors the Order /
    Fill invariant suite so a regression in this model surfaces as a test
    failure during the multi-commit migration.
    """

    @pytest.mark.parametrize(
        "wire_dollars,cents,bps",
        [
            ("0.01", 1, 100),
            ("0.53", 53, 5_300),
            ("0.99", 99, 9_900),
            ("1.00", 100, 10_000),
        ],
    )
    def test_market_bps_equals_cents_times_100_for_whole_cent(
        self, wire_dollars: str, cents: int, bps: int
    ) -> None:
        m = Market.model_validate({
            "ticker": "x",
            "event_ticker": "y",
            "title": "t",
            "status": "open",
            "yes_bid_dollars": wire_dollars,
        })
        assert m.yes_bid == cents
        assert m.yes_bid_bps == bps
        # Parallel-field invariant: bps == cents * 100 (expressed against the
        # parametrized values, not the model attrs, because Pyright narrows
        # Market.yes_bid to int | None even though the wire payload populated it).
        assert bps == cents * 100

    @pytest.mark.parametrize(
        "wire_fp,legacy,fp100",
        [
            ("0.00", 0, 0),
            ("1.00", 1, 100),
            ("15000.00", 15_000, 1_500_000),
        ],
    )
    def test_market_volume_fp100_equals_legacy_times_100_for_whole(
        self, wire_fp: str, legacy: int, fp100: int
    ) -> None:
        m = Market.model_validate({
            "ticker": "x",
            "event_ticker": "y",
            "title": "t",
            "status": "open",
            "volume_fp": wire_fp,
        })
        assert m.volume == legacy
        assert m.volume_fp100 == fp100
        # Parallel-field invariant: fp100 == legacy * 100. Expressed against
        # parametrized values (Market.volume is int | None post-migration).
        assert fp100 == legacy * 100
