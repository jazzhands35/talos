"""Live Textual UI benchmark — drives TreeScreen via Pilot and times the
real user-perceived operations.

Unlike bench_tree_perf.py (which measures raw library code in isolation),
this mounts the TreeScreen inside an actual Textual App and measures what
the user experiences: press 't' equivalent, time until tree populated,
time until a category expands, time until `space` actually toggles.

Run with: .venv/Scripts/python tools/bench_tree_ui.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

# Ensure src/ is on the path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from textual.app import App, ComposeResult  # noqa: E402
from textual.widgets import Footer, Header, Tree  # noqa: E402

from talos.discovery import DiscoveryService  # noqa: E402
from talos.milestones import MilestoneResolver  # noqa: E402
from talos.tree_metadata import TreeMetadataStore  # noqa: E402
from talos.ui.tree_screen import TreeScreen  # noqa: E402


class _Harness(App):
    """Minimal app that just hosts TreeScreen for measurement."""

    def __init__(
        self,
        discovery: DiscoveryService,
        milestones: MilestoneResolver,
        metadata: TreeMetadataStore,
    ) -> None:
        super().__init__()
        self._discovery = discovery
        self._milestones = milestones
        self._metadata = metadata
        self.bootstrap_elapsed_ms: float | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()

    async def on_mount(self) -> None:
        # Mount TreeScreen directly (skip the main TalosApp and its workers —
        # we're measuring ONLY the tree path).
        screen = TreeScreen(
            discovery=self._discovery,
            milestones=self._milestones,
            metadata=self._metadata,
            engine=None,
        )
        await self.push_screen(screen)

    async def drive_bootstrap_test(self) -> dict[str, float]:
        """Drive: kick off bootstrap, wait for tree to populate, time it.

        Returns a dict of phase → ms.
        """
        results: dict[str, float] = {}

        # Phase 1: bootstrap discovery (simulating what the app does)
        t0 = time.perf_counter()
        await self._discovery.bootstrap()
        results["bootstrap_ms"] = (time.perf_counter() - t0) * 1000

        # Phase 2: wait for the screen's poll_for_bootstrap to rebuild the tree
        t1 = time.perf_counter()
        screen = self.screen
        assert isinstance(screen, TreeScreen)
        # Poll until the Tree widget has children
        for _ in range(200):  # up to 20s
            tree = screen.query_one("#tree", Tree)
            if len(tree.root.children) > 0:
                break
            await asyncio.sleep(0.1)
        results["tree_populate_ms"] = (time.perf_counter() - t1) * 1000

        # Phase 3: expand the biggest category
        tree = screen.query_one("#tree", Tree)
        biggest_node = None
        biggest_count = 0
        for child in tree.root.children:
            lbl = str(child.label)
            # label format: "[ ] Entertainment   2358 open"
            parts = lbl.split()
            if len(parts) >= 3 and parts[-1] == "open":
                try:
                    n = int(parts[-2])
                    if n > biggest_count:
                        biggest_count = n
                        biggest_node = child
                except ValueError:
                    pass
        if biggest_node is None:
            results["biggest_expand_ms"] = -1
            return results
        print(f"  biggest category: {biggest_node.label} ({biggest_count} series)")

        t2 = time.perf_counter()
        biggest_node.expand()
        # Wait for children to populate (beyond the placeholder "…")
        for _ in range(300):  # up to 30s
            if len(biggest_node.children) > 10:  # past the placeholder
                break
            await asyncio.sleep(0.1)
        results["biggest_expand_ms"] = (time.perf_counter() - t2) * 1000
        results["biggest_children_count"] = float(len(biggest_node.children))

        return results


async def main() -> None:
    print("=" * 75)
    print("LIVE TEXTUAL UI BENCHMARK — TreeScreen")
    print("=" * 75)

    # Real collaborators (hit Kalshi API, use a temp metadata store)
    import tempfile
    tmp_dir = Path(tempfile.mkdtemp(prefix="bench_tree_"))
    print(f"  temp metadata dir: {tmp_dir}")

    discovery = DiscoveryService()
    milestones = MilestoneResolver()
    metadata = TreeMetadataStore(path=tmp_dir / "tree_metadata.json")
    metadata.load()

    app = _Harness(discovery, milestones, metadata)

    async with app.run_test(size=(120, 40)) as pilot:
        print("\n[phase 1] bootstrap + tree populate")
        results = await app.drive_bootstrap_test()
        for k, v in results.items():
            unit = "ms" if k.endswith("_ms") else ""
            print(f"  {k:<35} {v:>10.1f} {unit}")
        _ = pilot  # silence unused-var

    print("\n" + "=" * 75)


if __name__ == "__main__":
    asyncio.run(main())
