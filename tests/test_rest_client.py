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
                "side": "yes",
                "order_type": "limit",
                "price": 65,
                "count": 10,
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
            side="yes",
            order_type="limit",
            price=65,
            count=10,
        )
        assert order.order_id == "ord-123"
        # Verify POST body
        call_kwargs = client._http.request.call_args
        assert call_kwargs.kwargs["json"]["ticker"] == "KXBTC-26MAR-T50000"
        assert call_kwargs.kwargs["json"]["price"] == 65

    async def test_cancel_order(self, client: KalshiRESTClient) -> None:
        mock_data = {
            "order": {
                "order_id": "ord-123",
                "ticker": "KXBTC-26MAR-T50000",
                "side": "yes",
                "order_type": "limit",
                "price": 65,
                "count": 10,
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
                    "side": "yes",
                    "order_type": "limit",
                    "price": 65,
                    "count": 10,
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
