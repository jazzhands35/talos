"""Tests for Kalshi error hierarchy."""

from talos.errors import (
    KalshiAPIError,
    KalshiAuthError,
    KalshiConnectionError,
    KalshiError,
    KalshiRateLimitError,
)


class TestErrorHierarchy:
    def test_base_error(self) -> None:
        err = KalshiError("something broke")
        assert str(err) == "something broke"
        assert isinstance(err, Exception)

    def test_auth_error_is_kalshi_error(self) -> None:
        err = KalshiAuthError("bad key")
        assert isinstance(err, KalshiError)

    def test_api_error_has_status_and_body(self) -> None:
        err = KalshiAPIError(status_code=400, body={"error": "bad request"}, message="bad")
        assert err.status_code == 400
        assert err.body == {"error": "bad request"}
        assert isinstance(err, KalshiError)

    def test_rate_limit_error_has_retry_after(self) -> None:
        err = KalshiRateLimitError(retry_after=5.0)
        assert err.retry_after == 5.0
        assert isinstance(err, KalshiAPIError)

    def test_connection_error(self) -> None:
        err = KalshiConnectionError("ws dropped")
        assert isinstance(err, KalshiError)
