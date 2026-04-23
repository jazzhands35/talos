"""Tests for market data Pydantic models."""

import pytest

from talos.models.market import Event, Market, OrderBook, OrderBookLevel, Series, Trade


class TestMarket:
    def test_parse_market_dollars_format(self) -> None:
        """Post March 12: _dollars/_fp string fields → bps / fp100."""
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
        assert m.yes_bid_bps == 6500
        assert m.yes_ask_bps == 6700
        assert m.no_bid_bps == 3300
        assert m.no_ask_bps == 3500
        assert m.volume_fp100 == 1_500_000
        assert m.open_interest_fp100 == 320_000
        assert m.last_price_bps == 6600

    def test_market_optional_fields(self) -> None:
        data = {
            "ticker": "TEST-MKT",
            "event_ticker": "TEST-EVT",
            "title": "Test",
            "status": "open",
        }
        m = Market.model_validate(data)
        assert m.yes_bid_bps is None
        assert m.volume_fp100 is None


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
        """Integer wire (legacy) — cents/contracts promote to bps/fp100 via ×100."""
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "yes": [[65, 100], [64, 200]],
            "no": [[35, 150], [34, 50]],
        }
        ob = OrderBook.model_validate(data)
        assert ob.market_ticker == "KXBTC-26MAR-T50000"
        assert len(ob.yes) == 2
        assert ob.yes[0].price_bps == 6500
        assert ob.yes[0].quantity_fp100 == 10_000

    def test_parse_orderbook_dollars_fp_format(self) -> None:
        """Post March 12: string-pair levels [["0.65", "100"], ...]."""
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "yes": [["0.65", "100"], ["0.64", "200"]],
            "no": [["0.35", "150"], ["0.34", "50"]],
        }
        ob = OrderBook.model_validate(data)
        assert ob.yes[0].price_bps == 6500
        assert ob.yes[0].quantity_fp100 == 10_000
        assert ob.no[0].price_bps == 3500
        assert ob.no[1].quantity_fp100 == 5000


class TestTrade:
    def test_parse_trade_integer_wire(self) -> None:
        """Integer cents wire (legacy) — promotes to price_bps via ×100."""
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
        assert t.price_bps == 6500
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
        assert t.price_bps == 6500
        assert t.count_fp100 == 1000


class TestMarketBpsFp100Fields:
    """Task 3b-Market: Market model bps/fp100 wire population."""

    def test_whole_cents(self) -> None:
        """Wire '0.53' → yes_bid_bps==5300."""
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
        assert m.yes_bid_bps == 5300
        assert m.yes_ask_bps == 5500
        assert m.no_bid_bps == 4500
        assert m.no_ask_bps == 4700
        assert m.last_price_bps == 5400

    def test_subcent_price_retained_in_bps(self) -> None:
        """Wire '0.0488' → yes_bid_bps==488 (exact sub-cent retained)."""
        data = {
            "ticker": "DJT-MKT",
            "event_ticker": "DJT-EVT",
            "title": "T",
            "status": "open",
            "yes_bid_dollars": "0.0488",
            "no_ask_dollars": "0.9512",
        }
        m = Market.model_validate(data)
        assert m.yes_bid_bps == 488
        assert m.no_ask_bps == 9512

    def test_fractional_volume_retained_in_fp100(self) -> None:
        """Wire volume_fp='1.89' → volume_fp100==189 (exact retained)."""
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
        assert m.volume_fp100 == 189
        assert m.volume_24h_fp100 == 1050
        assert m.open_interest_fp100 == 325

    def test_absent_wire_fields_leave_none(self) -> None:
        """Absent wire fields → bps/fp100 stay None."""
        data = {
            "ticker": "MKT-1",
            "event_ticker": "EVT-1",
            "title": "T",
            "status": "open",
        }
        m = Market.model_validate(data)
        assert m.yes_bid_bps is None
        assert m.yes_ask_bps is None
        assert m.no_bid_bps is None
        assert m.no_ask_bps is None
        assert m.last_price_bps is None
        assert m.volume_fp100 is None
        assert m.volume_24h_fp100 is None
        assert m.open_interest_fp100 is None


class TestTradeBpsFp100Fields:
    """Task 3b-Market: Trade model bps/fp100 population."""

    def test_from_dollars_wire(self) -> None:
        """yes_price_dollars='0.65' → yes_price_bps==6500."""
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
        assert t.yes_price_bps == 6500
        assert t.no_price_bps == 3500
        assert t.count_fp100 == 1000

    def test_float_price_path_populates_bps_exact(self) -> None:
        """Float dollar price (Trade API quirk): price=0.53 → price_bps==5300.

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
        assert t.price_bps == 5_300

    def test_float_price_path_subcent_float(self) -> None:
        """Float path handles sub-cent exactly: 0.0488 → price_bps==488."""
        data = {
            "ticker": "DJT-MKT",
            "trade_id": "trd-subcent",
            "price": 0.0488,
            "count": 1,
            "side": "yes",
            "created_time": "2026-03-03T12:00:00Z",
        }
        t = Trade.model_validate(data)
        # int(round(0.0488 * 10_000)) == 488
        assert t.price_bps == 488

    def test_price_derivation_from_yes_price_wire(self) -> None:
        """yes_price_dollars='0.53' with no `price` key → price_bps derived."""
        data = {
            "ticker": "MKT-1",
            "trade_id": "trd-derive",
            "yes_price_dollars": "0.53",
            "count_fp": "1",
            "side": "yes",
            "created_time": "2026-03-03T12:00:00Z",
        }
        t = Trade.model_validate(data)
        assert t.price_bps == 5_300
        assert t.yes_price_bps == 5_300

    def test_integer_price_promotes_bps(self) -> None:
        """Integer cents price (pre-migration) promotes to bps via ×100."""
        data = {
            "ticker": "MKT-1",
            "trade_id": "trd-int",
            "price": 65,
            "count": 10,
            "side": "yes",
            "created_time": "2026-03-03T12:00:00Z",
        }
        t = Trade.model_validate(data)
        assert t.price_bps == 6500

    def test_defaults_when_absent(self) -> None:
        """Minimal Trade — count_fp100 stays default 0, yes/no bps None."""
        data = {
            "ticker": "MKT-1",
            "trade_id": "trd-empty",
            "price": 0.01,
            "side": "yes",
            "created_time": "2026-03-03T12:00:00Z",
        }
        t = Trade.model_validate(data)
        assert t.count_fp100 == 0
        assert t.yes_price_bps is None
        assert t.no_price_bps is None


class TestOrderBookLevelFields:
    """OrderBookLevel uses bps/fp100 only post-13a-2b."""

    def test_direct_construction(self) -> None:
        """OrderBookLevel(price_bps=5300, quantity_fp100=1000)."""
        lvl = OrderBookLevel(price_bps=5_300, quantity_fp100=1_000)
        assert lvl.price_bps == 5_300
        assert lvl.quantity_fp100 == 1_000

    def test_wire_parse_populates_bps_fp100(self) -> None:
        """OrderBook._coerce_levels populates from [["0.65", "100"], ...]."""
        data = {
            "market_ticker": "MKT-1",
            "yes": [["0.65", "100"], ["0.64", "200"]],
            "no": [["0.35", "150"]],
        }
        ob = OrderBook.model_validate(data)
        assert ob.yes[0].price_bps == 6_500
        assert ob.yes[0].quantity_fp100 == 10_000
        assert ob.yes[1].price_bps == 6_400
        assert ob.yes[1].quantity_fp100 == 20_000
        assert ob.no[0].price_bps == 3_500
        assert ob.no[0].quantity_fp100 == 15_000

    def test_wire_parse_subcent_level(self) -> None:
        """['0.0488', '1.89'] → price_bps==488 exact, fp100==189."""
        data = {
            "market_ticker": "MARJ-MKT",
            "yes": [["0.0488", "1.89"]],
            "no": [["0.9512", "10.00"]],
        }
        ob = OrderBook.model_validate(data)
        assert ob.yes[0].price_bps == 488
        assert ob.yes[0].quantity_fp100 == 189
        assert ob.no[0].price_bps == 9_512
        assert ob.no[0].quantity_fp100 == 1_000

    def test_integer_wire_shape_promotes_to_bps(self) -> None:
        """Pre-migration integer levels [[65, 100], ...] promote to bps via ×100."""
        data = {
            "market_ticker": "MKT-1",
            "yes": [[65, 100]],
            "no": [[35, 150]],
        }
        ob = OrderBook.model_validate(data)
        assert ob.yes[0].price_bps == 6_500
        assert ob.yes[0].quantity_fp100 == 10_000


class TestMarketFieldInvariants:
    """Pin the wire→bps contract for whole-cent values."""

    @pytest.mark.parametrize(
        "wire_dollars,bps",
        [
            ("0.01", 100),
            ("0.53", 5_300),
            ("0.99", 9_900),
            ("1.00", 10_000),
        ],
    )
    def test_market_yes_bid_bps_from_wire(
        self, wire_dollars: str, bps: int
    ) -> None:
        m = Market.model_validate({
            "ticker": "x",
            "event_ticker": "y",
            "title": "t",
            "status": "open",
            "yes_bid_dollars": wire_dollars,
        })
        assert m.yes_bid_bps == bps

    @pytest.mark.parametrize(
        "wire_fp,fp100",
        [
            ("0.00", 0),
            ("1.00", 100),
            ("15000.00", 1_500_000),
        ],
    )
    def test_market_volume_fp100_from_wire(
        self, wire_fp: str, fp100: int
    ) -> None:
        m = Market.model_validate({
            "ticker": "x",
            "event_ticker": "y",
            "title": "t",
            "status": "open",
            "volume_fp": wire_fp,
        })
        assert m.volume_fp100 == fp100
