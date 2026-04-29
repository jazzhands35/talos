"""Drift report: compare persisted ledger filled-state vs Kalshi truth.

Reads ``games_full.json`` (whatever's in ``get_data_dir()``) and queries
``GET /portfolio/fills`` for each pair, then rebuilds the authoritative
``filled_count_fp100`` / ``filled_total_cost_bps`` / ``filled_fees_bps``
the same way ``PositionLedger._rebuild_from_fills`` does. Prints a per-pair
drift table.

Default mode: read-only.

``--apply``: also write a corrected ``games_full.json`` (the prior file is
saved alongside as ``games_full.json.bak.<unix-ts>``). DO NOT run with
``--apply`` while Talos is running — Talos persists ``games_full.json``
on every refresh cycle and will overwrite the correction.

Usage:
    .venv/Scripts/python scripts/drift_report.py            # read-only
    .venv/Scripts/python scripts/drift_report.py --apply    # write fix
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _load_dotenv(env_file: Path) -> None:
    if not env_file.is_file():
        return
    import os

    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write a corrected games_full.json. Talos must be stopped first.",
    )
    parser.add_argument(
        "--ticker",
        default=None,
        help="Limit to one ticker (substring match against ticker_a/event_ticker).",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    _load_dotenv(repo_root / ".env")

    from talos.auth import KalshiAuth
    from talos.config import KalshiConfig
    from talos.persistence import get_data_dir
    from talos.rest_client import KalshiRESTClient
    from talos.units import ONE_CONTRACT_FP100

    data_dir = get_data_dir()
    games_path = data_dir / "games_full.json"
    if not games_path.is_file():
        print(f"games_full.json not found at {games_path}", file=sys.stderr)
        return 2

    payload = json.loads(games_path.read_text())
    games = payload.get("games", [])
    schema_version = payload.get("schema_version")
    print(f"games_full.json: {games_path}  (schema_version={schema_version}, {len(games)} games)")

    config = KalshiConfig.from_env()
    auth = KalshiAuth(config.key_id, config.private_key_path)
    rest = KalshiRESTClient(auth, config)
    print(f"Kalshi: {config.environment.value} @ {config.rest_base_url}")
    print()

    drift_rows: list[dict[str, int | str | bool]] = []
    corrections: dict[int, dict[str, int]] = {}  # idx -> rebuilt fields

    for idx, entry in enumerate(games):
        ticker_a = entry.get("ticker_a", "")
        ticker_b = entry.get("ticker_b", "")
        event_ticker = entry.get("event_ticker", "")
        side_a_str = entry.get("side_a", "no")
        side_b_str = entry.get("side_b", "no")
        is_same_ticker = ticker_a == ticker_b

        if args.ticker and (
            args.ticker not in ticker_a and args.ticker not in event_ticker
        ):
            continue

        ledger_data = (entry.get("ledger") or {}).get("ledger") or {}
        cur_fa = int(ledger_data.get("filled_count_fp100_a", 0) or 0)
        cur_fb = int(ledger_data.get("filled_count_fp100_b", 0) or 0)
        cur_ca = int(ledger_data.get("filled_total_cost_bps_a", 0) or 0)
        cur_cb = int(ledger_data.get("filled_total_cost_bps_b", 0) or 0)
        # Skip pairs with no fills at all — there's nothing to compare.
        if cur_fa == 0 and cur_fb == 0:
            continue

        # Fetch fills.
        try:
            fills_a = await rest.get_all_fills(ticker=ticker_a)
            fills_b: list = (
                [] if is_same_ticker else await rest.get_all_fills(ticker=ticker_b)
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  [{event_ticker}] FETCH FAILED: {exc}")
            continue

        # Rebuild — same logic as PositionLedger._rebuild_from_fills.
        rebuilt_count = {"a": 0, "b": 0}
        rebuilt_cost = {"a": 0, "b": 0}
        rebuilt_fees = {"a": 0, "b": 0}

        def _classify(fill: object) -> tuple[str | None, int, int, int]:
            """Return (side_key, count_fp100, price_bps, fee_bps) for a buy
            fill on this pair, or (None, 0, 0, 0) to skip."""
            action = getattr(fill, "action", "buy") or "buy"
            if action != "buy":
                return None, 0, 0, 0
            count_fp100 = int(getattr(fill, "count_fp100", 0) or 0)
            side_str = getattr(fill, "side", "")
            price_bps = (
                int(getattr(fill, "no_price_bps", 0) or 0)
                if side_str == "no"
                else int(getattr(fill, "yes_price_bps", 0) or 0)
            )
            fee_bps = int(getattr(fill, "fee_cost_bps", 0) or 0)
            return None, count_fp100, price_bps, fee_bps

        if is_same_ticker:
            for f in fills_a:
                _, count, price, fee = _classify(f)
                if count == 0:
                    continue
                f_side = getattr(f, "side", "")
                if f_side == side_a_str:
                    sk = "a"
                elif f_side == side_b_str:
                    sk = "b"
                else:
                    continue
                rebuilt_count[sk] += count
                rebuilt_cost[sk] += count * price // ONE_CONTRACT_FP100
                rebuilt_fees[sk] += fee
        else:
            for sk, fills_list in (("a", fills_a), ("b", fills_b)):
                for f in fills_list:
                    _, count, price, fee = _classify(f)
                    if count == 0:
                        continue
                    rebuilt_count[sk] += count
                    rebuilt_cost[sk] += count * price // ONE_CONTRACT_FP100
                    rebuilt_fees[sk] += fee

        drift_a = cur_fa - rebuilt_count["a"]
        drift_b = cur_fb - rebuilt_count["b"]

        drift_rows.append(
            {
                "idx": idx,
                "event_ticker": event_ticker,
                "is_same_ticker": is_same_ticker,
                "cur_fa": cur_fa,
                "cur_fb": cur_fb,
                "rebuilt_fa": rebuilt_count["a"],
                "rebuilt_fb": rebuilt_count["b"],
                "drift_a": drift_a,
                "drift_b": drift_b,
                "cur_ca": cur_ca,
                "rebuilt_ca": rebuilt_cost["a"],
                "cur_cb": cur_cb,
                "rebuilt_cb": rebuilt_cost["b"],
            }
        )

        if drift_a != 0 or drift_b != 0:
            corrections[idx] = {
                "filled_count_fp100_a": rebuilt_count["a"],
                "filled_count_fp100_b": rebuilt_count["b"],
                "filled_total_cost_bps_a": rebuilt_cost["a"],
                "filled_total_cost_bps_b": rebuilt_cost["b"],
                "filled_fees_bps_a": rebuilt_fees["a"],
                "filled_fees_bps_b": rebuilt_fees["b"],
            }

    header = (
        f"{'Event':45}"
        f" {'A cur':>7} {'A real':>7} {'A drift':>8}"
        f" {'B cur':>7} {'B real':>7} {'B drift':>8}"
    )
    print(header)
    print("-" * len(header))
    inflated_pairs = 0
    total_drift_contracts = 0.0
    for r in drift_rows:
        cur_fa = r["cur_fa"]
        rebuilt_fa = r["rebuilt_fa"]
        drift_av = r["drift_a"]
        cur_fb = r["cur_fb"]
        rebuilt_fb = r["rebuilt_fb"]
        drift_bv = r["drift_b"]
        assert isinstance(cur_fa, int) and isinstance(rebuilt_fa, int)
        assert isinstance(drift_av, int) and isinstance(cur_fb, int)
        assert isinstance(rebuilt_fb, int) and isinstance(drift_bv, int)
        a_cur = cur_fa / ONE_CONTRACT_FP100
        a_real = rebuilt_fa / ONE_CONTRACT_FP100
        a_drift = drift_av / ONE_CONTRACT_FP100
        b_cur = cur_fb / ONE_CONTRACT_FP100
        b_real = rebuilt_fb / ONE_CONTRACT_FP100
        b_drift = drift_bv / ONE_CONTRACT_FP100
        flag = " " if a_drift == 0 and b_drift == 0 else "*"
        row = (
            f"{flag} {str(r['event_ticker']):43}"
            f" {a_cur:>7.2f} {a_real:>7.2f} {a_drift:>+8.2f}"
            f" {b_cur:>7.2f} {b_real:>7.2f} {b_drift:>+8.2f}"
        )
        print(row)
        if a_drift != 0 or b_drift != 0:
            inflated_pairs += 1
            total_drift_contracts += abs(a_drift) + abs(b_drift)
    print("-" * len(header))
    print(
        f"Pairs with drift: {inflated_pairs} / {len(drift_rows)} examined  "
        f"(total over-count: {total_drift_contracts:.2f} contracts)"
    )

    if not args.apply:
        if corrections:
            print()
            print(
                "Run with --apply to write a corrected games_full.json "
                "(Talos must be stopped first)."
            )
        return 0

    if not corrections:
        print("No corrections needed.")
        return 0

    # Apply mode — write corrections.
    backup = games_path.with_suffix(f".json.bak.{int(time.time())}")
    backup.write_text(games_path.read_text())
    print(f"Backed up to {backup}")

    for idx, fields in corrections.items():
        ledger = games[idx].setdefault("ledger", {"schema_version": 2, "ledger": {}})
        inner = ledger.setdefault("ledger", {})
        for k, v in fields.items():
            inner[k] = v

    games_path.write_text(json.dumps(payload, indent=2))
    print(f"Wrote corrected {games_path} ({len(corrections)} pairs updated)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
