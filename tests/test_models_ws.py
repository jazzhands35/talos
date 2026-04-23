"""Tests for WebSocket message Pydantic models (post bps/fp100 migration)."""

from talos.models.ws import (
    OrderBookDelta,
    OrderBookSnapshot,
    TickerMessage,
    TradeMessage,
    WSError,
    WSSubscribed,
)


class TestOrderBookSnapshot:
    def test_parse_snapshot_legacy_integer_wire(self) -> None:
        """Integer-wire path (pre-March-12 legacy, still accepted for fixtures).

        When ``yes_bps_fp100`` / ``no_bps_fp100`` are absent, the validator
        preserves the integer pairs in-place. After the 13a-2d cleanup the
        model only exposes the ``_bps_fp100`` arrays; legacy kwargs still
        work as dict values but consumers read only the bps arrays.
        """
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "market_id": "uuid-123",
            "yes_bps_fp100": [[6500, 10000], [6400, 20000]],
            "no_bps_fp100": [[3500, 15000], [3400, 5000]],
        }
        snap = OrderBookSnapshot.model_validate(data)
        assert snap.market_ticker == "KXBTC-26MAR-T50000"
        assert len(snap.yes_bps_fp100) == 2
        assert snap.yes_bps_fp100[0] == [6500, 10000]

    def test_parse_snapshot_dollars_fp_format(self) -> None:
        """Post March 12: yes_dollars_fp/no_dollars_fp with string pairs."""
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "market_id": "uuid-123",
            "yes_dollars_fp": [["0.65", "100.00"], ["0.64", "200.00"]],
            "no_dollars_fp": [["0.35", "150.00"], ["0.34", "50.00"]],
        }
        snap = OrderBookSnapshot.model_validate(data)
        # 0.65 dollars = 6500 bps; 100.00 fp = 10000 fp100.
        assert snap.yes_bps_fp100[0] == [6500, 10000]
        assert snap.yes_bps_fp100[1] == [6400, 20000]
        assert snap.no_bps_fp100[0] == [3500, 15000]
        assert snap.no_bps_fp100[1] == [3400, 5000]


class TestOrderBookDelta:
    def test_parse_delta_bps_fp100(self) -> None:
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "market_id": "uuid-123",
            "price_bps": 6500,
            "delta_fp100": -2000,
            "side": "yes",
            "ts": "2026-03-03T12:00:00Z",
        }
        d = OrderBookDelta.model_validate(data)
        assert d.price_bps == 6500
        assert d.delta_fp100 == -2000
        assert d.side == "yes"

    def test_parse_delta_dollars_fp_format(self) -> None:
        """Post March 12: price_dollars and delta_fp strings."""
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "market_id": "uuid-123",
            "price_dollars": "0.65",
            "delta_fp": "-20.00",
            "side": "yes",
            "ts": "2026-03-12T12:00:00Z",
        }
        d = OrderBookDelta.model_validate(data)
        assert d.price_bps == 6500
        assert d.delta_fp100 == -2000


class TestTickerMessage:
    def test_parse_ticker_bps_fp100(self) -> None:
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "yes_bid_bps": 6500,
            "yes_ask_bps": 6700,
            "no_bid_bps": 3300,
            "no_ask_bps": 3500,
            "last_price_bps": 6600,
            "volume_fp100": 1_500_000,
        }
        t = TickerMessage.model_validate(data)
        assert t.yes_bid_bps == 6500
        assert t.volume_fp100 == 1_500_000

    def test_parse_ticker_dollars_fp_format(self) -> None:
        """Post March 12: _dollars/_fp string fields."""
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "yes_bid_dollars": "0.65",
            "yes_ask_dollars": "0.67",
            "no_bid_dollars": "0.33",
            "no_ask_dollars": "0.35",
            "last_price_dollars": "0.66",
            "volume_fp": "15000.00",
        }
        t = TickerMessage.model_validate(data)
        assert t.yes_bid_bps == 6500
        assert t.yes_ask_bps == 6700
        assert t.no_bid_bps == 3300
        assert t.no_ask_bps == 3500
        assert t.last_price_bps == 6600
        assert t.volume_fp100 == 1_500_000


class TestTradeMessage:
    def test_parse_trade_bps_fp100(self) -> None:
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "price_bps": 6500,
            "count_fp100": 1000,
            "side": "yes",
            "ts": "2026-03-03T12:00:01Z",
            "trade_id": "trade-xyz",
        }
        t = TradeMessage.model_validate(data)
        assert t.count_fp100 == 1000

    def test_parse_trade_dollars_fp_format(self) -> None:
        """Post March 12: yes_price_dollars/no_price_dollars and count_fp."""
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "no_price_dollars": "0.35",
            "count_fp": "10.00",
            "side": "no",
            "ts": "2026-03-12T12:00:01Z",
            "trade_id": "trade-abc",
        }
        t = TradeMessage.model_validate(data)
        assert t.price_bps == 3500
        assert t.count_fp100 == 1000


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
