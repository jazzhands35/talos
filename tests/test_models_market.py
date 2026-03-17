"""Tests for market data Pydantic models."""

from talos.models.market import Event, Market, OrderBook, Series, Trade


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
