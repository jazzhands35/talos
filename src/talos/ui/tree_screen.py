"""TreeScreen — tree-driven selection surface for Talos.

This screen is pushed on top of the main monitoring view. It renders the
discovery cache as an expandable tree and lets the user stage tick/untick
changes before committing them to the Engine.

See spec §4 for UX details.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Tree
from textual.widgets.tree import TreeNode

from talos.models.tree import ArbPairRecord, StagedChanges

if TYPE_CHECKING:
    from talos.discovery import DiscoveryService
    from talos.engine import TradingEngine
    from talos.milestones import MilestoneResolver
    from talos.tree_metadata import TreeMetadataStore


class TreeScreen(Screen):
    """Tree-driven selection screen."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("r", "manual_refresh", "Refresh"),
    ]

    def __init__(
        self,
        *,
        discovery: DiscoveryService | None,
        milestones: MilestoneResolver | None,
        metadata: TreeMetadataStore | None,
        engine: TradingEngine | None,
    ) -> None:
        super().__init__()
        self._discovery = discovery
        self._milestones = milestones
        self._metadata = metadata
        self._engine = engine
        self.staged_changes: StagedChanges = StagedChanges.empty()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="filter", id="filter-input")
        yield Tree[dict[str, Any]]("Kalshi", id="tree")
        yield Footer()

    def on_mount(self) -> None:
        self._rebuild_tree()

    def _rebuild_tree(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.root.remove_children()
        if self._discovery is None:
            return

        for cat_name, cat in sorted(self._discovery.categories.items()):
            cat_node = tree.root.add(
                f"[ ] {cat_name}   {cat.series_count} open",
                data={"kind": "category", "name": cat_name},
                expand=False,
            )
            cat_node.add("…", data={"kind": "placeholder"})

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node: TreeNode = event.node
        data = node.data or {}
        kind = data.get("kind")
        if kind == "category":
            self._expand_category(node, data["name"])
        elif kind == "series":
            self.run_worker(self._expand_series(node, data["ticker"]))

    def _expand_category(self, node: TreeNode, category: str) -> None:
        node.remove_children()
        if self._discovery is None:
            return
        cat = self._discovery.categories.get(category)
        if not cat:
            return
        for ticker, series in sorted(cat.series.items()):
            child = node.add(
                f"[ ] {ticker}",
                data={"kind": "series", "ticker": ticker},
                expand=False,
            )
            child.add("…", data={"kind": "placeholder"})
            _ = series  # reserved for future metadata display

    async def _expand_series(self, node: TreeNode, series_ticker: str) -> None:
        if self._discovery is None:
            return
        events = await self._discovery.get_events_for_series(series_ticker)
        node.remove_children()
        for event_ticker, ev in sorted(events.items()):
            node.add_leaf(
                f"[ ] {event_ticker}   {ev.title[:40]}",
                data={"kind": "event", "ticker": event_ticker},
            )

    async def action_manual_refresh(self) -> None:
        if self._discovery is not None:
            await self._discovery.bootstrap()

    def toggle_event_by_ticker(self, kalshi_event_ticker: str) -> None:
        """Programmatic toggle used by tests and by keybindings.

        If the event is not currently staged for add, stage it by building
        an ArbPairRecord per active market. If it IS currently staged, unstage
        (remove all records for this event from to_add).
        """
        if self._discovery is None:
            return

        # Find the event
        event_node = None
        series_ref = None
        cat_ref = None
        for cat in self._discovery.categories.values():
            for series in cat.series.values():
                if series.events is None:
                    continue
                if kalshi_event_ticker in series.events:
                    event_node = series.events[kalshi_event_ticker]
                    series_ref = series
                    cat_ref = cat
                    break
            if event_node is not None:
                break
        if event_node is None or series_ref is None or cat_ref is None:
            return

        existing = [
            r for r in self.staged_changes.to_add if r.kalshi_event_ticker == kalshi_event_ticker
        ]
        if existing:
            for r in list(existing):
                self.staged_changes.to_add.remove(r)
            return

        for mkt in event_node.markets:
            if mkt.status != "active":
                continue
            self.staged_changes.to_add.append(
                ArbPairRecord(
                    event_ticker=mkt.ticker,
                    ticker_a=mkt.ticker,
                    ticker_b=mkt.ticker,
                    side_a="yes",
                    side_b="no",
                    kalshi_event_ticker=kalshi_event_ticker,
                    series_ticker=series_ref.ticker,
                    category=cat_ref.name,
                    fee_type=series_ref.fee_type,
                    sub_title=event_node.sub_title,
                    close_time=(
                        event_node.close_time.isoformat() if event_node.close_time else None
                    ),
                    volume_24h_a=mkt.volume_24h,
                    volume_24h_b=mkt.volume_24h,
                )
            )
