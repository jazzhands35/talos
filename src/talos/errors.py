"""Kalshi API error hierarchy."""

from __future__ import annotations

from typing import Any


class KalshiError(Exception):
    """Base exception for all Kalshi API errors."""


class KalshiAuthError(KalshiError):
    """Authentication or signing failure."""


class KalshiAPIError(KalshiError):
    """Non-2xx API response."""

    def __init__(
        self,
        status_code: int,
        body: Any,
        message: str = "",
    ) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(message or f"Kalshi API error {status_code}: {body}")


class KalshiRateLimitError(KalshiAPIError):
    """429 Too Many Requests."""

    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(
            status_code=429,
            body=None,
            message=f"Rate limited (retry after {retry_after}s)" if retry_after else "Rate limited",
        )


class KalshiNotFoundError(KalshiAPIError):
    """Raised when Kalshi returns 404 on an order/resource fetch.

    Subclass of :class:`KalshiAPIError` so existing ``except KalshiAPIError``
    blocks still catch it. The dedicated class lets callers distinguish
    resource-gone from other API errors (F33: a 404 on a resting-order GET
    is a specific signal that the single tracked order_id no longer exists
    — but does NOT prove the whole side is empty).
    """

    def __init__(self, body: Any = None, message: str = "") -> None:
        super().__init__(status_code=404, body=body, message=message)


class KalshiConnectionError(KalshiError):
    """WebSocket or network connection failure."""
