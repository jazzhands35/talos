"""Diagnostic: check what the trades endpoint actually returns."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


async def main() -> None:
    # Load env
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if env_file.is_file():
        import os

        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ[key.strip()] = value.strip().strip('"').strip("'")

    from talos.auth import KalshiAuth
    from talos.config import KalshiConfig
    from talos.rest_client import KalshiRESTClient

    config = KalshiConfig.from_env()
    auth = KalshiAuth(config.key_id, config.private_key_path)
    rest = KalshiRESTClient(auth, config)

    print(f"Environment: {config.environment.value}")
    print(f"Base URL: {config.rest_base_url}")
    print()

    # Use a known ticker from the user's screenshot
    ticker = "KXATPMATCH-26MAR06BROTIA-TIA"

    # 1. Try our existing endpoint (query param form)
    print(f"=== Test 1: GET /markets/trades?ticker={ticker}&limit=5 ===")
    import httpx
    url = f"{config.rest_base_url}/markets/trades"
    headers = auth.headers("GET", "/trade-api/v2/markets/trades")
    async with httpx.AsyncClient() as http:
        resp = await http.get(url, headers=headers, params={"ticker": ticker, "limit": 5})
    print(f"Status: {resp.status_code}")
    try:
        data = resp.json()
        print(json.dumps(data, indent=2)[:2000])
    except Exception:
        print(resp.text[:2000])
    print()

    # 2. Try the path-based endpoint
    print(f"=== Test 2: GET /markets/{ticker}/trades?limit=5 ===")
    url2 = f"{config.rest_base_url}/markets/{ticker}/trades"
    headers2 = auth.headers("GET", f"/trade-api/v2/markets/{ticker}/trades")
    async with httpx.AsyncClient() as http:
        resp2 = await http.get(url2, headers=headers2, params={"limit": 5})
    print(f"Status: {resp2.status_code}")
    try:
        data2 = resp2.json()
        print(json.dumps(data2, indent=2)[:2000])
    except Exception:
        print(resp2.text[:2000])
    print()

    # 3. Try via our REST client method
    print("=== Test 3: Via KalshiRESTClient.get_trades() ===")
    try:
        trades = await rest.get_trades(ticker, limit=5)
        print(f"Got {len(trades)} trades")
        for t in trades[:3]:
            print(f"  {t.trade_id} {t.side} {t.price}¢ x{t.count} @ {t.created_time}")
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}")
    print()

    await rest.close()


if __name__ == "__main__":
    asyncio.run(main())
