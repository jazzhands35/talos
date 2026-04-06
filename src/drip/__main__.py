"""Entry point: python -m drip."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _load_dotenv() -> None:
    """Load .env file from project root if it exists."""
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="drip",
        description="Drip \u2014 staggered 1-contract arbitrage manager",
    )
    parser.add_argument(
        "event_ticker",
        help="Kalshi event ticker (e.g. KXNHLGAME-26MAR19WPGBOS)",
    )
    parser.add_argument(
        "ticker_a",
        help="Market ticker for side A",
    )
    parser.add_argument(
        "ticker_b",
        help="Market ticker for side B",
    )
    parser.add_argument(
        "price_a",
        type=int,
        help="NO price in cents for side A",
    )
    parser.add_argument(
        "price_b",
        type=int,
        help="NO price in cents for side B",
    )
    parser.add_argument(
        "--max-resting",
        type=int,
        default=20,
        help="Maximum resting orders per side (default: 20)",
    )
    parser.add_argument(
        "--stagger-delay",
        type=float,
        default=5.0,
        help="Seconds between contract deployments (default: 5.0)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use Kalshi demo environment (default: production)",
    )
    return parser.parse_args()


def main() -> None:
    """Launch the Drip TUI."""
    _load_dotenv()
    args = _parse_args()

    # Set KALSHI_ENV from --demo flag so KalshiConfig.from_env() picks it up
    if args.demo:
        os.environ["KALSHI_ENV"] = "demo"
    elif "KALSHI_ENV" not in os.environ:
        os.environ["KALSHI_ENV"] = "production"

    # --- Build objects ---
    from drip.config import DripConfig
    from drip.ui.app import DripApp
    from talos.auth import KalshiAuth
    from talos.config import KalshiConfig
    from talos.rest_client import KalshiRESTClient

    try:
        kalshi_config = KalshiConfig.from_env()
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    auth = KalshiAuth(kalshi_config.key_id, kalshi_config.private_key_path)
    rest = KalshiRESTClient(auth, kalshi_config)

    drip_config = DripConfig(
        event_ticker=args.event_ticker,
        ticker_a=args.ticker_a,
        ticker_b=args.ticker_b,
        price_a=args.price_a,
        price_b=args.price_b,
        max_resting=args.max_resting,
        stagger_delay=args.stagger_delay,
    )

    app = DripApp(drip_config, rest, auth, kalshi_config)
    app.run()


if __name__ == "__main__":
    main()
