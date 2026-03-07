"""Diagnostic: check what the queue positions endpoint actually returns."""

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

    # Fetch orders first
    orders = await rest.get_orders(limit=10)
    resting = [o for o in orders if o.remaining_count > 0]
    print(f"Orders: {len(orders)} total, {len(resting)} resting")
    for o in resting:
        print(f"  {o.order_id[:12]}... {o.ticker} {o.side} {o.no_price}¢ "
              f"{o.fill_count}/{o.initial_count} queue_position={o.queue_position}")
    print()

    # Call queue positions endpoint RAW
    import httpx

    url = f"{config.rest_base_url}/portfolio/orders/queue_positions"
    headers = auth.headers("GET", "/trade-api/v2/portfolio/orders/queue_positions")
    async with httpx.AsyncClient() as http:
        resp = await http.get(url, headers=headers)
    print(f"Queue endpoint status: {resp.status_code}")
    print(f"Queue endpoint response:")
    try:
        data = resp.json()
        print(json.dumps(data, indent=2)[:2000])
    except Exception:
        print(resp.text[:2000])
    print()

    # Try with market tickers
    tickers = list({o.ticker for o in resting})
    print(f"Retrying with market_tickers={tickers}")
    result = await rest.get_queue_positions(market_tickers=tickers)
    print(f"Parsed get_queue_positions(): {result}")

    await rest.close()


if __name__ == "__main__":
    asyncio.run(main())
