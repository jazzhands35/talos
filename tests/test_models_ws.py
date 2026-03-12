"""Tests for WebSocket message Pydantic models."""

from talos.models.ws import (
    OrderBookDelta,
    OrderBookSnapshot,
    TickerMessage,
    TradeMessage,
    WSError,
    WSSubscribed,
)


class TestOrderBookSnapshot:
    def test_parse_snapshot(self) -> None:
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "market_id": "uuid-123",
            "yes": [[65, 100], [64, 200]],
            "no": [[35, 150], [34, 50]],
        }
        snap = OrderBookSnapshot.model_validate(data)
        assert snap.market_ticker == "KXBTC-26MAR-T50000"
        assert len(snap.yes) == 2
        assert snap.yes[0] == [65, 100]

    def test_parse_snapshot_dollars_fp_format(self) -> None:
        """Post March 12: yes_dollars_fp/no_dollars_fp with string pairs."""
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "market_id": "uuid-123",
            "yes_dollars_fp": [["0.65", "100"], ["0.64", "200"]],
            "no_dollars_fp": [["0.35", "150"], ["0.34", "50"]],
        }
        snap = OrderBookSnapshot.model_validate(data)
        assert snap.yes[0] == [65, 100]
        assert snap.yes[1] == [64, 200]
        assert snap.no[0] == [35, 150]
        assert snap.no[1] == [34, 50]


class TestOrderBookDelta:
    def test_parse_delta(self) -> None:
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "market_id": "uuid-123",
            "price": 65,
            "delta": -20,
            "side": "yes",
            "ts": "2026-03-03T12:00:00Z",
        }
        d = OrderBookDelta.model_validate(data)
        assert d.price == 65
        assert d.delta == -20
        assert d.side == "yes"

    def test_parse_delta_dollars_fp_format(self) -> None:
        """Post March 12: price_dollars and delta_fp strings."""
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "market_id": "uuid-123",
            "price_dollars": "0.65",
            "delta_fp": "-20",
            "side": "yes",
            "ts": "2026-03-12T12:00:00Z",
        }
        d = OrderBookDelta.model_validate(data)
        assert d.price == 65
        assert d.delta == -20


class TestTickerMessage:
    def test_parse_ticker(self) -> None:
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "yes_bid": 65,
            "yes_ask": 67,
            "no_bid": 33,
            "no_ask": 35,
            "last_price": 66,
            "volume": 15000,
        }
        t = TickerMessage.model_validate(data)
        assert t.yes_bid == 65
        assert t.volume == 15000

    def test_parse_ticker_dollars_fp_format(self) -> None:
        """Post March 12: _dollars/_fp string fields."""
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "yes_bid_dollars": "0.65",
            "yes_ask_dollars": "0.67",
            "no_bid_dollars": "0.33",
            "no_ask_dollars": "0.35",
            "last_price_dollars": "0.66",
            "volume_fp": "15000",
        }
        t = TickerMessage.model_validate(data)
        assert t.yes_bid == 65
        assert t.yes_ask == 67
        assert t.no_bid == 33
        assert t.no_ask == 35
        assert t.last_price == 66
        assert t.volume == 15000


class TestTradeMessage:
    def test_parse_trade(self) -> None:
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "price": 65,
            "count": 10,
            "side": "yes",
            "ts": "2026-03-03T12:00:01Z",
            "trade_id": "trade-xyz",
        }
        t = TradeMessage.model_validate(data)
        assert t.count == 10

    def test_parse_trade_dollars_fp_format(self) -> None:
        """Post March 12: yes_price_dollars/no_price_dollars and count_fp."""
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "no_price_dollars": "0.35",
            "count_fp": "10",
            "side": "no",
            "ts": "2026-03-12T12:00:01Z",
            "trade_id": "trade-abc",
        }
        t = TradeMessage.model_validate(data)
        assert t.price == 35
        assert t.count == 10


class TestWSSubscribed:
    def test_parse_subscribed(self) -> None:
        data = {"channel": "orderbook_delta", "sid": 1}
        s = WSSubscribed.model_validate(data)
        assert s.channel == "orderbook_delta"
        assert s.sid == 1


class TestWSError:
    def test_parse_error(self) -> None:
        data = {"code": 400, "msg": "invalid ticker"}
        e = WSError.model_validate(data)
        assert e.code == 400
        assert e.msg == "invalid ticker"
