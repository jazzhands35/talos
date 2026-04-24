"""Diagnostic: dump all orders and positions for a specific event ticker.

Usage: .venv/Scripts/python tools/check_event_orders.py KXAHLGAME-26MAR171900HARCHA
"""

import asyncio
import sys

from talos.auth import KalshiAuth
from talos.config import KalshiConfig
from talos.rest_client import KalshiRESTClient


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python tools/check_event_orders.py <event_ticker>", file=sys.stderr)
        sys.exit(1)

    event_ticker = sys.argv[1]
    config = KalshiConfig.from_env()
    auth = KalshiAuth(config.key_id, config.private_key_path)
    rest = KalshiRESTClient(auth, config)

    print(f"=== Orders for {event_ticker} ===\n")

    # Fetch ALL orders (including cancelled, executed) for this event
    all_orders = await rest.get_all_orders(event_ticker=event_ticker)
    if not all_orders:
        print("  (no orders found)")
    for o in all_orders:
        print(
            f"  {o.status:10s}  {o.ticker}  "
            f"NO@{o.no_price}c  "
            f"filled={o.fill_count}/{o.initial_count}  "
            f"remaining={o.remaining_count}  "
            f"id={o.order_id[:12]}  "
            f"created={o.created_time[:19]}"
        )
        if o.maker_fees:
            print(f"             maker_fees={o.maker_fees}")

    print(f"\n=== Positions for {event_ticker} ===\n")

    # Fetch market-level positions
    positions = await rest.get_positions(event_ticker=event_ticker)
    if not positions:
        print("  (no positions found)")
    for p in positions:
        print(
            f"  {p.ticker}  "
            f"position={p.position}  "
            f"total_traded={p.total_traded}"
        )

    print(f"\n=== Fills for event tickers ===\n")

    # Get fills for each market ticker
    seen_tickers: set[str] = set()
    for o in all_orders:
        seen_tickers.add(o.ticker)

    for ticker in sorted(seen_tickers):
        fills = await rest.get_fills(ticker=ticker, limit=100)
        if fills:
            print(f"  {ticker}:")
            for f in fills:
                print(
                    f"    {f.created_time[:19]}  "
                    f"NO@{f.no_price}c  "
                    f"count={f.count}  "
                    f"side={f.side}  "
                    f"action={f.action}"
                )
        else:
            print(f"  {ticker}: (no fills)")


asyncio.run(main())
