"""Fetch REST orderbooks for random active tickers and display NO-side best prices.

Compares REST ground truth against what the WS-fed local books should show.
Run while Talos is active to spot stale books.

Usage:
    .venv/Scripts/python tools/verify_orderbook.py [--series KXNHLGAME] [--count 10]
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path


def _load_dotenv() -> None:
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        import os
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

from talos.auth import KalshiAuth  # noqa: E402
from talos.config import KalshiConfig  # noqa: E402
from talos.rest_client import KalshiRESTClient  # noqa: E402


def _cents(val: int | str) -> int:
    """Convert to integer cents, handling both old (int) and new (dollars str) formats."""
    if isinstance(val, str):
        return round(float(val) * 100)
    return val


async def _get_orderbook(rest: KalshiRESTClient, ticker: str) -> dict:
    """Fetch orderbook and extract best prices, bypassing the OrderBook model."""
    data = await rest._request("GET", f"/markets/{ticker}/orderbook", params={"depth": 1})
    book_data = data.get("orderbook") or data.get("orderbook_fp", {})
    yes_levels = book_data.get("yes", [])
    no_levels = book_data.get("no", [])
    no_best = _cents(no_levels[0][0]) if no_levels else None
    yes_best = _cents(yes_levels[0][0]) if yes_levels else None
    return {"no_best": no_best, "yes_best": yes_best}


async def _retry(coro_fn, retries: int = 3, base_delay: float = 2.0):
    """Retry a coroutine factory on rate limit errors."""
    for attempt in range(retries):
        try:
            return await coro_fn()
        except Exception as e:
            if "rate" in str(e).lower() or "429" in str(e):
                delay = base_delay * (attempt + 1)
                print(f"  (rate limited, retrying in {delay:.0f}s...)", file=sys.stderr)
                await asyncio.sleep(delay)
            else:
                raise
    return await coro_fn()  # final attempt


async def main(series: list[str], count: int) -> None:
    config = KalshiConfig.from_env()
    auth = KalshiAuth(config.key_id, config.private_key_path)
    rest = KalshiRESTClient(auth, config)

    # Gather active events across all requested series, with retries
    all_events = []
    for s in series:
        events = await _retry(
            lambda s=s: rest.get_events(
                series_ticker=s, status="open", with_nested_markets=True
            )
        )
        all_events.extend(events)
        await asyncio.sleep(0.5)  # gentle pacing

    if not all_events:
        print("No active events found for series:", series)
        return

    # Collect all market tickers (active markets only)
    market_tickers: list[tuple[str, str, str]] = []  # (event_ticker, market_ticker, title)
    for ev in all_events:
        for mkt in ev.markets:
            if mkt.status == "active":
                market_tickers.append((ev.event_ticker, mkt.ticker, ev.sub_title or ev.title))

    if not market_tickers:
        print("No active markets found")
        return

    # Pick random sample
    sample = random.sample(market_tickers, min(count, len(market_tickers)))
    sample.sort(key=lambda x: x[0])  # group by event

    print(f"\n{'Ticker':<45} {'NO best':>8} {'YES best':>9} {'Spread':>7}  Event")
    print("-" * 110)

    for _event_ticker, ticker, title in sample:
        try:
            # Inject market_ticker so the OrderBook model can validate
            book = await _retry(lambda t=ticker: _get_orderbook(rest, t))
            no_best = book["no_best"]
            yes_best = book["yes_best"]
            spread = ""
            if no_best is not None and yes_best is not None:
                spread = f"{100 - no_best - yes_best:+d}c"
            no_str = f"{no_best}c" if no_best is not None else "--"
            yes_str = f"{yes_best}c" if yes_best is not None else "--"
            print(f"{ticker:<45} {no_str:>8} {yes_str:>9} {spread:>7}  {title[:40]}")
        except Exception as e:
            print(f"{ticker:<45} {'ERROR':>8}  {e!s:.60}")
        await asyncio.sleep(0.3)  # pace orderbook fetches

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify orderbook prices via REST")
    parser.add_argument(
        "--series",
        nargs="+",
        default=["KXNHLGAME", "KXNCAAMBBGAME"],
        help="Series tickers to check (default: KXNHLGAME KXNCAAMBBGAME)",
    )
    parser.add_argument("--count", type=int, default=10, help="Number of random tickers")
    args = parser.parse_args()
    asyncio.run(main(args.series, args.count))
