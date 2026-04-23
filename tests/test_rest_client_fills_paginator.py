"""Tests for the get_all_fills paginator (Task 9).

Covers:
  - Single-page exhaustion (Kalshi returns "" cursor on last page)
  - Multi-page exhaustion with cursor threading
  - MAX_FILLS_PAGES overrun raises KalshiAPIError
  - get_fills_page cursor threading + absence
  - ticker + order_id filters propagate to every request
  - Empty response
  - Null / missing cursor key termination

All tests patch ``_request`` via AsyncMock, mirroring the
``_build_test_client`` pattern from test_rest_client_wire_format.py.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.errors import KalshiAPIError
from talos.rest_client import FillsPage, KalshiRESTClient
from talos.units import MAX_FILLS_PAGES


def _build_test_client() -> KalshiRESTClient:
    """Construct a KalshiRESTClient that bypasses httpx/auth setup.

    Mirrors test_rest_client_wire_format.py: __new__ avoids opening a real
    httpx.AsyncClient or installing an auth object, since every test
    patches ``_request``.
    """
    client = KalshiRESTClient.__new__(KalshiRESTClient)
    client._http = MagicMock()
    client._auth = MagicMock()
    client._auth.headers = MagicMock(return_value={})
    client._base_url = "https://demo-api.kalshi.co/trade-api/v2"
    client._sem = asyncio.Semaphore(20)
    return client


def _fill_payload(trade_id: str = "trade-1", **overrides: Any) -> dict[str, Any]:
    """Minimal Fill wire payload using the post-March-12 dollars/fp format."""
    base: dict[str, Any] = {
        "trade_id": trade_id,
        "order_id": "ord-abc",
        "ticker": "KXBTC-26MAR-T50000",
        "side": "no",
        "yes_price_dollars": "0.40",
        "no_price_dollars": "0.60",
        "count_fp": "5",
        "fee_cost_dollars": "0.0130",
        "created_time": "2026-03-12T12:01:00Z",
    }
    base.update(overrides)
    return base


# ── get_all_fills ──────────────────────────────────────────────────


class TestGetAllFillsExhaustion:
    async def test_single_page_empty_cursor_terminates(self) -> None:
        """Kalshi returns ``""`` cursor on last page → single request, chain ends."""
        client = _build_test_client()
        f1 = _fill_payload("t1")
        request_mock = AsyncMock(return_value={"fills": [f1], "cursor": ""})
        client._request = request_mock  # type: ignore[method-assign]

        result = await client.get_all_fills()

        assert len(result) == 1
        assert result[0].trade_id == "t1"
        assert request_mock.call_count == 1

    async def test_multi_page_cursor_threading(self) -> None:
        """Three pages → cursor from each response threads into next request's params."""
        client = _build_test_client()
        pages = [
            {"fills": [_fill_payload("t1")], "cursor": "c2"},
            {"fills": [_fill_payload("t2")], "cursor": "c3"},
            {"fills": [_fill_payload("t3")], "cursor": ""},
        ]
        request_mock = AsyncMock(side_effect=pages)
        client._request = request_mock  # type: ignore[method-assign]

        result = await client.get_all_fills()

        assert [f.trade_id for f in result] == ["t1", "t2", "t3"]
        assert request_mock.call_count == 3

        # Page 1: no cursor in params.
        call1_params = request_mock.call_args_list[0].kwargs["params"]
        assert "cursor" not in call1_params

        # Page 2: cursor="c2".
        call2_params = request_mock.call_args_list[1].kwargs["params"]
        assert call2_params["cursor"] == "c2"

        # Page 3: cursor="c3".
        call3_params = request_mock.call_args_list[2].kwargs["params"]
        assert call3_params["cursor"] == "c3"

    async def test_max_pages_overrun_raises(self) -> None:
        """Infinite cursor chain must abort at MAX_FILLS_PAGES with KalshiAPIError."""
        client = _build_test_client()
        request_mock = AsyncMock(
            return_value={"fills": [_fill_payload("t-inf")], "cursor": "never-empty"},
        )
        client._request = request_mock  # type: ignore[method-assign]

        with pytest.raises(KalshiAPIError, match="MAX_FILLS_PAGES"):
            await client.get_all_fills()

        # We fetch exactly MAX_FILLS_PAGES pages before raising.
        assert request_mock.call_count == MAX_FILLS_PAGES

    async def test_empty_response(self) -> None:
        """Empty fills list + empty cursor → [] after one request."""
        client = _build_test_client()
        request_mock = AsyncMock(return_value={"fills": [], "cursor": ""})
        client._request = request_mock  # type: ignore[method-assign]

        result = await client.get_all_fills()

        assert result == []
        assert request_mock.call_count == 1

    async def test_null_cursor_terminates(self) -> None:
        """cursor=None in the response terminates the chain."""
        client = _build_test_client()
        request_mock = AsyncMock(return_value={"fills": [_fill_payload("t1")], "cursor": None})
        client._request = request_mock  # type: ignore[method-assign]

        result = await client.get_all_fills()

        assert len(result) == 1
        assert request_mock.call_count == 1

    async def test_missing_cursor_key_terminates(self) -> None:
        """No ``cursor`` key in the response terminates the chain."""
        client = _build_test_client()
        request_mock = AsyncMock(return_value={"fills": [_fill_payload("t1")]})
        client._request = request_mock  # type: ignore[method-assign]

        result = await client.get_all_fills()

        assert len(result) == 1
        assert request_mock.call_count == 1

    async def test_filters_propagate_to_every_page(self) -> None:
        """ticker + order_id must appear in every ``_request`` call's params."""
        client = _build_test_client()
        pages = [
            {"fills": [_fill_payload("t1")], "cursor": "c2"},
            {"fills": [_fill_payload("t2")], "cursor": ""},
        ]
        request_mock = AsyncMock(side_effect=pages)
        client._request = request_mock  # type: ignore[method-assign]

        await client.get_all_fills(ticker="T1-ABC", order_id="ord-123")

        assert request_mock.call_count == 2
        for call in request_mock.call_args_list:
            params = call.kwargs["params"]
            assert params["ticker"] == "T1-ABC"
            assert params["order_id"] == "ord-123"


# ── get_fills_page ─────────────────────────────────────────────────


class TestGetFillsPage:
    async def test_cursor_included_when_provided(self) -> None:
        """get_fills_page(cursor=...) threads the cursor into params."""
        client = _build_test_client()
        request_mock = AsyncMock(return_value={"fills": [_fill_payload()], "cursor": "next-c"})
        client._request = request_mock  # type: ignore[method-assign]

        page = await client.get_fills_page(cursor="abc")

        assert isinstance(page, FillsPage)
        assert page.cursor == "next-c"
        params = request_mock.call_args.kwargs["params"]
        assert params["cursor"] == "abc"

    async def test_cursor_omitted_when_absent(self) -> None:
        """get_fills_page() without cursor must NOT include a 'cursor' key in params."""
        client = _build_test_client()
        request_mock = AsyncMock(return_value={"fills": [_fill_payload()], "cursor": ""})
        client._request = request_mock  # type: ignore[method-assign]

        page = await client.get_fills_page()

        assert page.cursor is None  # empty string normalized to None
        params = request_mock.call_args.kwargs["params"]
        assert "cursor" not in params
