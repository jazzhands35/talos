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
