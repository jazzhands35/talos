"""Benchmark tree-mode discovery and TreeScreen-widget performance.

Run with: .venv/Scripts/python tools/bench_tree_perf.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

# Ensure src/ is on the path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from talos.discovery import _KALSHI_API_BASE, DiscoveryService  # noqa: E402
from talos.milestones import MilestoneResolver  # noqa: E402
from talos.models.tree import CategoryNode  # noqa: E402


def _hr(label: str, seconds: float) -> str:
    return f"  {label:<50} {seconds * 1000:>8.1f} ms"


async def bench_fetch_series() -> str:
    print("\n[1/6] /series fetch (network + download)")
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        resp = await http.get(f"{_KALSHI_API_BASE}/series")
        resp.raise_for_status()
        text = resp.text
    elapsed = time.perf_counter() - t0
    size_mb = len(text) / 1024 / 1024
    print(_hr(f"fetched {size_mb:.1f} MB", elapsed))
    return text


def bench_json_parse(raw_text: str) -> list[dict[str, Any]]:
    print("\n[2/6] json.loads on /series body")
    t0 = time.perf_counter()
    data = json.loads(raw_text)
    elapsed = time.perf_counter() - t0
    series_list = data.get("series", [])
    print(_hr(f"parsed {len(series_list)} series", elapsed))
    return series_list


def bench_build_categories(all_series: list) -> dict[str, CategoryNode]:
    print("\n[3/6] Pydantic SeriesNode/CategoryNode build")
    t0 = time.perf_counter()
    categories = DiscoveryService._build_categories(all_series)
    elapsed = time.perf_counter() - t0
    total = sum(c.series_count for c in categories.values())
    print(_hr(f"built {total} SeriesNodes into {len(categories)} cats", elapsed))
    return categories


async def bench_milestones() -> None:
    print("\n[4/6] /milestones refresh (paginated)")
    r = MilestoneResolver()
    t0 = time.perf_counter()
    await r.refresh()
    elapsed = time.perf_counter() - t0
    print(_hr(f"indexed {r.count} events", elapsed))


def bench_label_build(categories: dict[str, CategoryNode]) -> None:
    """Just string/sort work — the pure-Python part of category expansion."""
    print("\n[5/6] Label string build (Python only, no widgets)")
    biggest = max(categories.values(), key=lambda c: c.series_count)
    print(f"  target: {biggest.name} ({biggest.series_count} series)")

    t0 = time.perf_counter()
    labels = [f"[ ] {t}" for t, _ in sorted(biggest.series.items())]
    elapsed = time.perf_counter() - t0
    print(_hr(f"built {len(labels)} labels", elapsed))


def bench_textual_tree_adds(categories: dict[str, CategoryNode]) -> None:
    """THIS is likely the real bottleneck — Textual Tree.add() cost per node."""
    print("\n[6/6] Textual Tree.add() cost (WIDGET work — the suspect)")

    # Import textual only here so the tool works even without display.
    try:
        from textual.widgets import Tree
    except ImportError as e:
        print(f"  textual not importable: {e}")
        return

    biggest = max(categories.values(), key=lambda c: c.series_count)
    series_items = sorted(biggest.series.items())
    n = len(series_items)

    # Build a detached Tree widget. No app running, no rendering,
    # just measuring the cost of .add() calls on the data model.
    tree: Tree[Any] = Tree("root")
    t0 = time.perf_counter()
    root_node = tree.root
    for ticker, _series in series_items:
        child = root_node.add(f"[ ] {ticker}", expand=False)
        child.add("...")
    elapsed = time.perf_counter() - t0
    per_ms = (elapsed / n) * 1000 if n else 0
    print(_hr(f"added {n} tree nodes (+ placeholder each)", elapsed))
    print(_hr("  per-node", per_ms / 1000))

    # Also try JUST add, no placeholder child
    tree2: Tree[Any] = Tree("root2")
    t0 = time.perf_counter()
    for ticker, _series in series_items:
        tree2.root.add(f"[ ] {ticker}", expand=False)
    elapsed2 = time.perf_counter() - t0
    print(_hr(f"same but no placeholder: {n} adds only", elapsed2))

    # And with add_leaf (no children-support)
    tree3: Tree[Any] = Tree("root3")
    t0 = time.perf_counter()
    for ticker, _series in series_items:
        tree3.root.add_leaf(f"[ ] {ticker}")
    elapsed3 = time.perf_counter() - t0
    print(_hr(f"add_leaf instead of add: {n} calls", elapsed3))


async def main() -> None:
    print("=" * 75)
    print("TREE-MODE PERFORMANCE BENCHMARK")
    print("=" * 75)

    try:
        raw_text = await bench_fetch_series()
    except Exception as e:
        print(f"FAIL: {e}")
        return

    series_list = bench_json_parse(raw_text)
    categories = bench_build_categories(series_list)

    try:
        await bench_milestones()
    except Exception as e:
        print(f"  milestones FAIL: {e}")

    bench_label_build(categories)
    bench_textual_tree_adds(categories)

    print("\n" + "=" * 75)
    print("Summary: the real cost is Textual widget operations, not data work.")
    print("=" * 75)


if __name__ == "__main__":
    asyncio.run(main())
