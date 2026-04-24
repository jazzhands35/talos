"""Wire-format tests for KalshiRESTClient outbound order payloads.

Covers the bps/fp100 migration (Task 8): create_order, amend_order, and
decrease_order accept _bps / _fp100 kwargs alongside the legacy cents /
whole-contract params. Wire serialization uses talos.units helpers:
  - bps_to_dollars_str: 2-decimal for whole cents, 4-decimal otherwise
  - fp100_to_fp_str: 2-decimal fp always

These tests patch ``_request`` and capture the body dict, rather than
exercising the HTTP path. That keeps the parametric matrix cheap and
isolates the wire-format contract from transport/auth concerns.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from talos.rest_client import KalshiRESTClient


def _build_test_client() -> KalshiRESTClient:
    """Construct a KalshiRESTClient that bypasses httpx/auth setup.

    __init__ opens a real httpx.AsyncClient and installs an auth object;
    neither is needed here because every test patches ``_request``. Using
    ``__new__`` keeps the fixture fast and side-effect-free.
    """
    client = KalshiRESTClient.__new__(KalshiRESTClient)
    client._http = MagicMock()
    client._auth = MagicMock()
    client._auth.headers = MagicMock(return_value={})
    client._base_url = "https://demo-api.kalshi.co/trade-api/v2"
    client._sem = asyncio.Semaphore(20)
    return client


def _order_response(**overrides: Any) -> dict[str, Any]:
    base = {
        "order_id": "ord-x",
        "ticker": "MKT-X",
        "action": "buy",
        "side": "no",
        "type": "limit",
        "status": "resting",
    }
    base.update(overrides)
    return {"order": base}


def _amend_response() -> dict[str, Any]:
    common = {
        "order_id": "ord-x",
        "ticker": "MKT-X",
        "action": "buy",
        "side": "no",
        "type": "limit",
        "status": "resting",
    }
    return {"old_order": dict(common), "order": dict(common)}


# ── create_order ───────────────────────────────────────────────────


class TestCreateOrderWireFormat:
    async def test_cent_tick_via_legacy_cents(self) -> None:
        """Legacy cents path → proven 2-decimal wire format (unchanged)."""
        client = _build_test_client()
        request_mock = AsyncMock(return_value=_order_response())
        client._request = request_mock  # type: ignore[method-assign]

        await client.create_order(
            ticker="T-CENT",
            action="buy",
            side="no",
            order_type="limit",
            count=5,
            no_price=53,
        )
        body = request_mock.call_args.kwargs["json"]
        assert body["no_price_dollars"] == "0.53"
        assert body["count_fp"] == "5.00"

    async def test_cent_tick_via_bps_path(self) -> None:
        """Bps path with whole-cent value → identical 2-decimal body."""
        client = _build_test_client()
        request_mock = AsyncMock(return_value=_order_response())
        client._request = request_mock  # type: ignore[method-assign]

        await client.create_order(
            ticker="T-CENT",
            action="buy",
            side="no",
            order_type="limit",
            count_fp100=500,
            no_price_bps=5300,
        )
        body = request_mock.call_args.kwargs["json"]
        assert body["no_price_dollars"] == "0.53"
        assert body["count_fp"] == "5.00"

    async def test_sub_cent_via_bps_path(self) -> None:
        """DJT-class sub-cent price → 4-decimal wire format."""
        client = _build_test_client()
        request_mock = AsyncMock(return_value=_order_response())
        client._request = request_mock  # type: ignore[method-assign]

        await client.create_order(
            ticker="KXDJT-YES",
            action="buy",
            side="yes",
            order_type="limit",
            count_fp100=500,
            no_price_bps=488,
        )
        body = request_mock.call_args.kwargs["json"]
        assert body["no_price_dollars"] == "0.0488"
        assert body["count_fp"] == "5.00"

    async def test_fractional_count_fp100(self) -> None:
        """Fractional fp100 count → 2-decimal wire format (189 fp100 → '1.89').

        Talos does not submit fractional today, but the serializer must
        handle it — callers may opt in after Task 12 relaxes Phase 0.
        """
        client = _build_test_client()
        request_mock = AsyncMock(return_value=_order_response())
        client._request = request_mock  # type: ignore[method-assign]

        await client.create_order(
            ticker="KXDJT-YES",
            action="buy",
            side="yes",
            order_type="limit",
            count_fp100=189,
            no_price_bps=4888,
        )
        body = request_mock.call_args.kwargs["json"]
        assert body["count_fp"] == "1.89"
        assert body["no_price_dollars"] == "0.4888"

    async def test_yes_price_sub_cent(self) -> None:
        """Sub-cent YES price routes through bps_to_dollars_str."""
        client = _build_test_client()
        request_mock = AsyncMock(return_value=_order_response())
        client._request = request_mock  # type: ignore[method-assign]

        await client.create_order(
            ticker="KXDJT-YES",
            action="buy",
            side="yes",
            order_type="limit",
            count_fp100=500,
            yes_price_bps=9512,
        )
        body = request_mock.call_args.kwargs["json"]
        assert body["yes_price_dollars"] == "0.9512"
        assert "no_price_dollars" not in body

    async def test_missing_count_raises(self) -> None:
        """Neither count nor count_fp100 supplied → ValueError."""
        client = _build_test_client()

        with pytest.raises(ValueError, match="count or count_fp100"):
            await client.create_order(
                ticker="MKT-X",
                action="buy",
                side="no",
                order_type="limit",
                no_price_bps=5300,
            )

    async def test_bps_wins_when_both_passed(self) -> None:
        """When both no_price and no_price_bps are given, bps wins.

        Rationale: _bps carries sub-cent precision, so it's the more
        exact specification. A caller passing both is presumed to mean
        "use the bps value I just computed; the cents value is stale
        or a display artifact."
        """
        client = _build_test_client()
        request_mock = AsyncMock(return_value=_order_response())
        client._request = request_mock  # type: ignore[method-assign]

        # cents would say "0.05" (rounded); bps says exactly 4.88¢.
        await client.create_order(
            ticker="KXDJT-YES",
            action="buy",
            side="no",
            order_type="limit",
            count=5,
            count_fp100=500,
            no_price=5,
            no_price_bps=488,
        )
        body = request_mock.call_args.kwargs["json"]
        assert body["no_price_dollars"] == "0.0488"
        assert body["count_fp"] == "5.00"


# ── amend_order ────────────────────────────────────────────────────


class TestAmendOrderWireFormat:
    async def test_cent_tick_via_legacy_cents(self) -> None:
        client = _build_test_client()
        request_mock = AsyncMock(return_value=_amend_response())
        client._request = request_mock  # type: ignore[method-assign]

        await client.amend_order(
            "ord-x",
            ticker="T-CENT",
            side="no",
            action="buy",
            no_price=53,
            count=5,
        )
        body = request_mock.call_args.kwargs["json"]
        assert body["no_price_dollars"] == "0.53"
        assert body["count_fp"] == "5.00"

    async def test_cent_tick_via_bps_path(self) -> None:
        client = _build_test_client()
        request_mock = AsyncMock(return_value=_amend_response())
        client._request = request_mock  # type: ignore[method-assign]

        await client.amend_order(
            "ord-x",
            ticker="T-CENT",
            side="no",
            action="buy",
            no_price_bps=5300,
            count_fp100=500,
        )
        body = request_mock.call_args.kwargs["json"]
        assert body["no_price_dollars"] == "0.53"
        assert body["count_fp"] == "5.00"

    async def test_sub_cent_via_bps_path(self) -> None:
        client = _build_test_client()
        request_mock = AsyncMock(return_value=_amend_response())
        client._request = request_mock  # type: ignore[method-assign]

        await client.amend_order(
            "ord-x",
            ticker="KXDJT-YES",
            side="yes",
            action="buy",
            yes_price_bps=488,
            count_fp100=500,
        )
        body = request_mock.call_args.kwargs["json"]
        assert body["yes_price_dollars"] == "0.0488"
        assert body["count_fp"] == "5.00"

    async def test_fractional_count_fp100(self) -> None:
        client = _build_test_client()
        request_mock = AsyncMock(return_value=_amend_response())
        client._request = request_mock  # type: ignore[method-assign]

        await client.amend_order(
            "ord-x",
            ticker="KXDJT-YES",
            side="yes",
            action="buy",
            yes_price_bps=4888,
            count_fp100=189,
        )
        body = request_mock.call_args.kwargs["json"]
        assert body["count_fp"] == "1.89"
        assert body["yes_price_dollars"] == "0.4888"

    async def test_bps_wins_when_both_passed(self) -> None:
        client = _build_test_client()
        request_mock = AsyncMock(return_value=_amend_response())
        client._request = request_mock  # type: ignore[method-assign]

        await client.amend_order(
            "ord-x",
            ticker="KXDJT-YES",
            side="no",
            action="buy",
            no_price=5,
            no_price_bps=488,
            count=5,
            count_fp100=500,
        )
        body = request_mock.call_args.kwargs["json"]
        assert body["no_price_dollars"] == "0.0488"
        assert body["count_fp"] == "5.00"


# ── decrease_order ─────────────────────────────────────────────────


class TestDecreaseOrderWireFormat:
    async def test_reduce_by_fp100(self) -> None:
        client = _build_test_client()
        request_mock = AsyncMock(return_value=_order_response())
        client._request = request_mock  # type: ignore[method-assign]

        await client.decrease_order("ord-x", reduce_by_fp100=50)
        body = request_mock.call_args.kwargs["json"]
        assert body["reduce_by_fp"] == "0.50"
        assert "reduce_to_fp" not in body

    async def test_reduce_to_fp100(self) -> None:
        client = _build_test_client()
        request_mock = AsyncMock(return_value=_order_response())
        client._request = request_mock  # type: ignore[method-assign]

        await client.decrease_order("ord-x", reduce_to_fp100=300)
        body = request_mock.call_args.kwargs["json"]
        assert body["reduce_to_fp"] == "3.00"
        assert "reduce_by_fp" not in body

    async def test_legacy_reduce_by_becomes_two_decimal(self) -> None:
        """Legacy whole-contract path now emits 2-decimal fp string."""
        client = _build_test_client()
        request_mock = AsyncMock(return_value=_order_response())
        client._request = request_mock  # type: ignore[method-assign]

        await client.decrease_order("ord-x", reduce_by=3)
        body = request_mock.call_args.kwargs["json"]
        assert body["reduce_by_fp"] == "3.00"

    async def test_fp100_wins_when_both_passed(self) -> None:
        client = _build_test_client()
        request_mock = AsyncMock(return_value=_order_response())
        client._request = request_mock  # type: ignore[method-assign]

        # Legacy says 5 (500 fp100); fp100 says 489 (4.89 contracts).
        await client.decrease_order(
            "ord-x",
            reduce_by=5,
            reduce_by_fp100=489,
        )
        body = request_mock.call_args.kwargs["json"]
        assert body["reduce_by_fp"] == "4.89"
