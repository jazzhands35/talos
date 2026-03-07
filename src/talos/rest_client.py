"""Async REST client for the Kalshi trading API."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import structlog

from talos.auth import KalshiAuth
from talos.config import KalshiConfig
from talos.errors import KalshiAPIError, KalshiRateLimitError
from talos.models.market import Event, Market, OrderBook, Series, Trade
from talos.models.order import BatchOrderResult, Fill, Order
from talos.models.portfolio import Balance, ExchangeStatus, Position

logger = structlog.get_logger()


class KalshiRESTClient:
    """Async HTTP client for Kalshi REST API endpoints."""

    def __init__(self, auth: KalshiAuth, config: KalshiConfig) -> None:
        self._auth = auth
        self._base_url = config.rest_base_url
        self._http = httpx.AsyncClient()

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send an authenticated request and return the JSON response."""
        url = f"{self._base_url}{path}"
        headers = self._auth.headers(method, f"/trade-api/v2{path}")

        response = await self._http.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=json,
        )

        logger.debug(
            "kalshi_api_response",
            method=method,
            path=path,
            status=response.status_code,
            body=response.text[:1000],
        )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise KalshiRateLimitError(retry_after=float(retry_after) if retry_after else None)

        if response.status_code >= 400:
            body = response.json() if response.text else None
            raise KalshiAPIError(
                status_code=response.status_code,
                body=body,
            )

        return response.json()

    # --- Exchange ---

    async def get_exchange_status(self) -> ExchangeStatus:
        data = await self._request("GET", "/exchange/status")
        return ExchangeStatus.model_validate(data)

    # --- Market Data ---

    async def get_market(self, ticker: str) -> Market:
        data = await self._request("GET", f"/markets/{ticker}")
        return Market.model_validate(data["market"])

    async def get_events(
        self,
        *,
        status: str | None = None,
        series_ticker: str | None = None,
        with_nested_markets: bool = False,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[Event]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/events", params=params)
        return [Event.model_validate(e) for e in data["events"]]

    async def get_event(self, event_ticker: str, *, with_nested_markets: bool = False) -> Event:
        params: dict[str, Any] = {}
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        data = await self._request("GET", f"/events/{event_ticker}", params=params)
        return Event.model_validate(data["event"])

    async def get_series(self, series_ticker: str) -> Series:
        data = await self._request("GET", f"/series/{series_ticker}")
        return Series.model_validate(data["series"])

    async def get_orderbook(self, ticker: str, *, depth: int = 0) -> OrderBook:
        params: dict[str, Any] = {}
        if depth > 0:
            params["depth"] = depth
        data = await self._request("GET", f"/markets/{ticker}/orderbook", params=params)
        return OrderBook.model_validate(data["orderbook"])

    async def get_trades(
        self,
        ticker: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[Trade]:
        params: dict[str, Any] = {"ticker": ticker, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/markets/trades", params=params)
        return [Trade.model_validate(t) for t in data["trades"]]

    # --- Orders ---

    async def create_order(
        self,
        *,
        ticker: str,
        action: str = "buy",
        side: str,
        order_type: str = "limit",
        no_price: int | None = None,
        yes_price: int | None = None,
        count: int,
    ) -> Order:
        body: dict[str, Any] = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "type": order_type,
            "count": count,
            "client_order_id": str(uuid.uuid4()),
        }
        if no_price is not None:
            body["no_price"] = no_price
        if yes_price is not None:
            body["yes_price"] = yes_price
        logger.info(
            "create_order",
            ticker=ticker,
            action=action,
            side=side,
            price=no_price or yes_price,
            count=count,
        )
        data = await self._request("POST", "/portfolio/orders", json=body)
        return Order.model_validate(data["order"])

    async def cancel_order(self, order_id: str) -> Order:
        data = await self._request("DELETE", f"/portfolio/orders/{order_id}")
        return Order.model_validate(data["order"])

    async def amend_order(
        self,
        order_id: str,
        *,
        new_price: int | None = None,
        new_count: int | None = None,
    ) -> Order:
        body: dict[str, Any] = {}
        if new_price is not None:
            body["new_price"] = new_price
        if new_count is not None:
            body["new_count"] = new_count
        data = await self._request("POST", f"/portfolio/orders/{order_id}/amend", json=body)
        return Order.model_validate(data["order"])

    async def batch_create_orders(self, orders: list[dict[str, Any]]) -> list[BatchOrderResult]:
        data = await self._request("POST", "/portfolio/orders/batched", json={"orders": orders})
        return [BatchOrderResult.model_validate(r) for r in data["orders"]]

    async def batch_cancel_orders(self, order_ids: list[str]) -> list[BatchOrderResult]:
        data = await self._request(
            "DELETE", "/portfolio/orders/batched", json={"order_ids": order_ids}
        )
        return [BatchOrderResult.model_validate(r) for r in data["orders"]]

    async def get_orders(
        self,
        *,
        ticker: str | None = None,
        status: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[Order]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/portfolio/orders", params=params)
        return [Order.model_validate(o) for o in data["orders"]]

    async def get_order(self, order_id: str) -> Order:
        data = await self._request("GET", f"/portfolio/orders/{order_id}")
        return Order.model_validate(data["order"])

    async def get_queue_positions(
        self,
        *,
        event_ticker: str | None = None,
        market_tickers: list[str] | None = None,
    ) -> dict[str, int]:
        """Fetch queue positions for resting orders. Returns {order_id: position}.

        Prefers ``queue_position_fp`` (dollar-denominated) over ``queue_position``
        when both are present.  Handles alternate response keys across API versions.
        """
        params: dict[str, Any] = {}
        if event_ticker:
            params["event_ticker"] = event_ticker
        if market_tickers:
            params["market_tickers"] = ",".join(market_tickers)
        data = await self._request("GET", "/portfolio/orders/queue_positions", params=params)
        items = data.get("queue_positions") or data.get("data") or data.get("results") or []
        result: dict[str, int] = {}
        for qp in items:
            oid = qp.get("order_id", "")
            fp = qp.get("queue_position_fp")
            if fp is not None:
                fp_val = float(fp)
                pos = max(1, round(fp_val)) if fp_val > 0 else 0
            else:
                pos = qp.get("queue_position", 0)
            result[oid] = pos
        logger.debug(
            "queue_positions_parsed",
            count=len(result),
            raw_items=len(items),
            sample=dict(list(result.items())[:3]),
        )
        return result

    # --- Portfolio ---

    async def get_balance(self) -> Balance:
        data = await self._request("GET", "/portfolio/balance")
        return Balance.model_validate(data)

    async def get_positions(
        self,
        *,
        ticker: str | None = None,
        event_ticker: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[Position]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/portfolio/positions", params=params)
        return [Position.model_validate(p) for p in data["market_positions"]]

    async def get_fills(
        self,
        *,
        ticker: str | None = None,
        order_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[Fill]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if order_id:
            params["order_id"] = order_id
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/portfolio/fills", params=params)
        return [Fill.model_validate(f) for f in data["fills"]]
