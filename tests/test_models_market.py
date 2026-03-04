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
