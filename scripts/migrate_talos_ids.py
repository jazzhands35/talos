"""One-time migration: backfill talos_id for existing pairs in games_full.json.

Strategy: for each event_ticker with talos_id==0, look up MIN(ts) in
game_adds, sort all such pairs chronologically, then assign per-month
sequential ids (YY.MM.NNN) and bump the talos_id_counter so post-migration
assignments don't collide.

Pairs without any game_adds row (rare) get assigned in current local month
after all the dated ones.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from talos.talos_id import (
    bump_seq,
    encode_talos_id,
    ensure_counter_schema,
    format_talos_id,
)

_LOCAL_TZ = ZoneInfo("America/Los_Angeles")


def _first_seen_for_tickers(
    conn: sqlite3.Connection, tickers: list[str]
) -> dict[str, datetime]:
    """Return {event_ticker: earliest ts as aware datetime} for given tickers."""
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"SELECT event_ticker, MIN(ts) FROM game_adds "
        f"WHERE event_ticker IN ({placeholders}) GROUP BY event_ticker",
        tickers,
    ).fetchall()
    out: dict[str, datetime] = {}
    for ticker, ts in rows:
        if ts is None:
            continue
        out[ticker] = datetime.fromisoformat(ts)
    return out


def migrate(*, db: sqlite3.Connection, games_path: Path) -> None:
    """Run the migration. Idempotent — pairs with talos_id != 0 are left alone."""
    ensure_counter_schema(db)
    payload = json.loads(games_path.read_text())
    games = payload["games"]

    needs_migration = [g for g in games if int(g.get("talos_id", 0)) == 0]
    if not needs_migration:
        print("All pairs already have talos_id assigned. No migration needed.")
        return

    tickers = [g["event_ticker"] for g in needs_migration]
    first_seen = _first_seen_for_tickers(db, tickers)

    now_local = datetime.now(_LOCAL_TZ)
    fallback_dt = now_local

    def _sort_key(g: dict) -> tuple[int, datetime]:
        ts = first_seen.get(g["event_ticker"])
        return (0, ts) if ts is not None else (1, fallback_dt)

    needs_migration.sort(key=_sort_key)

    assignments: dict[str, int] = {}
    for g in needs_migration:
        ticker = g["event_ticker"]
        ts = first_seen.get(ticker, fallback_dt).astimezone(_LOCAL_TZ)
        seq = bump_seq(db, year=ts.year, month=ts.month)
        assignments[ticker] = encode_talos_id(year=ts.year, month=ts.month, seq=seq)

    for g in games:
        if int(g.get("talos_id", 0)) == 0 and g["event_ticker"] in assignments:
            g["talos_id"] = assignments[g["event_ticker"]]

    games_path.write_text(json.dumps(payload, indent=2))
    print(f"Migrated {len(assignments)} pairs.")
    for ticker, tid in sorted(assignments.items(), key=lambda x: x[1]):
        print(f"  {format_talos_id(tid)}  {ticker}")


def _main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="talos_data.db")
    p.add_argument("--games", default="games_full.json")
    args = p.parse_args()
    db = sqlite3.connect(args.db)
    migrate(db=db, games_path=Path(args.games))


if __name__ == "__main__":
    _main()
