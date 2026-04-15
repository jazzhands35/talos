"""Summarize stale-book recovery cycles from a Talos soak log."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from statistics import mean

_EVENT = "stale_book_recovery_cycle"
_FIELD_RE = re.compile(r"(\w+)=(-?\d+)")


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * pct)
    return ordered[index]


def _parse_cycles(path: Path) -> list[dict[str, int]]:
    cycles: list[dict[str, int]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if _EVENT not in line:
            continue
        fields = {name: int(value) for name, value in _FIELD_RE.findall(line)}
        if fields:
            cycles.append(fields)
    return cycles


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize Talos stale-book recovery cycle logs."
    )
    parser.add_argument("logfile", type=Path, help="Path to Talos stderr soak log")
    args = parser.parse_args()

    cycles = _parse_cycles(args.logfile)
    if not cycles:
        print("No stale_book_recovery_cycle events found.")
        return 1

    elapsed = [cycle.get("elapsed_ms", 0) for cycle in cycles]
    active = [cycle.get("active_stale_count", 0) for cycle in cycles]
    attempted = [cycle.get("attempted_count", 0) for cycle in cycles]
    skipped = [cycle.get("skipped_cooldown_count", 0) for cycle in cycles]
    recovered = [cycle.get("recovered_count", 0) for cycle in cycles]
    failed = [cycle.get("failed_count", 0) for cycle in cycles]

    print(f"cycles: {len(cycles)}")
    print(f"active stale total: {sum(active)}")
    print(f"attempted total: {sum(attempted)}")
    print(f"cooldown skipped total: {sum(skipped)}")
    print(f"recovered total: {sum(recovered)}")
    print(f"failed total: {sum(failed)}")
    print(f"elapsed avg ms: {mean(elapsed):.1f}")
    print(f"elapsed p95 ms: {_percentile(elapsed, 0.95)}")
    print(f"elapsed max ms: {max(elapsed)}")
    print(f"active stale max: {max(active)}")
    print(f"attempted max: {max(attempted)}")
    print(f"cooldown skipped max: {max(skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
