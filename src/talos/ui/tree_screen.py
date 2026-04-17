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


try:
    from talos.game_manager import SPORTS_SERIES as _SPORTS_GSR_PREFIXES_LIST

    _SPORTS_GSR_PREFIXES: set[str] = set(_SPORTS_GSR_PREFIXES_LIST)
except ImportError:  # pragma: no cover - defensive fallback
    _SPORTS_GSR_PREFIXES = {
        "KXNHLGAME",
        "KXNBAGAME",
        "KXMLBGAME",
        "KXNFLGAME",
        "KXWNBAGAME",
        "KXCFBGAME",
        "KXCBBGAME",
        "KXMLSGAME",
        "KXEPLGAME",
        "KXAHLGAME",
        "KXLOLGAME",
        "KXCS2GAME",
        "KXVALGAME",
        "KXDOTA2GAME",
        "KXCODGAME",
        "KXATPMATCH",
        "KXWTAMATCH",
        "KXATPCHALLENGERMATCH",
        "KXWTACHALLENGERMATCH",
        "KXATPDOUBLES",
        "KXLALIGAGAME",
        "KXBUNDESLIGAGAME",
        "KXSERIEAGAME",
        "KXLIGUE1GAME",
        "KXUCLGAME",
        "KXLIGAMXGAME",
        "KXKLEAGUEGAME",
        "KXSHLGAME",
        "KXKHLGAME",
        "KXEUROLEAGUEGAME",
        "KXNBLGAME",
        "KXBBLGAME",
        "KXCBAGAME",
        "KXKBLGAME",
        "KXUFCFIGHT",
        "KXBOXING",
        "KXT20MATCH",
        "KXIPL",
        "KXCRICKETODIMATCH",
        "KXRUGBYNRLMATCH",
        "KXAFLGAME",
        "KXNCAAMLAXGAME",
    }


def _series_has_sports_gsr_coverage(series_ticker: str) -> bool:
    """Quick check: does this series have sports GSR coverage?

    Mirrors the prefix set ``GameManager.SPORTS_SERIES`` uses. Events whose
    series is covered here always have a game-start timestamp available from
    GSR, so the commit-time schedule validator can skip them.
    """
    return series_ticker in _SPORTS_GSR_PREFIXES


class TreeScreen(Screen):
    """Tree-driven selection screen."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("r", "manual_refresh", "Refresh"),
        ("space", "toggle_current_node", "Toggle"),
        ("c", "commit_changes", "Commit"),
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
        self._deferred_set_unticked: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="filter", id="filter-input")
        yield Tree[dict[str, Any]]("Kalshi", id="tree")
        yield Footer()

    def on_mount(self) -> None:
        self._rebuild_tree()
        self._load_persisted_deferred()

    def _load_persisted_deferred(self) -> None:
        """Rehydrate _deferred_set_unticked from TreeMetadataStore.

        Called on mount so that deferred-untick flags set in a prior session
        survive a Talos restart. The persisted pending list is owned by
        TreeMetadataStore; this just mirrors it into the in-memory set used
        by the commit/promote path.
        """
        if self._metadata is not None:
            self._deferred_set_unticked = set(self._metadata.pending_unticked())

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

    def _events_needing_schedule(self) -> list[ArbPairRecord]:
        """Return staged-add records whose event has no known schedule source.

        An event has a schedule source iff ANY of these is true:
          - TreeMetadataStore has a manual_event_start entry for its
            kalshi_event_ticker (including "none" = opt-out)
          - MilestoneResolver has a milestone for its kalshi_event_ticker
          - The series_ticker matches a sports GSR-covered prefix
            (sports events always have GSR data)

        Returns records that need a schedule. Deduplicated by
        kalshi_event_ticker — if multiple pairs share the same event, the
        user is asked only once.
        """
        if self._metadata is None:
            return []
        seen: set[str] = set()
        needs: list[ArbPairRecord] = []
        for r in self.staged_changes.to_add:
            if r.kalshi_event_ticker in seen:
                continue
            seen.add(r.kalshi_event_ticker)

            # Manual override already set? (including "none" opt-out)
            if self._metadata.manual_event_start(r.kalshi_event_ticker) is not None:
                continue

            # Milestone covers it?
            if self._milestones is not None:
                ms = self._milestones.event_start(r.kalshi_event_ticker)
                if ms is not None:
                    continue

            # Sports GSR covers it? (prefix heuristic; exact match happens
            # in the engine cascade at tick time).
            if _series_has_sports_gsr_coverage(r.series_ticker):
                continue

            needs.append(r)
        return needs

    async def commit(self) -> None:
        """Push staged changes through Engine and reconcile metadata.

        Pre-commit: any staged add whose event has no milestone, no manual
        override, and no sports GSR coverage triggers the SchedulePopup so
        the user can enter an event-start time (or explicitly opt out of
        exit-only scheduling) BEFORE the engine is touched. Cancelling the
        popup aborts the commit and preserves staged_changes.
        """
        if self._engine is None or self._metadata is None:
            return

        # Pre-commit validation: prompt for any uncurated events.
        needs_schedule = self._events_needing_schedule()
        if needs_schedule:
            from talos.ui.schedule_popup import SchedulePopup

            popup = SchedulePopup(needs_schedule)
            result = await self.app.push_screen_wait(popup)
            if result is None:
                # User cancelled — abort commit, preserve staged_changes.
                self.app.notify("Commit cancelled.", severity="warning")
                return
            # Merge popup-provided schedules into staged.to_set_manual_start.
            self.staged_changes.to_set_manual_start.update(result)

        staged = self.staged_changes

        # 1. Apply manual_event_start FIRST so the resolver cascade sees the
        #    override before the engine ever ticks for the new pairs.
        for k, v in staged.to_set_manual_start.items():
            self._metadata.set_manual_event_start(k, v)

        # 2. Engine add/remove.
        added: list[Any] = []
        remove_outcomes: list[Any] = []
        if staged.to_add:
            added = await self._engine.add_pairs_from_selection(
                [r.model_dump() for r in staged.to_add]
            )
        if staged.to_remove:
            remove_outcomes = await self._engine.remove_pairs_from_selection(
                staged.to_remove,
            )

        # 3. Apply deferred/applied unticked per §5.1a rules.
        #    - set_unticked applied when ALL staged pairs for the event came
        #      back "removed". Mixed/winding → defer via _deferred_set_unticked.
        #    - clear_unticked applied when ALL staged pairs for the event
        #      were successfully added.
        staged_remove_set = set(staged.to_remove)
        for k in staged.to_set_unticked:
            matching = [
                o
                for o in remove_outcomes
                if o.kalshi_event_ticker == k and o.pair_ticker in staged_remove_set
            ]
            if matching and all(o.status == "removed" for o in matching):
                self._metadata.set_deliberately_unticked(k)
            else:
                # Some pair(s) went winding_down or failed → defer.
                # Persist the pending flag so it survives a restart even if
                # the winding-down pair is still settling when Talos crashes.
                self._deferred_set_unticked.add(k)
                self._metadata.set_deliberately_unticked_pending(k)

        added_keys = {(p.kalshi_event_ticker or p.event_ticker) for p in added}
        for k in staged.to_clear_unticked:
            if k in added_keys:
                self._metadata.clear_deliberately_unticked(k)

        # 4. Clear staged.
        self.staged_changes = StagedChanges.empty()

    def on_event_fully_removed(self, kalshi_event_ticker: str) -> None:
        """Engine listener callback: promote deferred [·] to applied."""
        if kalshi_event_ticker in self._deferred_set_unticked:
            if self._metadata is not None:
                self._metadata.promote_pending_to_applied(kalshi_event_ticker)
            self._deferred_set_unticked.discard(kalshi_event_ticker)

    # ── Keybinding actions ───────────────────────────────────────────────

    def action_toggle_current_node(self) -> None:
        """Toggle the currently-highlighted tree node.

        For event nodes: stages an add (or unstages if already staged).
        For series / category nodes: no-op for now (bulk toggle deferred).
        """
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return
        data = node.data
        kind = data.get("kind") if isinstance(data, dict) else None
        if kind == "event":
            ticker = data.get("ticker") if isinstance(data, dict) else None
            if ticker:
                self.toggle_event_by_ticker(ticker)
                self._refresh_node_label(node, ticker)

    def _refresh_node_label(self, node: TreeNode, kalshi_event_ticker: str) -> None:
        """Update the node's glyph to match the current staged state."""
        is_staged = any(
            r.kalshi_event_ticker == kalshi_event_ticker for r in self.staged_changes.to_add
        )
        glyph = "[x]" if is_staged else "[ ]"
        data = node.data if isinstance(node.data, dict) else {}
        ticker = data.get("ticker", kalshi_event_ticker)
        # First-pass UI: rebuild with glyph + ticker. Title suffix will be
        # re-rendered on the next full tree rebuild.
        node.set_label(f"{glyph} {ticker}")

    async def action_commit_changes(self) -> None:
        """Run the commit flow — pushes staged changes through Engine."""
        if self.staged_changes.is_empty():
            self.notify("No staged changes to commit.", severity="information")
            return
        await self.commit()
        self.notify("Commit complete.", severity="information")
        # Rebuild the tree so node glyphs reflect cleared staging
        self._rebuild_tree()
