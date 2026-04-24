"""Async REST client for the Kalshi trading API."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from talos.auth import KalshiAuth
from talos.config import KalshiConfig
from talos.errors import KalshiAPIError, KalshiNotFoundError, KalshiRateLimitError
from talos.models.market import Event, Market, OrderBook, Series, Trade
from talos.models.order import BatchOrderResult, Fill, Order
from talos.models.portfolio import Balance, EventPosition, ExchangeStatus, Position, Settlement
from talos.units import (
    MAX_FILLS_PAGES,
    ONE_CENT_BPS,
    ONE_CONTRACT_FP100,
    bps_to_dollars_str,
    fp100_to_fp_str,
)

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class FillsPage:
    """Single page of fills with next-page cursor.

    Unlike the hot-path :meth:`KalshiRESTClient.get_fills` (which drops
    the cursor), this carries the next-page cursor so the reconcile
    path can exhaust the chain.
    """

    fills: list[Fill]
    cursor: str | None  # None iff this is the last page


class KalshiRESTClient:
    """Async HTTP client for Kalshi REST API endpoints."""

    def __init__(self, auth: KalshiAuth, config: KalshiConfig) -> None:
        self._auth = auth
        self._base_url = config.rest_base_url
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, pool=60.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
        # Global concurrency limiter — prevents pool exhaustion at 1000+ tickers
        self._sem = asyncio.Semaphore(20)

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

        async with self._sem:
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
            body: dict[str, Any] | str | None = None
            if response.text:
                try:
                    body = response.json()
                except (ValueError, UnicodeDecodeError):
                    # CloudFront/nginx may return HTML or plain text on 5xx
                    body = response.text[:500]
            if response.status_code == 404:
                raise KalshiNotFoundError(body=body)
            raise KalshiAPIError(
                status_code=response.status_code,
                body=body,
            )

        try:
            return response.json()
        except (ValueError, UnicodeDecodeError) as err:
            # CloudFront/proxy can return HTML 200s (maintenance, redirects)
            raise KalshiAPIError(
                status_code=response.status_code,
                body=response.text[:500],
            ) from err

    # --- Exchange ---

    async def get_exchange_status(self) -> ExchangeStatus:
        data = await self._request("GET", "/exchange/status")
        return ExchangeStatus.model_validate(data)

    # --- Market Data ---

    async def get_market(self, ticker: str) -> Market:
        data = await self._request("GET", f"/markets/{ticker}")
        return Market.model_validate(data["market"])

    async def get_markets(
        self,
        *,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        tickers: list[str] | None = None,
        min_close_ts: int | None = None,
        max_close_ts: int | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[Market]:
        """Fetch a list of markets with full per-market data.

        Use this (not get_events with with_nested_markets=True) whenever
        you need volume_24h, last_price, or other per-market fields.
        Kalshi's /events?with_nested_markets=true response strips
        volume_24h on nested markets — discovered the hard way for the
        hurricane series. /markets returns full Market objects via
        Pydantic validation, so volume_24h_fp is parsed correctly.

        Single page only — pass cursor for pagination if needed.
        """
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        if tickers:
            params["tickers"] = ",".join(tickers)
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts
        if max_close_ts is not None:
            params["max_close_ts"] = max_close_ts
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/markets", params=params)
        return [Market.model_validate(m) for m in data["markets"]]

    async def get_events(
        self,
        *,
        status: str | None = None,
        series_ticker: str | None = None,
        with_nested_markets: bool = False,
        min_close_ts: int | None = None,
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
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/events", params=params)
        return [Event.model_validate(e) for e in data["events"]]

    async def get_events_raw(
        self,
        *,
        status: str | None = None,
        series_ticker: str | None = None,
        with_nested_markets: bool = False,
        min_close_ts: int | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Authenticated /events call returning the raw API response dict.

        Used by DiscoveryService, which needs fields the Event Pydantic
        model doesn't carry (e.g. close_time on events, dollars-form price
        fields on markets). Auth + rate-limit handling is inherited from
        _request; just skips Pydantic validation.
        """
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/events", params=params)

    async def get_all_events(
        self,
        *,
        status: str | None = None,
        series_ticker: str | None = None,
        with_nested_markets: bool = False,
        min_close_ts: int | None = None,
        page_size: int = 200,
        max_pages: int = 20,
    ) -> list[Event]:
        """Fetch all events by paginating through cursor-based results.

        Stops when the cursor is empty, fewer results than page_size are
        returned, or max_pages is reached (safeguard against runaway queries).
        """
        all_events: list[Event] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": page_size}
            if status:
                params["status"] = status
            if series_ticker:
                params["series_ticker"] = series_ticker
            if with_nested_markets:
                params["with_nested_markets"] = "true"
            if min_close_ts is not None:
                params["min_close_ts"] = min_close_ts
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/events", params=params)
            events = [Event.model_validate(e) for e in data["events"]]
            all_events.extend(events)
            cursor = data.get("cursor")
            if not cursor:
                break
        return all_events

    async def get_event(self, event_ticker: str, *, with_nested_markets: bool = False) -> Event:
        params: dict[str, Any] = {}
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        data = await self._request("GET", f"/events/{event_ticker}", params=params)
        return Event.model_validate(data["event"])

    async def get_series(self, series_ticker: str) -> Series:
        data = await self._request("GET", f"/series/{series_ticker}")
        payload = dict(data["series"])
        payload.setdefault("series_ticker", series_ticker)
        return Series.model_validate(payload)

    async def get_fee_schedule(
        self, series_ticker: str, *, show_historical: bool = False
    ) -> list[dict[str, Any]]:
        """Fetch fee change schedule for a series."""
        params: dict[str, Any] = {"series_ticker": series_ticker}
        if show_historical:
            params["show_historical"] = "true"
        data = await self._request("GET", "/series/fee_changes", params=params)
        return data.get("fee_changes", [])

    async def get_orderbook(self, ticker: str, *, depth: int = 0) -> OrderBook:
        params: dict[str, Any] = {}
        if depth > 0:
            params["depth"] = depth
        data = await self._request("GET", f"/markets/{ticker}/orderbook", params=params)
        # Post March 12: response may use "orderbook_fp" key instead of "orderbook"
        book_data = data.get("orderbook") or data.get("orderbook_fp", {})
        return OrderBook.model_validate(book_data)

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
        no_price_bps: int | None = None,
        yes_price_bps: int | None = None,
        count: int | None = None,
        count_fp100: int | None = None,
        post_only: bool = True,
        order_group_id: str | None = None,
    ) -> Order:
        # Resolve effective bps/fp100 values. When both legacy and new params
        # are passed, the more-precise (_bps / _fp100) form wins.
        if no_price_bps is None and no_price is not None:
            no_price_bps = no_price * ONE_CENT_BPS
        if yes_price_bps is None and yes_price is not None:
            yes_price_bps = yes_price * ONE_CENT_BPS
        if count_fp100 is None:
            if count is None:
                raise ValueError("create_order requires either count or count_fp100")
            count_fp100 = count * ONE_CONTRACT_FP100

        body: dict[str, Any] = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "type": order_type,
            "count_fp": fp100_to_fp_str(count_fp100),
            "client_order_id": str(uuid.uuid4()),
            "post_only": post_only,
        }
        if order_group_id is not None:
            body["order_group_id"] = order_group_id
        if no_price_bps is not None:
            body["no_price_dollars"] = bps_to_dollars_str(no_price_bps)
        if yes_price_bps is not None:
            body["yes_price_dollars"] = bps_to_dollars_str(yes_price_bps)
        logger.info(
            "create_order",
            ticker=ticker,
            action=action,
            side=side,
            price=no_price or yes_price,
            count=count,
            price_bps=no_price_bps if no_price_bps is not None else yes_price_bps,
            count_fp100=count_fp100,
        )
        data = await self._request("POST", "/portfolio/orders", json=body)
        return Order.model_validate(data["order"])

    async def decrease_order(
        self,
        order_id: str,
        *,
        reduce_by: int | None = None,
        reduce_to: int | None = None,
        reduce_by_fp100: int | None = None,
        reduce_to_fp100: int | None = None,
    ) -> Order:
        """Reduce an order's quantity without losing queue position.

        Exactly one of ``reduce_by`` / ``reduce_by_fp100`` /
        ``reduce_to`` / ``reduce_to_fp100`` must be provided.

        The ``_fp100`` variants carry fractional-contract precision; when
        both legacy (whole-contract) and new params are passed, the
        ``_fp100`` form wins.
        """
        if reduce_by_fp100 is None and reduce_by is not None:
            reduce_by_fp100 = reduce_by * ONE_CONTRACT_FP100
        if reduce_to_fp100 is None and reduce_to is not None:
            reduce_to_fp100 = reduce_to * ONE_CONTRACT_FP100
        body: dict[str, Any] = {}
        if reduce_by_fp100 is not None:
            body["reduce_by_fp"] = fp100_to_fp_str(reduce_by_fp100)
        if reduce_to_fp100 is not None:
            body["reduce_to_fp"] = fp100_to_fp_str(reduce_to_fp100)
        data = await self._request("POST", f"/portfolio/orders/{order_id}/decrease", json=body)
        return Order.model_validate(data["order"])

    async def cancel_order(self, order_id: str) -> Order:
        data = await self._request("DELETE", f"/portfolio/orders/{order_id}")
        return Order.model_validate(data["order"])

    async def amend_order(
        self,
        order_id: str,
        *,
        ticker: str,
        side: str = "no",
        action: str = "buy",
        no_price: int | None = None,
        yes_price: int | None = None,
        no_price_bps: int | None = None,
        yes_price_bps: int | None = None,
        count: int | None = None,
        count_fp100: int | None = None,
    ) -> tuple[Order, Order]:
        """Amend an existing order's price and/or quantity.

        For partially filled orders, ``count`` is the total
        (``fill_count + remaining_count``), and only the unfilled
        portion moves to the new price queue.

        When both legacy (cents / whole-contract) and new
        (``_bps`` / ``_fp100``) params are passed for the same field,
        the more-precise form wins.

        Returns ``(old_order, amended_order)``.
        """
        # Resolve effective bps/fp100 values.
        if no_price_bps is None and no_price is not None:
            no_price_bps = no_price * ONE_CENT_BPS
        if yes_price_bps is None and yes_price is not None:
            yes_price_bps = yes_price * ONE_CENT_BPS
        if count_fp100 is None and count is not None:
            count_fp100 = count * ONE_CONTRACT_FP100

        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": action,
        }
        if no_price_bps is not None:
            body["no_price_dollars"] = bps_to_dollars_str(no_price_bps)
        if yes_price_bps is not None:
            body["yes_price_dollars"] = bps_to_dollars_str(yes_price_bps)
        if count_fp100 is not None:
            body["count_fp"] = fp100_to_fp_str(count_fp100)
        data = await self._request("POST", f"/portfolio/orders/{order_id}/amend", json=body)
        return (
            Order.model_validate(data["old_order"]),
            Order.model_validate(data["order"]),
        )

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
        event_ticker: str | None = None,
        status: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[Order]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/portfolio/orders", params=params)
        return [Order.model_validate(o) for o in data["orders"]]

    async def get_all_orders(
        self,
        *,
        ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        page_size: int = 200,
    ) -> list[Order]:
        """Fetch ALL orders by paginating through cursor-based results."""
        all_orders: list[Order] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": page_size}
            if ticker:
                params["ticker"] = ticker
            if event_ticker:
                params["event_ticker"] = event_ticker
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/portfolio/orders", params=params)
            orders = [Order.model_validate(o) for o in data["orders"]]
            all_orders.extend(orders)
            cursor = data.get("cursor")
            if not cursor or len(orders) < page_size:
                break
        return all_orders

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

        When ``market_tickers`` exceeds 50, batches into multiple requests to
        avoid CloudFront's URI length limit (414 error on ~8K+ URLs).
        """
        if market_tickers and len(market_tickers) > 50:
            result: dict[str, int] = {}
            for i in range(0, len(market_tickers), 50):
                chunk = market_tickers[i : i + 50]
                batch = await self._get_queue_positions_single(
                    event_ticker=event_ticker, market_tickers=chunk
                )
                result.update(batch)
            return result
        return await self._get_queue_positions_single(
            event_ticker=event_ticker, market_tickers=market_tickers
        )

    async def _get_queue_positions_single(
        self,
        *,
        event_ticker: str | None = None,
        market_tickers: list[str] | None = None,
    ) -> dict[str, int]:
        """Single batch queue position fetch (≤50 tickers)."""
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

    # --- Order Groups ---

    async def create_order_group(
        self,
        name: str,
        contracts_limit: int | None = None,
        *,
        contracts_limit_fp100: int | None = None,
    ) -> str:
        """Create an order group with a fill limit. Returns the order_group_id.

        Accepts either a legacy whole-contract ``contracts_limit`` OR the
        exact-precision ``contracts_limit_fp100`` kwarg (1 contract = 100
        fp100). When both are passed, fp100 wins — matches the resolution
        rule on :meth:`create_order` / :meth:`amend_order` / :meth:`decrease_order`
        (migration commit 03dd771).

        Wire format: the ``contracts_limit_fp`` wire field is a fixed-point
        dollars-style string (``"5.00"``, not ``"5"``), matching Kalshi's
        documented schema. Pre-migration the serializer was ``str(N)`` which
        emitted ``"5"`` — Kalshi accepted both, but spec-exact form is
        strictly more correct and matches every other ``_fp`` field Talos
        sends post-migration.
        """
        if contracts_limit_fp100 is None:
            if contracts_limit is None:
                raise ValueError(
                    "create_order_group requires either contracts_limit or contracts_limit_fp100"
                )
            contracts_limit_fp100 = contracts_limit * ONE_CONTRACT_FP100
        data = await self._request(
            "POST",
            "/portfolio/order_groups",
            json={"name": name, "contracts_limit_fp": fp100_to_fp_str(contracts_limit_fp100)},
        )
        return data["order_group"]["order_group_id"]

    async def get_order_groups(self) -> list[dict[str, Any]]:
        """List active order groups."""
        data = await self._request("GET", "/portfolio/order_groups")
        return data.get("order_groups", [])

    async def delete_order_group(self, order_group_id: str) -> None:
        """Delete an order group."""
        await self._request("DELETE", f"/portfolio/order_groups/{order_group_id}")

    async def reset_order_group(self, order_group_id: str) -> None:
        """Reset an order group's matched contracts counter."""
        await self._request("POST", f"/portfolio/order_groups/{order_group_id}/reset")

    async def trigger_order_group(self, order_group_id: str) -> None:
        """Trigger an order group — cancels all orders in the group."""
        await self._request("POST", f"/portfolio/order_groups/{order_group_id}/trigger")

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

    async def get_all_positions(
        self,
        *,
        ticker: str | None = None,
        event_ticker: str | None = None,
        page_size: int = 200,
    ) -> list[Position]:
        """Fetch ALL positions by paginating through cursor-based results."""
        all_positions: list[Position] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": page_size}
            if ticker:
                params["ticker"] = ticker
            if event_ticker:
                params["event_ticker"] = event_ticker
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/portfolio/positions", params=params)
            positions = [Position.model_validate(p) for p in data["market_positions"]]
            all_positions.extend(positions)
            cursor = data.get("cursor")
            if not cursor or len(positions) < page_size:
                break
        return all_positions

    async def get_event_positions(self) -> list[EventPosition]:
        """Fetch event-level positions (events with fills or resting orders)."""
        data = await self._request("GET", "/portfolio/positions")
        return [EventPosition.model_validate(ep) for ep in data.get("event_positions", [])]

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

    async def get_fills_page(
        self,
        *,
        ticker: str | None = None,
        order_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> FillsPage:
        """Fetch one page of fills with the next-page cursor.

        Unlike :meth:`get_fills`, this returns the structured page so
        :meth:`get_all_fills` can exhaust the cursor chain.
        """
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if order_id:
            params["order_id"] = order_id
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/portfolio/fills", params=params)
        next_cursor = data.get("cursor") or None  # Kalshi returns "" on last page
        return FillsPage(
            fills=[Fill.model_validate(f) for f in data["fills"]],
            cursor=next_cursor,
        )

    async def get_all_fills(
        self,
        *,
        ticker: str | None = None,
        order_id: str | None = None,
    ) -> list[Fill]:
        """Exhaust all pages of fills. Raises on pagination overrun.

        Used by the reconcile path to rebuild authoritative ledger state
        from per-fill ground truth. The hot path uses :meth:`get_fills`,
        which returns a single page.

        Raises:
            KalshiAPIError: if pagination exceeds :data:`MAX_FILLS_PAGES`.
        """
        all_fills: list[Fill] = []
        cursor: str | None = None
        pages = 0
        while True:
            page = await self.get_fills_page(ticker=ticker, order_id=order_id, cursor=cursor)
            all_fills.extend(page.fills)
            pages += 1
            cursor = page.cursor
            if cursor is None:
                break
            if pages >= MAX_FILLS_PAGES:
                message = (
                    f"get_all_fills exceeded MAX_FILLS_PAGES={MAX_FILLS_PAGES} "
                    f"(ticker={ticker!r}, order_id={order_id!r}) — abort reconcile"
                )
                # Client-side pagination guard — no HTTP response; status_code=0
                # signals a synthetic (non-transport) error.
                raise KalshiAPIError(status_code=0, body=None, message=message)
        logger.info(
            "get_all_fills_complete",
            ticker=ticker,
            order_id=order_id,
            pages=pages,
            fills=len(all_fills),
        )
        return all_fills

    async def get_settlements(
        self,
        *,
        ticker: str | None = None,
        event_ticker: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[Settlement]:
        """Fetch settlement history (single page).

        Settlements provide Kalshi's authoritative P&L (P7/P21).
        Note: ``revenue`` is cents int, ``fee_cost`` is dollars string in the response.
        """
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/portfolio/settlements", params=params)
        return [Settlement.model_validate(s) for s in data["settlements"]]

    async def get_all_settlements(
        self,
        *,
        page_size: int = 200,
    ) -> list[Settlement]:
        """Fetch ALL settlements by paginating through cursor-based results."""
        all_settlements: list[Settlement] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": page_size}
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/portfolio/settlements", params=params)
            settlements = [Settlement.model_validate(s) for s in data["settlements"]]
            all_settlements.extend(settlements)
            cursor = data.get("cursor")
            if not cursor or len(settlements) < page_size:
                break
        return all_settlements
