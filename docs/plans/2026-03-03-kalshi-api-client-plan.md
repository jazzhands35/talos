# Kalshi API Client Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the complete Kalshi REST + WebSocket API client — authentication, typed models, async HTTP, and real-time data feeds.

**Architecture:** Four modules (config, auth, models, clients) with strict layering: config feeds auth, auth feeds clients, models are shared. All I/O is async. All data is Pydantic-typed. TDD throughout.

**Tech Stack:** Python 3.12+, httpx (async HTTP), websockets (WS), pydantic v2 (models), cryptography (RSA-PSS), structlog (logging), pytest + pytest-asyncio (tests)

**Design doc:** `docs/plans/2026-03-03-kalshi-api-client-design.md`

---

### Task 1: Add cryptography dependency

**Files:**
- Modify: `pyproject.toml:6-12` (add `cryptography` to dependencies)

**Step 1: Add dependency**

In `pyproject.toml`, add `"cryptography>=43.0"` to the `dependencies` list:

```toml
dependencies = [
    "httpx>=0.27",
    "websockets>=13",
    "textual>=1.0",
    "pydantic>=2.0",
    "structlog>=24.0",
    "rich>=13.0",
    "cryptography>=43.0",
]
```

**Step 2: Install**

Run: `pip install -e ".[dev]"`
Expected: All deps install cleanly including `cryptography`

**Step 3: Verify existing test still passes**

Run: `pytest tests/test_smoke.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: add cryptography dependency for RSA-PSS auth"
```

---

### Task 2: Config module

**Files:**
- Create: `src/talos/config.py`
- Create: `tests/test_config.py`

**Step 1: Write the failing tests**

```python
"""Tests for Kalshi environment configuration."""

import os

import pytest

from talos.config import KalshiConfig, KalshiEnvironment


class TestKalshiEnvironment:
    def test_demo_is_default(self) -> None:
        assert KalshiEnvironment.DEMO.value == "demo"

    def test_production_value(self) -> None:
        assert KalshiEnvironment.PRODUCTION.value == "production"


class TestKalshiConfig:
    def test_demo_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/tmp/test.pem")
        monkeypatch.delenv("KALSHI_ENV", raising=False)

        config = KalshiConfig.from_env()

        assert config.environment == KalshiEnvironment.DEMO
        assert config.key_id == "test-key-id"
        assert str(config.private_key_path) == "/tmp/test.pem"
        assert "demo-api.kalshi.co" in config.rest_base_url
        assert "demo-api.kalshi.co" in config.ws_url

    def test_production_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KALSHI_KEY_ID", "prod-key")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/tmp/prod.pem")
        monkeypatch.setenv("KALSHI_ENV", "production")

        config = KalshiConfig.from_env()

        assert config.environment == KalshiEnvironment.PRODUCTION
        assert "api.elections.kalshi.com" in config.rest_base_url
        assert "api.elections.kalshi.com" in config.ws_url

    def test_missing_key_id_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KALSHI_KEY_ID", raising=False)
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/tmp/test.pem")

        with pytest.raises(ValueError, match="KALSHI_KEY_ID"):
            KalshiConfig.from_env()

    def test_missing_key_path_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KALSHI_KEY_ID", "test-key")
        monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)

        with pytest.raises(ValueError, match="KALSHI_PRIVATE_KEY_PATH"):
            KalshiConfig.from_env()

    def test_invalid_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KALSHI_KEY_ID", "test-key")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/tmp/test.pem")
        monkeypatch.setenv("KALSHI_ENV", "staging")

        with pytest.raises(ValueError, match="KALSHI_ENV"):
            KalshiConfig.from_env()
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'talos.config'`

**Step 3: Write the implementation**

```python
"""Kalshi API environment configuration."""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

from pydantic import BaseModel


class KalshiEnvironment(Enum):
    """Kalshi API environment — demo by default, production opt-in."""

    DEMO = "demo"
    PRODUCTION = "production"


_URLS: dict[KalshiEnvironment, tuple[str, str]] = {
    KalshiEnvironment.DEMO: (
        "https://demo-api.kalshi.co/trade-api/v2",
        "wss://demo-api.kalshi.co/",
    ),
    KalshiEnvironment.PRODUCTION: (
        "https://api.elections.kalshi.com/trade-api/v2",
        "wss://api.elections.kalshi.com/",
    ),
}


class KalshiConfig(BaseModel):
    """Immutable Kalshi API configuration."""

    model_config = {"frozen": True}

    environment: KalshiEnvironment
    key_id: str
    private_key_path: Path
    rest_base_url: str
    ws_url: str

    @classmethod
    def from_env(cls) -> KalshiConfig:
        """Load configuration from environment variables.

        Required env vars:
            KALSHI_KEY_ID: API key identifier
            KALSHI_PRIVATE_KEY_PATH: Path to RSA private key PEM file

        Optional env vars:
            KALSHI_ENV: "demo" (default) or "production"
        """
        key_id = os.environ.get("KALSHI_KEY_ID")
        if not key_id:
            msg = "KALSHI_KEY_ID environment variable is required"
            raise ValueError(msg)

        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if not key_path:
            msg = "KALSHI_PRIVATE_KEY_PATH environment variable is required"
            raise ValueError(msg)

        env_str = os.environ.get("KALSHI_ENV", "demo")
        try:
            environment = KalshiEnvironment(env_str)
        except ValueError:
            msg = f"KALSHI_ENV must be 'demo' or 'production', got '{env_str}'"
            raise ValueError(msg) from None

        rest_url, ws_url = _URLS[environment]

        return cls(
            environment=environment,
            key_id=key_id,
            private_key_path=Path(key_path),
            rest_base_url=rest_url,
            ws_url=ws_url,
        )
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/talos/config.py tests/test_config.py
git commit -m "feat: add Kalshi config module with demo/production profiles"
```

---

### Task 3: Auth module

**Files:**
- Create: `src/talos/auth.py`
- Create: `tests/test_auth.py`

**Step 1: Write the failing tests**

```python
"""Tests for Kalshi RSA-PSS authentication."""

import base64
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization

from talos.auth import KalshiAuth


@pytest.fixture()
def rsa_key_pair(tmp_path: Path) -> tuple[Path, rsa.RSAPublicKey]:
    """Generate a test RSA key pair and save private key to a temp file."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path = tmp_path / "test_key.pem"
    pem_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return pem_path, private_key.public_key()


class TestKalshiAuth:
    def test_init_loads_key(self, rsa_key_pair: tuple[Path, rsa.RSAPublicKey]) -> None:
        pem_path, _ = rsa_key_pair
        auth = KalshiAuth(key_id="test-key", private_key_path=pem_path)
        assert auth.key_id == "test-key"

    def test_init_bad_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            KalshiAuth(key_id="test-key", private_key_path=tmp_path / "nonexistent.pem")

    def test_headers_returns_three_keys(
        self, rsa_key_pair: tuple[Path, rsa.RSAPublicKey]
    ) -> None:
        pem_path, _ = rsa_key_pair
        auth = KalshiAuth(key_id="test-key", private_key_path=pem_path)

        headers = auth.headers("GET", "/trade-api/v2/markets")

        assert "KALSHI-ACCESS-KEY" in headers
        assert "KALSHI-ACCESS-TIMESTAMP" in headers
        assert "KALSHI-ACCESS-SIGNATURE" in headers

    def test_headers_key_matches_key_id(
        self, rsa_key_pair: tuple[Path, rsa.RSAPublicKey]
    ) -> None:
        pem_path, _ = rsa_key_pair
        auth = KalshiAuth(key_id="my-key-123", private_key_path=pem_path)

        headers = auth.headers("GET", "/trade-api/v2/markets")

        assert headers["KALSHI-ACCESS-KEY"] == "my-key-123"

    def test_headers_timestamp_is_current_ms(
        self, rsa_key_pair: tuple[Path, rsa.RSAPublicKey]
    ) -> None:
        pem_path, _ = rsa_key_pair
        auth = KalshiAuth(key_id="test-key", private_key_path=pem_path)

        before = int(time.time() * 1000)
        headers = auth.headers("GET", "/trade-api/v2/markets")
        after = int(time.time() * 1000)

        ts = int(headers["KALSHI-ACCESS-TIMESTAMP"])
        assert before <= ts <= after

    def test_signature_is_valid(
        self, rsa_key_pair: tuple[Path, rsa.RSAPublicKey]
    ) -> None:
        pem_path, public_key = rsa_key_pair
        auth = KalshiAuth(key_id="test-key", private_key_path=pem_path)

        method = "POST"
        path = "/trade-api/v2/portfolio/orders"
        headers = auth.headers(method, path)

        timestamp = headers["KALSHI-ACCESS-TIMESTAMP"]
        signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
        message = f"{timestamp}{method}{path}".encode()

        # Should not raise — verifies the signature is valid
        public_key.verify(
            signature,
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )

    def test_path_query_params_stripped(
        self, rsa_key_pair: tuple[Path, rsa.RSAPublicKey]
    ) -> None:
        """Auth should sign the path WITHOUT query parameters."""
        pem_path, public_key = rsa_key_pair
        auth = KalshiAuth(key_id="test-key", private_key_path=pem_path)

        # Sign a path with query params
        headers = auth.headers("GET", "/trade-api/v2/portfolio/orders?limit=5&status=resting")

        timestamp = headers["KALSHI-ACCESS-TIMESTAMP"]
        signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])

        # The signed message should NOT include query params
        message = f"{timestamp}GET/trade-api/v2/portfolio/orders".encode()

        public_key.verify(
            signature,
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'talos.auth'`

**Step 3: Write the implementation**

```python
"""Kalshi RSA-PSS request authentication."""

from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class KalshiAuth:
    """Generates authenticated headers for Kalshi API requests.

    Loads the RSA private key once, then signs each request with
    RSA-PSS / SHA-256 per Kalshi's auth spec.
    """

    def __init__(self, key_id: str, private_key_path: Path) -> None:
        self.key_id = key_id
        pem_data = private_key_path.read_bytes()
        self._private_key: rsa.RSAPrivateKey = serialization.load_pem_private_key(
            pem_data, password=None
        )  # type: ignore[assignment]

    def headers(self, method: str, path: str) -> dict[str, str]:
        """Generate the three required Kalshi auth headers.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.)
            path: Request path, may include query params (they'll be stripped).

        Returns:
            Dict with KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE.
        """
        # Strip query parameters — Kalshi signs path only
        clean_path = path.split("?")[0]

        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method}{clean_path}".encode()

        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_auth.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/talos/auth.py tests/test_auth.py
git commit -m "feat: add RSA-PSS authentication module"
```

---

### Task 4: Pydantic models — errors + market data

**Files:**
- Create: `src/talos/errors.py`
- Create: `src/talos/models/__init__.py`
- Create: `src/talos/models/market.py`
- Create: `tests/test_errors.py`
- Create: `tests/test_models_market.py`

**Step 1: Write the failing tests for errors**

```python
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
```

**Step 2: Write failing tests for market models**

```python
"""Tests for market data Pydantic models."""

from talos.models.market import Event, Market, OrderBook, OrderBookLevel, Series, Trade


class TestMarket:
    def test_parse_market_json(self) -> None:
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "event_ticker": "KXBTC-26MAR",
            "title": "BTC above 50000?",
            "status": "open",
            "yes_bid": 65,
            "yes_ask": 67,
            "no_bid": 33,
            "no_ask": 35,
            "volume": 15000,
            "open_interest": 3200,
            "last_price": 66,
        }
        m = Market.model_validate(data)
        assert m.ticker == "KXBTC-26MAR-T50000"
        assert m.yes_bid == 65
        assert m.volume == 15000

    def test_market_optional_fields(self) -> None:
        """Markets may have null/missing optional fields."""
        data = {
            "ticker": "TEST-MKT",
            "event_ticker": "TEST-EVT",
            "title": "Test",
            "status": "open",
        }
        m = Market.model_validate(data)
        assert m.yes_bid is None
        assert m.volume is None


class TestEvent:
    def test_parse_event_json(self) -> None:
        data = {
            "event_ticker": "KXBTC-26MAR",
            "series_ticker": "KXBTC",
            "title": "Bitcoin March 2026",
            "category": "Crypto",
            "status": "open",
            "markets": [],
        }
        e = Event.model_validate(data)
        assert e.event_ticker == "KXBTC-26MAR"
        assert e.category == "Crypto"

    def test_event_with_nested_markets(self) -> None:
        data = {
            "event_ticker": "KXBTC-26MAR",
            "series_ticker": "KXBTC",
            "title": "Bitcoin March 2026",
            "category": "Crypto",
            "status": "open",
            "markets": [
                {
                    "ticker": "KXBTC-26MAR-T50000",
                    "event_ticker": "KXBTC-26MAR",
                    "title": "BTC above 50000?",
                    "status": "open",
                }
            ],
        }
        e = Event.model_validate(data)
        assert len(e.markets) == 1
        assert e.markets[0].ticker == "KXBTC-26MAR-T50000"


class TestSeries:
    def test_parse_series_json(self) -> None:
        data = {
            "series_ticker": "KXBTC",
            "title": "Bitcoin Prices",
            "category": "Crypto",
            "tags": ["bitcoin", "crypto"],
        }
        s = Series.model_validate(data)
        assert s.series_ticker == "KXBTC"
        assert "bitcoin" in s.tags


class TestOrderBook:
    def test_parse_orderbook_json(self) -> None:
        data = {
            "market_ticker": "KXBTC-26MAR-T50000",
            "yes": [[65, 100], [64, 200]],
            "no": [[35, 150], [34, 50]],
        }
        ob = OrderBook.model_validate(data)
        assert ob.market_ticker == "KXBTC-26MAR-T50000"
        assert len(ob.yes) == 2
        assert ob.yes[0].price == 65
        assert ob.yes[0].quantity == 100


class TestTrade:
    def test_parse_trade_json(self) -> None:
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "trade_id": "abc-123",
            "price": 65,
            "count": 10,
            "side": "yes",
            "created_time": "2026-03-03T12:00:00Z",
        }
        t = Trade.model_validate(data)
        assert t.ticker == "KXBTC-26MAR-T50000"
        assert t.price == 65
        assert t.side == "yes"
```

**Step 3: Run tests to verify they fail**

Run: `pytest tests/test_errors.py tests/test_models_market.py -v`
Expected: FAIL — modules not found

**Step 4: Write errors implementation**

```python
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


class KalshiConnectionError(KalshiError):
    """WebSocket or network connection failure."""
```

**Step 5: Write market models implementation**

```python
"""Pydantic models for Kalshi market data."""

from __future__ import annotations

from pydantic import BaseModel


class OrderBookLevel(BaseModel):
    """A single price level in the orderbook."""

    price: int
    quantity: int


class Market(BaseModel):
    """A Kalshi market (contract)."""

    ticker: str
    event_ticker: str
    title: str
    status: str
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    volume: int | None = None
    open_interest: int | None = None
    last_price: int | None = None


class Event(BaseModel):
    """A Kalshi event containing one or more markets."""

    event_ticker: str
    series_ticker: str
    title: str
    category: str
    status: str
    markets: list[Market] = []


class Series(BaseModel):
    """A Kalshi series (template for events)."""

    series_ticker: str
    title: str
    category: str
    tags: list[str] = []


class OrderBook(BaseModel):
    """Orderbook snapshot for a market.

    Raw API returns [[price, qty], ...] arrays — we parse into OrderBookLevel.
    """

    market_ticker: str
    yes: list[OrderBookLevel]
    no: list[OrderBookLevel]

    @classmethod
    def _parse_levels(cls, raw: list[list[int]]) -> list[OrderBookLevel]:
        return [OrderBookLevel(price=pair[0], quantity=pair[1]) for pair in raw]

    def model_post_init(self, _context: object) -> None:
        # Handle raw [[price, qty], ...] format from API
        if self.yes and isinstance(self.yes[0], list):
            object.__setattr__(self, "yes", self._parse_levels(self.yes))  # type: ignore[arg-type]
        if self.no and isinstance(self.no[0], list):
            object.__setattr__(self, "no", self._parse_levels(self.no))  # type: ignore[arg-type]


class Trade(BaseModel):
    """A single trade execution."""

    ticker: str
    trade_id: str
    price: int
    count: int
    side: str
    created_time: str
```

**Step 6: Write models __init__.py**

```python
"""Talos Pydantic models for Kalshi API data."""

from talos.models.market import Event, Market, OrderBook, OrderBookLevel, Series, Trade

__all__ = [
    "Event",
    "Market",
    "OrderBook",
    "OrderBookLevel",
    "Series",
    "Trade",
]
```

**Step 7: Run tests to verify they pass**

Run: `pytest tests/test_errors.py tests/test_models_market.py -v`
Expected: All PASS

**Step 8: Commit**

```bash
git add src/talos/errors.py src/talos/models/ tests/test_errors.py tests/test_models_market.py
git commit -m "feat: add error hierarchy and market data models"
```

---

### Task 5: Pydantic models — orders + portfolio

**Files:**
- Create: `src/talos/models/order.py`
- Create: `src/talos/models/portfolio.py`
- Create: `tests/test_models_order.py`
- Create: `tests/test_models_portfolio.py`
- Modify: `src/talos/models/__init__.py`

**Step 1: Write failing tests for order models**

```python
"""Tests for order Pydantic models."""

from talos.models.order import BatchOrderResult, Fill, Order


class TestOrder:
    def test_parse_order_json(self) -> None:
        data = {
            "order_id": "ord-abc-123",
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
        o = Order.model_validate(data)
        assert o.order_id == "ord-abc-123"
        assert o.side == "yes"
        assert o.remaining_count == 10

    def test_order_optional_fields(self) -> None:
        data = {
            "order_id": "ord-123",
            "ticker": "TEST-MKT",
            "side": "no",
            "order_type": "limit",
            "price": 40,
            "count": 5,
            "remaining_count": 5,
            "fill_count": 0,
            "status": "resting",
            "created_time": "2026-03-03T12:00:00Z",
        }
        o = Order.model_validate(data)
        assert o.expiration_time is None


class TestFill:
    def test_parse_fill_json(self) -> None:
        data = {
            "trade_id": "trade-xyz",
            "order_id": "ord-abc-123",
            "ticker": "KXBTC-26MAR-T50000",
            "side": "yes",
            "price": 65,
            "count": 5,
            "created_time": "2026-03-03T12:01:00Z",
        }
        f = Fill.model_validate(data)
        assert f.trade_id == "trade-xyz"
        assert f.count == 5


class TestBatchOrderResult:
    def test_success_result(self) -> None:
        data = {
            "order_id": "ord-abc",
            "success": True,
        }
        r = BatchOrderResult.model_validate(data)
        assert r.success is True
        assert r.error is None

    def test_failure_result(self) -> None:
        data = {
            "order_id": "ord-def",
            "success": False,
            "error": "insufficient balance",
        }
        r = BatchOrderResult.model_validate(data)
        assert r.success is False
        assert r.error == "insufficient balance"
```

**Step 2: Write failing tests for portfolio models**

```python
"""Tests for portfolio Pydantic models."""

from talos.models.portfolio import Balance, ExchangeStatus, Position, Settlement


class TestBalance:
    def test_parse_balance_json(self) -> None:
        data = {
            "balance": 500000,
            "portfolio_value": 750000,
        }
        b = Balance.model_validate(data)
        assert b.balance == 500000
        assert b.portfolio_value == 750000


class TestPosition:
    def test_parse_position_json(self) -> None:
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "position": 10,
            "total_traded": 25,
            "market_exposure": 650,
        }
        p = Position.model_validate(data)
        assert p.ticker == "KXBTC-26MAR-T50000"
        assert p.position == 10

    def test_negative_position(self) -> None:
        """Short positions are negative."""
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "position": -5,
            "total_traded": 10,
            "market_exposure": 250,
        }
        p = Position.model_validate(data)
        assert p.position == -5


class TestSettlement:
    def test_parse_settlement_json(self) -> None:
        data = {
            "ticker": "KXBTC-26MAR-T50000",
            "settlement_price": 100,
            "payout": 1000,
            "settled_time": "2026-03-26T12:00:00Z",
        }
        s = Settlement.model_validate(data)
        assert s.settlement_price == 100
        assert s.payout == 1000


class TestExchangeStatus:
    def test_parse_status_json(self) -> None:
        data = {
            "trading_active": True,
            "exchange_active": True,
        }
        es = ExchangeStatus.model_validate(data)
        assert es.trading_active is True
```

**Step 3: Run to verify failures**

Run: `pytest tests/test_models_order.py tests/test_models_portfolio.py -v`
Expected: FAIL — modules not found

**Step 4: Write order models**

```python
"""Pydantic models for Kalshi orders and fills."""

from __future__ import annotations

from pydantic import BaseModel


class Order(BaseModel):
    """A Kalshi order."""

    order_id: str
    ticker: str
    side: str
    order_type: str
    price: int
    count: int
    remaining_count: int
    fill_count: int
    status: str
    created_time: str
    expiration_time: str | None = None


class Fill(BaseModel):
    """A single fill (partial or full order execution)."""

    trade_id: str
    order_id: str
    ticker: str
    side: str
    price: int
    count: int
    created_time: str


class BatchOrderResult(BaseModel):
    """Result of a single order in a batch operation."""

    order_id: str
    success: bool
    error: str | None = None
```

**Step 5: Write portfolio models**

```python
"""Pydantic models for Kalshi portfolio data."""

from __future__ import annotations

from pydantic import BaseModel


class Balance(BaseModel):
    """Account balance."""

    balance: int
    portfolio_value: int


class Position(BaseModel):
    """A position in a market. Positive = long, negative = short."""

    ticker: str
    position: int
    total_traded: int
    market_exposure: int


class Settlement(BaseModel):
    """A settled market position."""

    ticker: str
    settlement_price: int
    payout: int
    settled_time: str


class ExchangeStatus(BaseModel):
    """Exchange operational status."""

    trading_active: bool
    exchange_active: bool
```

**Step 6: Update models __init__.py**

```python
"""Talos Pydantic models for Kalshi API data."""

from talos.models.market import Event, Market, OrderBook, OrderBookLevel, Series, Trade
from talos.models.order import BatchOrderResult, Fill, Order
from talos.models.portfolio import Balance, ExchangeStatus, Position, Settlement

__all__ = [
    "Balance",
    "BatchOrderResult",
    "Event",
    "ExchangeStatus",
    "Fill",
    "Market",
    "Order",
    "OrderBook",
    "OrderBookLevel",
    "Position",
    "Series",
    "Settlement",
    "Trade",
]
```

**Step 7: Run tests**

Run: `pytest tests/test_models_order.py tests/test_models_portfolio.py -v`
Expected: All PASS

**Step 8: Commit**

```bash
git add src/talos/models/ tests/test_models_order.py tests/test_models_portfolio.py
git commit -m "feat: add order and portfolio models"
```

---

### Task 6: Pydantic models — WebSocket messages

**Files:**
- Create: `src/talos/models/ws.py`
- Create: `tests/test_models_ws.py`
- Modify: `src/talos/models/__init__.py`

**Step 1: Write failing tests**

```python
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
```

**Step 2: Run to verify failure**

Run: `pytest tests/test_models_ws.py -v`
Expected: FAIL

**Step 3: Write WS models**

```python
"""Pydantic models for Kalshi WebSocket messages."""

from __future__ import annotations

from pydantic import BaseModel


class OrderBookSnapshot(BaseModel):
    """Full orderbook snapshot received on subscription."""

    market_ticker: str
    market_id: str
    yes: list[list[int]]
    no: list[list[int]]


class OrderBookDelta(BaseModel):
    """Incremental orderbook change."""

    market_ticker: str
    market_id: str
    price: int
    delta: int
    side: str
    ts: str
    price_dollars: float | None = None
    delta_fp: str | None = None
    client_order_id: str | None = None
    subaccount: int | None = None


class TickerMessage(BaseModel):
    """Market ticker update."""

    market_ticker: str
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    last_price: int | None = None
    volume: int | None = None


class TradeMessage(BaseModel):
    """Public trade on a market."""

    market_ticker: str
    price: int
    count: int
    side: str
    ts: str
    trade_id: str


class WSSubscribed(BaseModel):
    """Server confirmation of a subscription."""

    channel: str
    sid: int


class WSError(BaseModel):
    """Server error message."""

    code: int
    msg: str
```

**Step 4: Update models __init__.py** — add WS model re-exports:

```python
"""Talos Pydantic models for Kalshi API data."""

from talos.models.market import Event, Market, OrderBook, OrderBookLevel, Series, Trade
from talos.models.order import BatchOrderResult, Fill, Order
from talos.models.portfolio import Balance, ExchangeStatus, Position, Settlement
from talos.models.ws import (
    OrderBookDelta,
    OrderBookSnapshot,
    TickerMessage,
    TradeMessage,
    WSError,
    WSSubscribed,
)

__all__ = [
    "Balance",
    "BatchOrderResult",
    "Event",
    "ExchangeStatus",
    "Fill",
    "Market",
    "Order",
    "OrderBook",
    "OrderBookDelta",
    "OrderBookLevel",
    "OrderBookSnapshot",
    "Position",
    "Series",
    "Settlement",
    "TickerMessage",
    "Trade",
    "TradeMessage",
    "WSError",
    "WSSubscribed",
]
```

**Step 5: Run tests**

Run: `pytest tests/test_models_ws.py -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add src/talos/models/ tests/test_models_ws.py
git commit -m "feat: add WebSocket message models"
```

---

### Task 7: REST client — core infrastructure

**Files:**
- Create: `src/talos/rest_client.py`
- Create: `tests/test_rest_client.py`

This task builds the REST client's core: construction, auth injection, request/response handling, error mapping. Endpoint methods come in Tasks 8-9.

**Step 1: Write failing tests**

```python
"""Tests for Kalshi REST client."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

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


class TestClientConstruction:
    def test_base_url_set(self, client: KalshiRESTClient) -> None:
        assert "demo-api.kalshi.co" in client._base_url


class TestAuthInjection:
    async def test_auth_headers_added(
        self, client: KalshiRESTClient, mock_auth: KalshiAuth
    ) -> None:
        """Every request should include auth headers."""
        mock_response = httpx.Response(200, json={"trading_active": True, "exchange_active": True})

        with patch.object(client._http, "request", new_callable=AsyncMock, return_value=mock_response):
            await client.get_exchange_status()
            mock_auth.headers.assert_called_once()


class TestErrorMapping:
    async def test_400_raises_api_error(
        self, client: KalshiRESTClient, mock_auth: KalshiAuth
    ) -> None:
        mock_response = httpx.Response(400, json={"error": "bad request"})

        with patch.object(client._http, "request", new_callable=AsyncMock, return_value=mock_response):
            with pytest.raises(KalshiAPIError) as exc_info:
                await client.get_exchange_status()
            assert exc_info.value.status_code == 400

    async def test_429_raises_rate_limit_error(
        self, client: KalshiRESTClient, mock_auth: KalshiAuth
    ) -> None:
        mock_response = httpx.Response(
            429,
            json={"error": "rate limited"},
            headers={"Retry-After": "5"},
        )

        with patch.object(client._http, "request", new_callable=AsyncMock, return_value=mock_response):
            with pytest.raises(KalshiRateLimitError) as exc_info:
                await client.get_exchange_status()
            assert exc_info.value.retry_after == 5.0
```

**Step 2: Run to verify failure**

Run: `pytest tests/test_rest_client.py -v`
Expected: FAIL

**Step 3: Write REST client implementation (core only)**

```python
"""Async REST client for the Kalshi trading API."""

from __future__ import annotations

import structlog
import httpx

from talos.auth import KalshiAuth
from talos.config import KalshiConfig
from talos.errors import KalshiAPIError, KalshiRateLimitError
from talos.models.portfolio import ExchangeStatus

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
        params: dict | None = None,
        json: dict | None = None,
    ) -> dict:
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
            raise KalshiRateLimitError(
                retry_after=float(retry_after) if retry_after else None
            )

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
```

**Step 4: Run tests**

Run: `pytest tests/test_rest_client.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/talos/rest_client.py tests/test_rest_client.py
git commit -m "feat: add REST client core with auth injection and error mapping"
```

---

### Task 8: REST client — market data endpoints

**Files:**
- Modify: `src/talos/rest_client.py`
- Modify: `tests/test_rest_client.py`

**Step 1: Write failing tests for market endpoints**

Add to `tests/test_rest_client.py`:

```python
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
        mock_response = httpx.Response(200, json=mock_data)

        with patch.object(client._http, "request", new_callable=AsyncMock, return_value=mock_response):
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
        mock_response = httpx.Response(200, json=mock_data)

        with patch.object(client._http, "request", new_callable=AsyncMock, return_value=mock_response):
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
        mock_response = httpx.Response(200, json=mock_data)

        with patch.object(client._http, "request", new_callable=AsyncMock, return_value=mock_response):
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
        mock_response = httpx.Response(200, json=mock_data)

        with patch.object(client._http, "request", new_callable=AsyncMock, return_value=mock_response):
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
        mock_response = httpx.Response(200, json=mock_data)

        with patch.object(client._http, "request", new_callable=AsyncMock, return_value=mock_response):
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
        mock_response = httpx.Response(200, json=mock_data)

        with patch.object(client._http, "request", new_callable=AsyncMock, return_value=mock_response):
            series = await client.get_series("KXBTC")
            assert series.series_ticker == "KXBTC"
```

**Step 2: Run to verify failure**

Run: `pytest tests/test_rest_client.py::TestMarketEndpoints -v`
Expected: FAIL — methods don't exist

**Step 3: Add market endpoint methods to `rest_client.py`**

Add these methods to the `KalshiRESTClient` class:

```python
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
        params: dict = {"limit": limit}
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

    async def get_event(
        self, event_ticker: str, *, with_nested_markets: bool = False
    ) -> Event:
        params: dict = {}
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        data = await self._request("GET", f"/events/{event_ticker}", params=params)
        return Event.model_validate(data["event"])

    async def get_series(self, series_ticker: str) -> Series:
        data = await self._request("GET", f"/series/{series_ticker}")
        return Series.model_validate(data["series"])

    async def get_orderbook(self, ticker: str, *, depth: int = 0) -> OrderBook:
        params: dict = {}
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
        params: dict = {"ticker": ticker, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/markets/trades", params=params)
        return [Trade.model_validate(t) for t in data["trades"]]
```

Also add the missing imports at top of `rest_client.py`:

```python
from talos.models.market import Event, Market, OrderBook, Series, Trade
```

**Step 4: Run tests**

Run: `pytest tests/test_rest_client.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/talos/rest_client.py tests/test_rest_client.py
git commit -m "feat: add REST client market data endpoints"
```

---

### Task 9: REST client — order + portfolio endpoints

**Files:**
- Modify: `src/talos/rest_client.py`
- Modify: `tests/test_rest_client.py`

**Step 1: Write failing tests for order endpoints**

Add to `tests/test_rest_client.py`:

```python
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
        mock_response = httpx.Response(200, json=mock_data)

        with patch.object(client._http, "request", new_callable=AsyncMock, return_value=mock_response) as mock_req:
            order = await client.create_order(
                ticker="KXBTC-26MAR-T50000",
                side="yes",
                order_type="limit",
                price=65,
                count=10,
            )
            assert order.order_id == "ord-123"
            # Verify POST was sent with correct body
            call_kwargs = mock_req.call_args
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
        mock_response = httpx.Response(200, json=mock_data)

        with patch.object(client._http, "request", new_callable=AsyncMock, return_value=mock_response):
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
        mock_response = httpx.Response(200, json=mock_data)

        with patch.object(client._http, "request", new_callable=AsyncMock, return_value=mock_response):
            orders = await client.get_orders()
            assert len(orders) == 1


class TestPortfolioEndpoints:
    async def test_get_balance(self, client: KalshiRESTClient) -> None:
        mock_data = {"balance": 500000, "portfolio_value": 750000}
        mock_response = httpx.Response(200, json=mock_data)

        with patch.object(client._http, "request", new_callable=AsyncMock, return_value=mock_response):
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
        mock_response = httpx.Response(200, json=mock_data)

        with patch.object(client._http, "request", new_callable=AsyncMock, return_value=mock_response):
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
        mock_response = httpx.Response(200, json=mock_data)

        with patch.object(client._http, "request", new_callable=AsyncMock, return_value=mock_response):
            fills = await client.get_fills()
            assert len(fills) == 1
            assert fills[0].count == 5
```

**Step 2: Run to verify failure**

Run: `pytest tests/test_rest_client.py::TestOrderEndpoints tests/test_rest_client.py::TestPortfolioEndpoints -v`
Expected: FAIL

**Step 3: Add order + portfolio methods to `rest_client.py`**

```python
    # --- Orders ---

    async def create_order(
        self,
        *,
        ticker: str,
        side: str,
        order_type: str,
        price: int,
        count: int,
    ) -> Order:
        body = {
            "ticker": ticker,
            "side": side,
            "type": order_type,
            "price": price,
            "count": count,
        }
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
        body: dict = {}
        if new_price is not None:
            body["new_price"] = new_price
        if new_count is not None:
            body["new_count"] = new_count
        data = await self._request("POST", f"/portfolio/orders/{order_id}/amend", json=body)
        return Order.model_validate(data["order"])

    async def batch_create_orders(
        self, orders: list[dict]
    ) -> list[BatchOrderResult]:
        data = await self._request("POST", "/portfolio/orders/batched", json={"orders": orders})
        return [BatchOrderResult.model_validate(r) for r in data["orders"]]

    async def batch_cancel_orders(
        self, order_ids: list[str]
    ) -> list[BatchOrderResult]:
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
        params: dict = {"limit": limit}
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
        params: dict = {"limit": limit}
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
        params: dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if order_id:
            params["order_id"] = order_id
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/portfolio/fills", params=params)
        return [Fill.model_validate(f) for f in data["fills"]]
```

Add missing imports:

```python
from talos.models.order import BatchOrderResult, Fill, Order
from talos.models.portfolio import Balance, ExchangeStatus, Position
```

**Step 4: Run tests**

Run: `pytest tests/test_rest_client.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/talos/rest_client.py tests/test_rest_client.py
git commit -m "feat: add REST client order and portfolio endpoints"
```

---

### Task 10: WebSocket client

**Files:**
- Create: `src/talos/ws_client.py`
- Create: `tests/test_ws_client.py`

**Step 1: Write failing tests**

```python
"""Tests for Kalshi WebSocket client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from talos.auth import KalshiAuth
from talos.config import KalshiConfig, KalshiEnvironment
from talos.models.ws import OrderBookDelta, OrderBookSnapshot
from talos.ws_client import KalshiWSClient


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
    auth = MagicMock(spec=KalshiAuth)
    auth.key_id = "test-key"
    auth.headers.return_value = {
        "KALSHI-ACCESS-KEY": "test-key",
        "KALSHI-ACCESS-TIMESTAMP": "1234567890",
        "KALSHI-ACCESS-SIGNATURE": "fakesig",
    }
    return auth


@pytest.fixture()
def client(config: KalshiConfig, mock_auth: KalshiAuth) -> KalshiWSClient:
    return KalshiWSClient(auth=mock_auth, config=config)


class TestSubscribeMessage:
    def test_builds_subscribe_command(self, client: KalshiWSClient) -> None:
        msg = client._build_subscribe("orderbook_delta", "KXBTC-26MAR-T50000")
        assert msg["cmd"] == "subscribe"
        assert msg["params"]["channels"] == ["orderbook_delta"]
        assert msg["params"]["market_ticker"] == "KXBTC-26MAR-T50000"
        assert isinstance(msg["id"], int)
        assert msg["id"] >= 1

    def test_message_ids_increment(self, client: KalshiWSClient) -> None:
        msg1 = client._build_subscribe("orderbook_delta", "MKT-1")
        msg2 = client._build_subscribe("ticker", "MKT-2")
        assert msg2["id"] == msg1["id"] + 1


class TestUnsubscribeMessage:
    def test_builds_unsubscribe_command(self, client: KalshiWSClient) -> None:
        msg = client._build_unsubscribe([1, 2, 3])
        assert msg["cmd"] == "unsubscribe"
        assert msg["params"]["sids"] == [1, 2, 3]


class TestMessageDispatch:
    async def test_dispatches_to_registered_callback(self, client: KalshiWSClient) -> None:
        callback = AsyncMock()
        client.on_message("orderbook_delta", callback)

        # Simulate a subscribed message to register the sid
        client._sid_to_channel[1] = "orderbook_delta"

        raw = {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {
                "market_ticker": "KXBTC-26MAR-T50000",
                "market_id": "uuid-123",
                "yes": [[65, 100]],
                "no": [[35, 50]],
            },
        }
        await client._dispatch(raw)
        callback.assert_called_once()

    async def test_ignores_unknown_sid(self, client: KalshiWSClient) -> None:
        callback = AsyncMock()
        client.on_message("orderbook_delta", callback)

        raw = {"type": "orderbook_delta", "sid": 999, "seq": 1, "msg": {}}
        await client._dispatch(raw)
        callback.assert_not_called()


class TestSeqTracking:
    async def test_detects_seq_gap(self, client: KalshiWSClient) -> None:
        """Logs a warning when seq numbers have a gap."""
        callback = AsyncMock()
        client.on_message("orderbook_delta", callback)
        client._sid_to_channel[1] = "orderbook_delta"
        client._sid_to_seq[1] = 5

        raw = {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 8,  # gap: expected 6
            "msg": {
                "market_ticker": "MKT",
                "market_id": "uuid",
                "price": 50,
                "delta": 10,
                "side": "yes",
                "ts": "2026-03-03T12:00:00Z",
            },
        }

        with patch("talos.ws_client.logger") as mock_logger:
            await client._dispatch(raw)
            mock_logger.warning.assert_called_once()


class TestCallbackRegistration:
    def test_register_callback(self, client: KalshiWSClient) -> None:
        callback = AsyncMock()
        client.on_message("ticker", callback)
        assert "ticker" in client._callbacks
```

**Step 2: Run to verify failure**

Run: `pytest tests/test_ws_client.py -v`
Expected: FAIL

**Step 3: Write WebSocket client implementation**

```python
"""WebSocket client for Kalshi real-time data feeds."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import structlog
import websockets

from talos.auth import KalshiAuth
from talos.config import KalshiConfig
from talos.models.ws import (
    OrderBookDelta,
    OrderBookSnapshot,
    TickerMessage,
    TradeMessage,
    WSError,
    WSSubscribed,
)

logger = structlog.get_logger()

# Maps WS message type strings to their Pydantic model
_MESSAGE_MODELS: dict[str, type] = {
    "orderbook_snapshot": OrderBookSnapshot,
    "orderbook_delta": OrderBookDelta,
    "ticker": TickerMessage,
    "trade": TradeMessage,
}


class KalshiWSClient:
    """WebSocket client for Kalshi real-time feeds.

    Manages connection, subscriptions, keepalive, and message dispatch.
    """

    def __init__(self, auth: KalshiAuth, config: KalshiConfig) -> None:
        self._auth = auth
        self._ws_url = config.ws_url
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._next_id = 1
        self._callbacks: dict[str, Callable] = {}
        self._sid_to_channel: dict[int, str] = {}
        self._sid_to_seq: dict[int, int] = {}

    def _next_message_id(self) -> int:
        msg_id = self._next_id
        self._next_id += 1
        return msg_id

    def _build_subscribe(self, channel: str, market_ticker: str) -> dict:
        return {
            "id": self._next_message_id(),
            "cmd": "subscribe",
            "params": {
                "channels": [channel],
                "market_ticker": market_ticker,
            },
        }

    def _build_unsubscribe(self, sids: list[int]) -> dict:
        return {
            "id": self._next_message_id(),
            "cmd": "unsubscribe",
            "params": {"sids": sids},
        }

    def on_message(self, channel: str, callback: Callable) -> None:
        """Register a callback for messages on a specific channel."""
        self._callbacks[channel] = callback

    async def connect(self) -> None:
        """Open the WebSocket connection with auth headers."""
        headers = self._auth.headers("GET", "/")
        self._ws = await websockets.connect(self._ws_url, additional_headers=headers)
        logger.info("ws_connected", url=self._ws_url)

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        if self._ws:
            await self._ws.close()
            self._ws = None
            logger.info("ws_disconnected")

    async def subscribe(self, channel: str, market_ticker: str) -> None:
        """Subscribe to a channel for a specific market."""
        if not self._ws:
            msg = "WebSocket not connected"
            raise RuntimeError(msg)
        message = self._build_subscribe(channel, market_ticker)
        await self._ws.send(json.dumps(message))
        logger.debug("ws_subscribe_sent", channel=channel, market_ticker=market_ticker)

    async def unsubscribe(self, sids: list[int]) -> None:
        """Unsubscribe from subscriptions by sid."""
        if not self._ws:
            msg = "WebSocket not connected"
            raise RuntimeError(msg)
        message = self._build_unsubscribe(sids)
        await self._ws.send(json.dumps(message))
        logger.debug("ws_unsubscribe_sent", sids=sids)

    async def _dispatch(self, raw: dict[str, Any]) -> None:
        """Parse and route a WebSocket message to the appropriate callback."""
        msg_type = raw.get("type", "")

        # Handle subscription confirmations
        if msg_type == "subscribed":
            sub = WSSubscribed.model_validate(raw.get("msg", {}))
            self._sid_to_channel[sub.sid] = sub.channel
            self._sid_to_seq[sub.sid] = 0
            logger.debug("ws_subscribed", channel=sub.channel, sid=sub.sid)
            return

        # Handle errors
        if msg_type == "error":
            err = WSError.model_validate(raw.get("msg", {}))
            logger.error("ws_error", code=err.code, msg=err.msg)
            return

        # Route data messages by sid
        sid = raw.get("sid")
        if sid is None or sid not in self._sid_to_channel:
            return

        channel = self._sid_to_channel[sid]

        # Check seq continuity
        seq = raw.get("seq")
        if seq is not None:
            expected = self._sid_to_seq.get(sid, 0) + 1
            if seq != expected and self._sid_to_seq.get(sid, 0) > 0:
                logger.warning(
                    "ws_seq_gap",
                    sid=sid,
                    channel=channel,
                    expected=expected,
                    got=seq,
                )
            self._sid_to_seq[sid] = seq

        # Parse message into model
        msg_data = raw.get("msg", {})
        model_cls = _MESSAGE_MODELS.get(msg_type)
        parsed = model_cls.model_validate(msg_data) if model_cls else msg_data

        # Dispatch to callback
        callback = self._callbacks.get(channel)
        if callback:
            await callback(parsed)

    async def listen(self) -> None:
        """Listen for messages and dispatch them. Blocks until disconnect."""
        if not self._ws:
            msg = "WebSocket not connected"
            raise RuntimeError(msg)

        async for raw_msg in self._ws:
            data = json.loads(raw_msg)
            logger.debug("ws_message", type=data.get("type"), sid=data.get("sid"))
            await self._dispatch(data)
```

**Step 4: Run tests**

Run: `pytest tests/test_ws_client.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/talos/ws_client.py tests/test_ws_client.py
git commit -m "feat: add WebSocket client with subscribe, dispatch, and seq tracking"
```

---

### Task 11: Run full test suite + lint

**Step 1: Run all tests**

Run: `pytest -v`
Expected: All tests pass

**Step 2: Run ruff**

Run: `ruff check src/ tests/`
Expected: Clean (fix any issues)

**Step 3: Run ruff format**

Run: `ruff format src/ tests/`
Expected: Clean

**Step 4: Run pyright**

Run: `pyright`
Expected: Clean (fix any type errors)

**Step 5: Fix any issues found, then commit**

```bash
git add -A
git commit -m "chore: fix lint and type issues across API client"
```

---

### Task 12: Final integration commit + brain update

**Step 1: Verify everything passes**

Run: `pytest -v && ruff check src/ tests/ && pyright`
Expected: All green

**Step 2: Update brain vault**

Update `brain/architecture.md` to reflect that Layer 1 is complete. Update `brain/codebase/index.md` with module descriptions.

**Step 3: Commit brain updates**

```bash
git add brain/
git commit -m "docs: update brain vault with Layer 1 completion"
```
