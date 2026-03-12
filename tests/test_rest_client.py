"""Tests for Kalshi REST client."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from talos.auth import KalshiAuth
from talos.config import KalshiConfig, KalshiEnvironment
from talos.errors import KalshiAPIError, KalshiRateLimitError
from talos.rest_client import KalshiRESTClient


@pytest.fixture()
def config() -> KalshiConfig:
    return KalshiConfig(
        environment=KalshiEnvironment.DEMO,
        key_id="test-key",
        private_key_path=Path("/tmp/fake.pem"),
        rest_base_url="https://demo-api.kalshi.co/trade-api/v2",
        ws_url="wss://demo-api.kalshi.co/",
    )


@pytest.fixture()
def mock_auth() -> KalshiAuth:
    auth = AsyncMock(spec=KalshiAuth)
    auth.key_id = "test-key"
    auth.headers.return_value = {
        "KALSHI-ACCESS-KEY": "test-key",
        "KALSHI-ACCESS-TIMESTAMP": "1234567890",
        "KALSHI-ACCESS-SIGNATURE": "fakesig",
    }
    return auth


@pytest.fixture()
def client(config: KalshiConfig, mock_auth: KalshiAuth) -> KalshiRESTClient:
    return KalshiRESTClient(auth=mock_auth, config=config)


def _mock_response(status: int, json_data: dict, headers: dict | None = None) -> httpx.Response:
    """Create a mock httpx.Response."""
    return httpx.Response(
        status_code=status,
        json=json_data,
        headers=headers or {},
    )


class TestClientConstruction:
    def test_base_url_set(self, client: KalshiRESTClient) -> None:
        assert "demo-api.kalshi.co" in client._base_url


class TestAuthInjection:
    async def test_auth_headers_added(
        self, client: KalshiRESTClient, mock_auth: KalshiAuth
    ) -> None:
        mock_resp = _mock_response(200, {"trading_active": True, "exchange_active": True})
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=mock_resp)

        await client.get_exchange_status()
        mock_auth.headers.assert_called_once()  # type: ignore[union-attr]


class TestErrorMapping:
    async def test_400_raises_api_error(self, client: KalshiRESTClient) -> None:
        mock_resp = _mock_response(400, {"error": "bad request"})
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=mock_resp)

        with pytest.raises(KalshiAPIError) as exc_info:
            await client.get_exchange_status()
        assert exc_info.value.status_code == 400

    async def test_429_raises_rate_limit_error(self, client: KalshiRESTClient) -> None:
        mock_resp = _mock_response(429, {"error": "rate limited"}, headers={"Retry-After": "5"})
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=mock_resp)

        with pytest.raises(KalshiRateLimitError) as exc_info:
            await client.get_exchange_status()
        assert exc_info.value.retry_after == 5.0


class TestExchangeStatus:
    async def test_get_exchange_status(self, client: KalshiRESTClient) -> None:
        mock_resp = _mock_response(200, {"trading_active": True, "exchange_active": True})
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=mock_resp)

        status = await client.get_exchange_status()
        assert status.trading_active is True
        assert status.exchange_active is True


class TestMarketEndpoints:
    async def test_get_market(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "market": {
                "ticker": "KXBTC-26MAR-T50000",
                "event_ticker": "KXBTC-26MAR",
                "title": "BTC above 50000?",
                "status": "open",
                "yes_bid": 65,
                "yes_ask": 67,
            }
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        market = await client.get_market("KXBTC-26MAR-T50000")
        assert market.ticker == "KXBTC-26MAR-T50000"
        assert market.yes_bid == 65

    async def test_get_events(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "events": [
                {
                    "event_ticker": "KXBTC-26MAR",
                    "series_ticker": "KXBTC",
                    "title": "Bitcoin March",
                    "category": "Crypto",
                    "status": "open",
                    "markets": [],
                }
            ],
            "cursor": None,
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        events = await client.get_events()
        assert len(events) == 1
        assert events[0].event_ticker == "KXBTC-26MAR"

    async def test_get_events_with_min_close_ts(self, client: KalshiRESTClient) -> None:
        mock_data = {"events": [], "cursor": None}
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        await client.get_events(min_close_ts=1741800000)

        _, kwargs = client._http.request.call_args
        assert kwargs["params"]["min_close_ts"] == 1741800000

    async def test_get_event(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "event": {
                "event_ticker": "KXBTC-26MAR",
                "series_ticker": "KXBTC",
                "title": "Bitcoin March",
                "category": "Crypto",
                "status": "open",
                "markets": [],
            }
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        event = await client.get_event("KXBTC-26MAR")
        assert event.event_ticker == "KXBTC-26MAR"

    async def test_get_orderbook(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "orderbook": {
                "market_ticker": "KXBTC-26MAR-T50000",
                "yes": [[65, 100], [64, 200]],
                "no": [[35, 150]],
            }
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        ob = await client.get_orderbook("KXBTC-26MAR-T50000")
        assert ob.market_ticker == "KXBTC-26MAR-T50000"
        assert len(ob.yes) == 2

    async def test_get_trades(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "trades": [
                {
                    "ticker": "KXBTC-26MAR-T50000",
                    "trade_id": "t1",
                    "price": 65,
                    "count": 10,
                    "side": "yes",
                    "created_time": "2026-03-03T12:00:00Z",
                }
            ],
            "cursor": None,
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        trades = await client.get_trades("KXBTC-26MAR-T50000")
        assert len(trades) == 1
        assert trades[0].price == 65

    async def test_get_series(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "series": {
                "series_ticker": "KXBTC",
                "title": "Bitcoin Prices",
                "category": "Crypto",
                "tags": ["bitcoin"],
            }
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        series = await client.get_series("KXBTC")
        assert series.series_ticker == "KXBTC"


class TestOrderEndpoints:
    async def test_create_order(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "order": {
                "order_id": "ord-123",
                "ticker": "KXBTC-26MAR-T50000",
                "action": "buy",
                "side": "yes",
                "type": "limit",
                "yes_price": 65,
                "no_price": 35,
                "initial_count": 10,
                "remaining_count": 10,
                "fill_count": 0,
                "status": "resting",
                "created_time": "2026-03-03T12:00:00Z",
            }
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        order = await client.create_order(
            ticker="KXBTC-26MAR-T50000",
            action="buy",
            side="yes",
            yes_price=65,
            count=10,
        )
        assert order.order_id == "ord-123"
        # Verify POST body
        call_kwargs = client._http.request.call_args
        assert call_kwargs.kwargs["json"]["ticker"] == "KXBTC-26MAR-T50000"
        assert call_kwargs.kwargs["json"]["yes_price_dollars"] == "0.65"
        assert call_kwargs.kwargs["json"]["action"] == "buy"
        assert call_kwargs.kwargs["json"]["post_only"] is True  # default

    async def test_create_order_post_only_false(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "order": {
                "order_id": "ord-taker",
                "ticker": "MKT-1",
                "action": "buy",
                "side": "no",
                "type": "limit",
                "no_price": 40,
                "initial_count": 10,
                "remaining_count": 0,
                "fill_count": 10,
                "status": "executed",
            }
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        await client.create_order(
            ticker="MKT-1",
            side="no",
            no_price=40,
            count=10,
            post_only=False,
        )
        call_kwargs = client._http.request.call_args
        assert call_kwargs.kwargs["json"]["post_only"] is False

    async def test_cancel_order(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "order": {
                "order_id": "ord-123",
                "ticker": "KXBTC-26MAR-T50000",
                "action": "buy",
                "side": "yes",
                "type": "limit",
                "yes_price": 65,
                "no_price": 35,
                "initial_count": 10,
                "remaining_count": 0,
                "fill_count": 0,
                "status": "canceled",
                "created_time": "2026-03-03T12:00:00Z",
            }
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        order = await client.cancel_order("ord-123")
        assert order.status == "canceled"

    async def test_get_orders(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "orders": [
                {
                    "order_id": "ord-1",
                    "ticker": "KXBTC-26MAR-T50000",
                    "action": "buy",
                    "side": "yes",
                    "type": "limit",
                    "yes_price": 65,
                    "no_price": 35,
                    "initial_count": 10,
                    "remaining_count": 10,
                    "fill_count": 0,
                    "status": "resting",
                    "created_time": "2026-03-03T12:00:00Z",
                }
            ],
            "cursor": None,
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        orders = await client.get_orders()
        assert len(orders) == 1

    async def test_get_orders_with_event_ticker_filter(self, client: KalshiRESTClient) -> None:
        mock_data = {"orders": [], "cursor": None}
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        await client.get_orders(event_ticker="EVT-A,EVT-B")

        _, kwargs = client._http.request.call_args
        assert kwargs["params"]["event_ticker"] == "EVT-A,EVT-B"

    async def test_amend_order(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "old_order": {
                "order_id": "ord-123",
                "ticker": "KXBTC-26MAR-T50000",
                "action": "buy",
                "side": "no",
                "type": "limit",
                "yes_price": 30,
                "no_price": 70,
                "initial_count": 10,
                "remaining_count": 10,
                "fill_count": 0,
                "status": "resting",
                "created_time": "2026-03-03T12:00:00Z",
            },
            "order": {
                "order_id": "ord-123",
                "ticker": "KXBTC-26MAR-T50000",
                "action": "buy",
                "side": "no",
                "type": "limit",
                "yes_price": 25,
                "no_price": 75,
                "initial_count": 10,
                "remaining_count": 10,
                "fill_count": 0,
                "status": "resting",
                "created_time": "2026-03-03T12:00:00Z",
            },
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        old_order, amended_order = await client.amend_order(
            "ord-123", ticker="KXBTC-26MAR-T50000", no_price=75, count=10
        )
        assert old_order.order_id == "ord-123"
        assert old_order.no_price == 70
        assert amended_order.order_id == "ord-123"
        assert amended_order.no_price == 75
        call_kwargs = client._http.request.call_args
        body = call_kwargs.kwargs["json"]
        assert body["ticker"] == "KXBTC-26MAR-T50000"
        assert body["side"] == "no"
        assert body["action"] == "buy"
        assert body["no_price_dollars"] == "0.75"
        assert body["count_fp"] == "10"

    async def test_amend_order_partial_fields(self, client: KalshiRESTClient) -> None:
        """Only optional fields are sent when specified; required fields always present."""
        mock_data = {
            "old_order": {
                "order_id": "ord-123",
                "ticker": "KXBTC-26MAR-T50000",
                "action": "buy",
                "side": "no",
                "type": "limit",
                "yes_price": 35,
                "no_price": 65,
                "initial_count": 10,
                "remaining_count": 10,
                "fill_count": 0,
                "status": "resting",
                "created_time": "2026-03-03T12:00:00Z",
            },
            "order": {
                "order_id": "ord-123",
                "ticker": "KXBTC-26MAR-T50000",
                "action": "buy",
                "side": "no",
                "type": "limit",
                "yes_price": 35,
                "no_price": 65,
                "initial_count": 10,
                "remaining_count": 10,
                "fill_count": 0,
                "status": "resting",
                "created_time": "2026-03-03T12:00:00Z",
            },
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        await client.amend_order("ord-123", ticker="KXBTC-26MAR-T50000", no_price=65)
        call_kwargs = client._http.request.call_args
        body = call_kwargs.kwargs["json"]
        # Required fields always present
        assert body["ticker"] == "KXBTC-26MAR-T50000"
        assert body["side"] == "no"
        assert body["action"] == "buy"
        # Optional: only no_price sent, count omitted
        assert body["no_price_dollars"] == "0.65"
        assert "count_fp" not in body

    async def test_get_order(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "order": {
                "order_id": "ord-456",
                "ticker": "KXBTC-26MAR-T50000",
                "action": "buy",
                "side": "yes",
                "type": "limit",
                "yes_price": 65,
                "no_price": 35,
                "initial_count": 5,
                "remaining_count": 3,
                "fill_count": 2,
                "status": "resting",
                "created_time": "2026-03-03T12:00:00Z",
            }
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        order = await client.get_order("ord-456")
        assert order.order_id == "ord-456"
        assert order.fill_count == 2

    async def test_batch_create_orders(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "orders": [
                {"order_id": "ord-1", "success": True},
                {"order_id": "ord-2", "success": True},
            ]
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        orders_input = [
            {"ticker": "MKT-A", "action": "buy", "side": "no", "no_price": 45, "count": 10},
            {"ticker": "MKT-B", "action": "buy", "side": "no", "no_price": 50, "count": 10},
        ]
        results = await client.batch_create_orders(orders_input)
        assert len(results) == 2
        assert results[0].success is True
        assert results[1].order_id == "ord-2"
        call_kwargs = client._http.request.call_args
        assert call_kwargs.kwargs["json"]["orders"] == orders_input

    async def test_batch_cancel_orders(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "orders": [
                {"order_id": "ord-1", "success": True},
                {"order_id": "ord-2", "success": False, "error": "not found"},
            ]
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        results = await client.batch_cancel_orders(["ord-1", "ord-2"])
        assert len(results) == 2
        assert results[0].success is True
        assert results[1].success is False
        assert results[1].error == "not found"
        call_kwargs = client._http.request.call_args
        assert call_kwargs.kwargs["json"]["order_ids"] == ["ord-1", "ord-2"]


class TestQueuePositions:
    async def test_get_queue_positions_prefers_fp(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "queue_positions": [
                {"order_id": "ord-1", "queue_position": 10, "queue_position_fp": "5.00"},
                {"order_id": "ord-2", "queue_position": 20},
            ]
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        result = await client.get_queue_positions()
        assert result["ord-1"] == 5  # prefers _fp (string → float → int)
        assert result["ord-2"] == 20  # falls back to queue_position

    async def test_small_fp_rounds_up_to_one(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "queue_positions": [
                {"order_id": "ord-1", "queue_position": 0, "queue_position_fp": "0.48"},
            ]
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        result = await client.get_queue_positions()
        assert result["ord-1"] == 1  # small positive fp → at least 1

    async def test_get_queue_positions_alternate_response_key(
        self, client: KalshiRESTClient
    ) -> None:
        mock_data = {
            "data": [
                {"order_id": "ord-1", "queue_position": 15},
            ]
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        result = await client.get_queue_positions()
        assert result["ord-1"] == 15

    async def test_get_queue_positions_with_market_tickers(self, client: KalshiRESTClient) -> None:
        mock_data = {"queue_positions": []}
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        await client.get_queue_positions(market_tickers=["MKT-A", "MKT-B"])
        call_kwargs = client._http.request.call_args
        assert call_kwargs.kwargs["params"]["market_tickers"] == "MKT-A,MKT-B"


class TestPortfolioEndpoints:
    async def test_get_balance(self, client: KalshiRESTClient) -> None:
        mock_data = {"balance": 500000, "portfolio_value": 750000}
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        balance = await client.get_balance()
        assert balance.balance == 500000

    async def test_get_positions(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "market_positions": [
                {
                    "ticker": "KXBTC-26MAR-T50000",
                    "position": 10,
                    "total_traded": 25,
                    "market_exposure": 650,
                }
            ],
            "cursor": None,
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        positions = await client.get_positions()
        assert len(positions) == 1
        assert positions[0].position == 10

    async def test_get_fills(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "fills": [
                {
                    "trade_id": "t1",
                    "order_id": "ord-1",
                    "ticker": "KXBTC-26MAR-T50000",
                    "side": "yes",
                    "price": 65,
                    "count": 5,
                    "created_time": "2026-03-03T12:01:00Z",
                }
            ],
            "cursor": None,
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        fills = await client.get_fills()
        assert len(fills) == 1
        assert fills[0].count == 5


class TestGetSettlements:
    async def test_get_settlements_parses_response(self, client: KalshiRESTClient):
        mock_data = {
            "settlements": [
                {
                    "ticker": "MKT-YES",
                    "event_ticker": "EVT-1",
                    "market_result": "yes",
                    "revenue": 500,
                    "fee_cost": "0.0770",
                    "yes_count_fp": "10",
                    "no_count_fp": "0",
                    "yes_total_cost_dollars": "4.50",
                    "no_total_cost_dollars": "0.00",
                    "settled_time": "2026-03-12T00:00:00Z",
                }
            ],
            "cursor": None,
        }
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        settlements = await client.get_settlements()
        assert len(settlements) == 1
        s = settlements[0]
        assert s.ticker == "MKT-YES"
        assert s.event_ticker == "EVT-1"
        assert s.market_result == "yes"
        assert s.revenue == 500  # Already cents int — unchanged
        assert s.fee_cost == 8  # "0.0770" → round(7.70) = 8 cents
        assert s.yes_count == 10
        assert s.no_count == 0
        assert s.yes_total_cost == 450  # "4.50" → 450 cents
        assert s.no_total_cost == 0

    async def test_get_settlements_with_event_ticker_filter(self, client: KalshiRESTClient):
        mock_data = {"settlements": [], "cursor": None}
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_mock_response(200, mock_data))

        await client.get_settlements(event_ticker="EVT-1")

        _, kwargs = client._http.request.call_args
        assert kwargs["params"]["event_ticker"] == "EVT-1"
