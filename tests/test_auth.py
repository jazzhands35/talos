"""Tests for Kalshi RSA-PSS authentication."""

import base64
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

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

    def test_headers_returns_three_keys(self, rsa_key_pair: tuple[Path, rsa.RSAPublicKey]) -> None:
        pem_path, _ = rsa_key_pair
        auth = KalshiAuth(key_id="test-key", private_key_path=pem_path)

        headers = auth.headers("GET", "/trade-api/v2/markets")

        assert "KALSHI-ACCESS-KEY" in headers
        assert "KALSHI-ACCESS-TIMESTAMP" in headers
        assert "KALSHI-ACCESS-SIGNATURE" in headers

    def test_headers_key_matches_key_id(self, rsa_key_pair: tuple[Path, rsa.RSAPublicKey]) -> None:
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

    def test_signature_is_valid(self, rsa_key_pair: tuple[Path, rsa.RSAPublicKey]) -> None:
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

    def test_path_query_params_stripped(self, rsa_key_pair: tuple[Path, rsa.RSAPublicKey]) -> None:
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
