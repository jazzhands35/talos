"""TreeScreen — tree-driven selection surface for Talos.

This screen is pushed on top of the main monitoring view. It renders the
discovery cache as an expandable tree and lets the user stage tick/untick
changes before committing them to the Engine.

See spec §4 for UX details.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Tree
from textual.widgets.tree import TreeNode

from talos.models.tree import ArbPairRecord, StagedChanges

if TYPE_CHECKING:
    from talos.discovery import DiscoveryService
    from talos.engine import TradingEngine
    from talos.milestones import MilestoneResolver
    from talos.models.tree import CategoryNode, SeriesNode
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
        # priority=True so this handler wins over Textual Tree's internal
        # `space` binding (which toggles expand/collapse). We route:
        #   event leaf  -> tick/untick one event
        #   series node -> bulk-tick all events in the series
        #   category    -> bulk-tick all events across the category
        # Expand/collapse still works via the arrow keys (← / →).
        Binding("space", "toggle_current_node", "Toggle", priority=True),
        ("c", "commit_changes", "Commit"),
    ]

    _GLYPHS = {
        "empty": "[ ]",
        "partial": "[~]",
        "checked": "[✓]",
        "deliberately_unticked": "[·]",
        "winding": "[W]",
    }

    def _glyph_for_state(self, state: str) -> str:
        return self._GLYPHS.get(state, "[ ]")

    def _series_selection_state(self, series: SeriesNode) -> str:
        """Aggregate tick-state across a series's events.

        Returns 'checked' only when every event is effectively checked;
        'partial' when some but not all are; 'empty' otherwise. Used to
        render the series node's parent-of-events glyph so it reflects
        whether the subtree is fully / partially / not selected.

        If events aren't loaded yet (events is None), we can't know — treat
        as empty rather than guessing.
        """
        if not series.events:
            return "empty"
        states = [self._effective_state(et) for et in series.events]
        if all(s == "checked" for s in states):
            return "checked"
        if any(s == "checked" for s in states):
            return "partial"
        return "empty"

    def _aggregate_state(self, sub_states: list[str]) -> str:
        """Combine child states into a parent state.

        All children checked -> checked.
        Any child checked/partial -> partial.
        Otherwise -> empty.
        """
        if not sub_states:
            return "empty"
        if all(s == "checked" for s in sub_states):
            return "checked"
        if any(s in ("checked", "partial") for s in sub_states):
            return "partial"
        return "empty"

    def _series_label(self, ticker: str, series: SeriesNode) -> str:
        """Render a series node's label, including its open-event count if
        we have one, and a glyph reflecting whether any/all of its events
        are staged/ticked.
        """
        count = series.event_count
        if count is None:
            suffix = "  ?"
        elif count == 0:
            suffix = "  (none open)"
        elif count == 1:
            suffix = "  1 event"
        else:
            suffix = f"  {count} events"
        glyph = self._glyph_for_state(self._series_selection_state(series))
        return f"{glyph} {ticker}{suffix}"

    @staticmethod
    def _is_series_visible(series: SeriesNode) -> bool:
        """Gate for tree rendering: hide series known to have zero open events.

        Tri-state: None (unknown — bootstrap fetch failed / pending) stays
        visible so a 429 on the bulk count doesn't blank the whole tree.
        0 (known-empty) is hidden. >0 is shown.
        """
        return series.event_count != 0

    @classmethod
    def _visible_series(cls, cat: CategoryNode) -> list[SeriesNode]:
        return [s for s in cat.series.values() if cls._is_series_visible(s)]

    def _category_label(self, name: str, cat: CategoryNode) -> str:
        """Render a category label with the visible-series and total-event
        counts, plus a glyph aggregating state across every visible series
        (so a category shows [✓] when everything under it is ticked).
        """
        visible = self._visible_series(cat)
        total_events = sum((s.event_count or 0) for s in visible)
        state = self._aggregate_state(
            [self._series_selection_state(s) for s in visible]
        )
        glyph = self._glyph_for_state(state)
        return f"{glyph} {name}   {len(visible)} series · {total_events} open"

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
        # Filter input deferred — it auto-focuses and swallows space/c/r
        # keybindings. Re-add later behind a keybinding (e.g. "/").
        yield Tree[dict[str, Any]]("Kalshi", id="tree")
        yield Footer()

    def on_mount(self) -> None:
        # Give the tree widget focus so keybindings (space/c/r) work
        # immediately without needing to tab into it.
        tree = self.query_one("#tree", Tree)
        tree.focus()
        self._rebuild_tree()
        self._load_persisted_deferred()
        # Start the two-stage poll: first watch for categories to appear,
        # then watch for event counts to backfill. Both can be in-progress
        # independently since bootstrap returns before the bulk count
        # fetch completes.
        self._categories_seen = bool(
            self._discovery and self._discovery.categories,
        )
        self._counts_seen = self._any_counts_populated()
        if self._discovery is not None and not (self._categories_seen and self._counts_seen):
            self._bootstrap_polls = 0
            self.set_interval(0.5, self._poll_for_bootstrap)

    def _any_counts_populated(self) -> bool:
        """Cheap check: has any series got a non-None event_count yet?
        Used to decide when to rebuild the tree after the async background
        bulk-count fetch finishes."""
        if self._discovery is None:
            return False
        for cat in self._discovery.categories.values():
            for s in cat.series.values():
                if s.event_count is not None:
                    return True
        return False

    def _poll_for_bootstrap(self) -> None:
        """Re-render the tree when discovery populates — in two stages.

        Stage 1: categories appear (fast; the /series fetch finishes).
        Stage 2: event counts backfill (slow; the bulk /events fetch runs
        async in the background and may 429 for a minute or more).

        We rebuild once per stage, then stop polling. 60s total cap covers
        the 45s bulk-fetch timeout with room to spare.
        """
        if getattr(self, "_bootstrap_done", False):
            return
        self._bootstrap_polls = getattr(self, "_bootstrap_polls", 0) + 1
        if self._discovery is None:
            self._bootstrap_done = True
            return

        if not self._categories_seen and self._discovery.categories:
            self._rebuild_tree()
            self._categories_seen = True

        if not self._counts_seen and self._any_counts_populated():
            # Counts arrived — rebuild so empty-series filter and sort apply.
            self._rebuild_tree()
            self._counts_seen = True

        if self._categories_seen and self._counts_seen:
            self._bootstrap_done = True
            return

        if self._bootstrap_polls > 120:  # ~60s hard cap
            if not self._categories_seen:
                self.notify(
                    "Discovery bootstrap didn't complete — check logs.",
                    severity="warning",
                )
            # Counts may never arrive if Kalshi stays rate-limited; that's
            # fine, lazy backfill via drill-ins handles it. Stop polling.
            self._bootstrap_done = True

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
            # Skip categories that have no visible series at all. We only
            # suppress when we're sure — if any series has an unknown count
            # (None), _is_series_visible keeps it, so the category stays.
            if not self._visible_series(cat):
                continue
            cat_node = tree.root.add(
                self._category_label(cat_name, cat),
                data={"kind": "category", "name": cat_name},
                expand=False,
            )
            cat_node.add("…", data={"kind": "placeholder"})

        # Re-focus the tree so space/c/r keybindings work. Without this,
        # after a rebuild triggered by _poll_for_bootstrap, focus may
        # have landed elsewhere (or nowhere) and key events are eaten.
        tree.focus()

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node: TreeNode = event.node
        data = node.data or {}
        kind = data.get("kind")
        if kind == "category":
            # Large categories (Entertainment = 2358 series, Politics = 1828)
            # must build their children asynchronously with periodic yields;
            # doing it sync blocks the event loop for several seconds.
            self.run_worker(self._expand_category_async(node, data["name"]))
        elif kind == "cluster":
            self.run_worker(self._expand_cluster(node))
        elif kind == "series":
            self.run_worker(self._expand_series(node, data["ticker"]))

    async def _expand_category_async(self, node: TreeNode, category: str) -> None:
        """Populate a category's children — clusters (if worthwhile) followed
        by orphan series rows. Large categories are built with periodic
        yields so the event loop stays responsive."""
        node.remove_children()
        if self._discovery is None:
            return
        cat = self._discovery.categories.get(category)
        if not cat:
            return
        import asyncio as _asyncio
        import time as _time

        import structlog as _structlog

        from talos.tree_clustering import cluster_series

        _log = _structlog.get_logger()
        _t0 = _time.perf_counter()
        _log.info("tree_expand_start", category=category, series_count=cat.series_count)

        tree = self.query_one("#tree", Tree)

        visible = [s for s in cat.series.values() if self._is_series_visible(s)]
        mode, clusters, orphans = cluster_series(visible)

        # Clusters first (sorted by member count desc — cluster_series does
        # this already), then orphan series under the category directly.
        for cluster_name, members in clusters:
            label = self._cluster_label(cluster_name, members)
            cluster_node = node.add(
                label,
                data={
                    "kind": "cluster",
                    "category": category,
                    "cluster_name": cluster_name,
                    # Store member tickers on the node so we can render
                    # children lazily without re-running the cluster_series
                    # function at expand time.
                    "tickers": [m.ticker for m in members],
                },
                expand=False,
            )
            cluster_node.add("…", data={"kind": "placeholder"})

        # Orphan series rendered directly under the category, same sort key
        # as before (events desc, alpha tiebreak).
        def _series_sort(s: SeriesNode) -> tuple[int, str]:
            count = s.event_count if s.event_count is not None else -1
            return (-count, s.ticker)

        added = 0
        for series in sorted(orphans, key=_series_sort):
            child = node.add(
                self._series_label(series.ticker, series),
                data={"kind": "series", "ticker": series.ticker},
                expand=False,
            )
            child.add("…", data={"kind": "placeholder"})
            added += 1
            if added % 100 == 0:
                tree.refresh()
                await _asyncio.sleep(0)

        tree.refresh()
        _log.info(
            "tree_expand_done",
            category=category,
            mode=mode,
            cluster_count=len(clusters),
            orphan_count=added,
            elapsed_ms=int((_time.perf_counter() - _t0) * 1000),
            series_total=cat.series_count,
        )

    def _cluster_label(self, cluster_name: str, members: list[SeriesNode]) -> str:
        """Render a cluster header with an aggregate glyph across its member
        series, so a fully-ticked cluster shows [✓] and a partially-ticked
        one shows [~]."""
        total_events = sum((s.event_count or 0) for s in members)
        state = self._aggregate_state(
            [self._series_selection_state(s) for s in members]
        )
        glyph = self._glyph_for_state(state)
        return f"{glyph} {cluster_name}   {len(members)} series · {total_events} open"

    async def _expand_cluster(self, node: TreeNode) -> None:
        """Render a cluster's member series as children. Member tickers are
        stashed on node.data at cluster-creation time so no re-clustering is
        needed here — just materialize rows in sort order."""
        node.remove_children()
        if self._discovery is None:
            return
        data = node.data if isinstance(node.data, dict) else {}
        tickers = data.get("tickers") or []
        if not tickers:
            return

        category = data.get("category", "")
        cat = self._discovery.categories.get(category)
        if cat is None:
            return

        members = [cat.series[t] for t in tickers if t in cat.series]

        def _sort(s: SeriesNode) -> tuple[int, str]:
            count = s.event_count if s.event_count is not None else -1
            return (-count, s.ticker)

        for series in sorted(members, key=_sort):
            child = node.add(
                self._series_label(series.ticker, series),
                data={"kind": "series", "ticker": series.ticker},
                expand=False,
            )
            child.add("…", data={"kind": "placeholder"})

    async def _expand_series(self, node: TreeNode, series_ticker: str) -> None:
        import time as _time

        import structlog as _structlog

        _log = _structlog.get_logger()
        _t0 = _time.perf_counter()
        _log.info("tree_series_expand_start", series_ticker=series_ticker)
        if self._discovery is None:
            _log.info(
                "tree_series_expand_done",
                series=series_ticker,
                elapsed_ms=0,
                event_count=0,
                reason="no_discovery",
            )
            return
        events = await self._discovery.get_events_for_series(series_ticker)
        fetch_ms = int((_time.perf_counter() - _t0) * 1000)
        _log.info(
            "tree_series_expand_fetched",
            series=series_ticker,
            elapsed_ms=fetch_ms,
            event_count=len(events),
        )

        # Backfill the series label with the fresh count — get_events_for_series
        # stamped event_count on the SeriesNode; update the tree display so
        # the user sees "? -> N events" the first time they drill in, even
        # when the bulk bootstrap count fetch failed.
        self._relabel_series_node(node, series_ticker)

        node.remove_children()

        # Surface likely fetch failures: if bootstrap said this series had
        # open events but we got zero back, the /events call almost certainly
        # failed (rate limit, network). Show a leaf so the user knows to
        # retry rather than staring at a silently-collapsed node.
        expected = 0
        cat_series = None
        for cat in self._discovery.categories.values():
            if series_ticker in cat.series:
                cat_series = cat.series[series_ticker]
                expected = cat_series.event_count or 0
                break
        if expected > 0 and not events:
            node.add_leaf(
                f"(fetch failed — {expected} expected; press 'r' to retry)",
                data={"kind": "fetch_error"},
            )
            return

        for event_ticker, ev in sorted(events.items()):
            state = self._effective_state(event_ticker)
            glyph = self._glyph_for_state(state)
            node.add_leaf(
                f"{glyph} {event_ticker}   {ev.title[:40]}",
                data={"kind": "event", "ticker": event_ticker},
            )
        _log.info(
            "tree_series_expand_done",
            series=series_ticker,
            elapsed_ms=int((_time.perf_counter() - _t0) * 1000),
            event_count=len(events),
        )

    def _engine_pairs_for_event(self, kalshi_event_ticker: str) -> list[str]:
        """Return pair-level event_tickers currently in GameManager for this event."""
        if self._engine is None:
            return []
        gm = getattr(self._engine, "_game_manager", None)
        if gm is None or not hasattr(gm, "_games"):
            return []
        out: list[str] = []
        for pt, pair in gm._games.items():
            pair_kalshi = getattr(pair, "kalshi_event_ticker", None) or getattr(
                pair, "event_ticker", None
            )
            if pair_kalshi == kalshi_event_ticker:
                out.append(pt)
        return out

    def _current_event_state(self, kalshi_event_ticker: str) -> str:
        """Return the display state for this event.

        Values: "checked", "deliberately_unticked", "winding", "empty".
        """
        # Winding-down takes precedence (user is in the middle of unticking)
        if self._engine is not None:
            winding = getattr(self._engine, "_winding_down", set())
            for pt in self._engine_pairs_for_event(kalshi_event_ticker):
                if pt in winding:
                    return "winding"

        # Currently monitored?
        if self._engine_pairs_for_event(kalshi_event_ticker):
            return "checked"

        # Deliberately unticked?
        if self._metadata is not None and self._metadata.is_deliberately_unticked(
            kalshi_event_ticker
        ):
            return "deliberately_unticked"

        return "empty"

    def _effective_state(self, kalshi_event_ticker: str) -> str:
        """Current state + staged overrides. Used to render the tickbox glyph.

        If the event has a staged add -> overrides to "checked" (pending).
        If the event has a staged remove -> overrides to "empty" (pending).
        """
        current = self._current_event_state(kalshi_event_ticker)
        staged_add = any(
            r.kalshi_event_ticker == kalshi_event_ticker for r in self.staged_changes.to_add
        )
        if staged_add:
            return "checked"
        # staged_remove is a list of pair_tickers; check if any for this event
        pair_tickers = set(self._engine_pairs_for_event(kalshi_event_ticker))
        staged_remove_overlap = any(pt in pair_tickers for pt in self.staged_changes.to_remove)
        if staged_remove_overlap:
            return "empty"
        return current

    async def action_manual_refresh(self) -> None:
        if self._discovery is not None:
            await self._discovery.bootstrap()

    def toggle_event_by_ticker(self, kalshi_event_ticker: str) -> None:
        """State-aware toggle.

        - Never-ticked -> stage add.
        - Currently monitored -> stage remove + set_unticked.
        - Deliberately unticked -> stage add + clear_unticked.
        - Winding-down -> stage add (re-engage trading).
        Staged-add / staged-remove get unstaged if toggled again.
        """
        import structlog as _structlog

        _log = _structlog.get_logger()
        _log.info(
            "tree_toggle_called",
            kalshi_event_ticker=kalshi_event_ticker,
            current_state=self._current_event_state(kalshi_event_ticker)
            if self._discovery
            else "no_discovery",
        )
        if self._discovery is None:
            return

        # Locate the event in the discovery cache so we can build records
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

        # If we can't find it in the discovery cache but it's currently
        # monitored, we can still untick (we don't need the event metadata
        # for the remove path).
        currently_monitored_pairs = self._engine_pairs_for_event(kalshi_event_ticker)

        # Stage-unstage first: if already in to_add, unstage it
        existing_adds = [
            r for r in self.staged_changes.to_add if r.kalshi_event_ticker == kalshi_event_ticker
        ]
        if existing_adds:
            for r in list(existing_adds):
                self.staged_changes.to_add.remove(r)
            # Also drop any to_clear_unticked we added alongside
            if kalshi_event_ticker in self.staged_changes.to_clear_unticked:
                self.staged_changes.to_clear_unticked.remove(kalshi_event_ticker)
            return

        # If staged remove, unstage it
        staged_remove_overlap = [
            pt for pt in self.staged_changes.to_remove if pt in currently_monitored_pairs
        ]
        if staged_remove_overlap:
            for pt in list(staged_remove_overlap):
                self.staged_changes.to_remove.remove(pt)
            # Also drop any to_set_unticked flag
            if kalshi_event_ticker in self.staged_changes.to_set_unticked:
                self.staged_changes.to_set_unticked.remove(kalshi_event_ticker)
            return

        # Fresh toggle — route by current state
        current_state = self._current_event_state(kalshi_event_ticker)

        if current_state in ("checked", "winding"):
            # Untick: stage remove for every pair + set_unticked flag
            for pt in currently_monitored_pairs:
                if pt not in self.staged_changes.to_remove:
                    self.staged_changes.to_remove.append(pt)
            if kalshi_event_ticker not in self.staged_changes.to_set_unticked:
                self.staged_changes.to_set_unticked.append(kalshi_event_ticker)
            return

        # current_state in ("empty", "deliberately_unticked") -> stage adds.
        # We need the event's market list to build ArbPairRecords.
        if event_node is None or series_ref is None or cat_ref is None:
            # Can't build records without discovery data (e.g., event aged
            # out of discovery cache). No-op.
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
                    expected_expiration_time=(
                        event_node.expected_expiration_time.isoformat()
                        if event_node.expected_expiration_time
                        else None
                    ),
                    volume_24h_a=mkt.volume_24h,
                    volume_24h_b=mkt.volume_24h,
                )
            )

        # If previously deliberately_unticked, schedule clear on commit
        if (
            current_state == "deliberately_unticked"
            and kalshi_event_ticker not in self.staged_changes.to_clear_unticked
        ):
            self.staged_changes.to_clear_unticked.append(kalshi_event_ticker)

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

    async def commit(self) -> bool:
        """Push staged changes through Engine and reconcile metadata.

        Pre-commit: any staged add whose event has no milestone, no manual
        override, and no sports GSR coverage triggers the SchedulePopup so
        the user can enter an event-start time (or explicitly opt out of
        exit-only scheduling) BEFORE the engine is touched. Cancelling the
        popup aborts the commit and preserves staged_changes.
        """
        import structlog as _structlog

        _log = _structlog.get_logger()
        _log.info(
            "tree_commit_start",
            to_add=len(self.staged_changes.to_add),
            to_remove=len(self.staged_changes.to_remove),
            to_set_unticked=len(self.staged_changes.to_set_unticked),
            to_clear_unticked=len(self.staged_changes.to_clear_unticked),
        )
        if self._engine is None or self._metadata is None:
            _log.info("tree_commit_aborted", reason="no_engine_or_metadata")
            return False

        # Pre-commit validation: prompt for any uncurated events.
        needs_schedule = self._events_needing_schedule()
        _log.info("tree_commit_validator", needs_schedule_count=len(needs_schedule))
        if needs_schedule:
            from talos.ui.schedule_popup import SchedulePopup

            popup = SchedulePopup(needs_schedule)
            result = await self.app.push_screen_wait(popup)
            if result is None:
                # User cancelled — abort commit, preserve staged_changes.
                self.app.notify("Commit cancelled.", severity="warning")
                return False
            # Merge popup-provided schedules into staged.to_set_manual_start.
            self.staged_changes.to_set_manual_start.update(result)

        staged = self.staged_changes

        # 1. Apply manual_event_start FIRST so the resolver cascade sees the
        #    override before the engine ever ticks for the new pairs.
        # Wrap in try/except so a tree_metadata.json write failure (disk
        # full, file locked) doesn't silently drop the manual override —
        # that would leave the resolver cascade with no schedule source
        # for the event and the engine would force-flip to exit-only or
        # trade unprotected. Preserve staging so user can retry.
        try:
            for k, v in staged.to_set_manual_start.items():
                self._metadata.set_manual_event_start(k, v)
        except Exception as exc:
            _log.warning(
                "tree_commit_metadata_write_failed",
                phase="set_manual_event_start",
                exc_type=type(exc).__name__,
                exc_msg=str(exc),
            )
            self.app.notify(
                f"Could not persist manual event-start ({type(exc).__name__}). "
                "Staged changes preserved — fix the disk issue and press 'c' "
                "again.",
                severity="error",
            )
            return False

        # 2. Engine add/remove.
        # add_pairs_from_selection now rolls back on partial failure and
        # re-raises. Catch here so the user gets a toast and staged_changes
        # are preserved for retry — propagating to the worker would log the
        # exception silently inside Textual and look like a hang.
        added: list[Any] = []
        remove_outcomes: list[Any] = []
        if staged.to_add:
            try:
                added = await self._engine.add_pairs_from_selection(
                    [r.model_dump() for r in staged.to_add]
                )
            except Exception as exc:
                _log.warning(
                    "tree_commit_add_failed",
                    exc_type=type(exc).__name__,
                    exc_msg=str(exc),
                )
                self.app.notify(
                    f"Add failed and was rolled back ({type(exc).__name__}). "
                    "Staged changes preserved — press 'c' again to retry.",
                    severity="error",
                )
                return False
        if staged.to_remove:
            try:
                remove_outcomes = await self._engine.remove_pairs_from_selection(
                    staged.to_remove,
                )
            except Exception as exc:
                # remove_pairs already mutated game_manager state in-memory
                # before _persist_active_games ran. We can't easily reverse
                # the removes (they don't keep enough metadata to restore),
                # so surface a hard warning: in-memory removal succeeded
                # but on restart the pairs will reappear from the stale
                # snapshot. User can re-commit after fixing the disk issue.
                _log.warning(
                    "tree_commit_remove_failed",
                    exc_type=type(exc).__name__,
                    exc_msg=str(exc),
                )
                self.app.notify(
                    f"Remove succeeded in memory but persistence failed "
                    f"({type(exc).__name__}). On restart removed pairs may "
                    "reappear from the stale snapshot. Fix the disk issue "
                    "and re-commit to make the removal durable.",
                    severity="error",
                )
                return False

        # 3. Apply deferred/applied unticked per §5.1a rules.
        #    - set_unticked applied when ALL staged pairs for the event came
        #      back "removed". Mixed/winding → defer via _deferred_set_unticked.
        #    - clear_unticked applied when ALL staged pairs for the event
        #      were successfully added.
        # Engine state has already been mutated at this point — if metadata
        # writes fail, we surface a hard warning rather than silently
        # losing the deliberately_unticked flags. Restart would otherwise
        # show the events as still-eligible-for-trade instead of
        # deliberately-skipped.
        staged_remove_set = set(staged.to_remove)
        try:
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
        except Exception as exc:
            _log.warning(
                "tree_commit_metadata_write_failed",
                phase="set_deliberately_unticked",
                exc_type=type(exc).__name__,
                exc_msg=str(exc),
            )
            self.app.notify(
                f"Engine state updated but deliberately-unticked flags "
                f"could not persist ({type(exc).__name__}). On restart "
                "the affected events may reappear as eligible. Fix the "
                "disk issue and re-commit to make the unticks durable.",
                severity="error",
            )

        added_keys = {(p.kalshi_event_ticker or p.event_ticker) for p in added}
        try:
            for k in staged.to_clear_unticked:
                if k in added_keys:
                    self._metadata.clear_deliberately_unticked(k)
        except Exception as exc:
            _log.warning(
                "tree_commit_metadata_write_failed",
                phase="clear_deliberately_unticked",
                exc_type=type(exc).__name__,
                exc_msg=str(exc),
            )
            self.app.notify(
                f"Engine state updated but deliberately-unticked clears "
                f"could not persist ({type(exc).__name__}). Affected events "
                "may stay marked as deliberately-unticked across restart "
                "until you re-commit.",
                severity="error",
            )

        # 4. Clear staged.
        self.staged_changes = StagedChanges.empty()
        return True

    def on_event_fully_removed(self, kalshi_event_ticker: str) -> None:
        """Engine listener callback: promote deferred [·] to applied."""
        if kalshi_event_ticker in self._deferred_set_unticked:
            if self._metadata is not None:
                self._metadata.promote_pending_to_applied(kalshi_event_ticker)
            self._deferred_set_unticked.discard(kalshi_event_ticker)

    # ── Keybinding actions ───────────────────────────────────────────────

    async def action_toggle_current_node(self) -> None:
        """Toggle the currently-highlighted tree node.

        Dispatches by kind:
          - event  : tick/untick the single event
          - series : bulk tick/untick every active event in the series
          - category: bulk tick/untick across every series in the category
        """
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return
        data = node.data if isinstance(node.data, dict) else {}
        kind = data.get("kind")
        if kind == "event":
            ticker = data.get("ticker")
            if ticker:
                self.toggle_event_by_ticker(ticker)
                self._refresh_node_label(node, ticker)
                # Propagate tick-state up through series → cluster → category
                p = node.parent
                while p is not None:
                    self._relabel_node(p)
                    p = p.parent
        elif kind == "series":
            series_ticker = data.get("ticker")
            if series_ticker:
                await self._bulk_toggle_series(series_ticker)
        elif kind == "cluster":
            tickers = data.get("tickers") or []
            cluster_name = data.get("cluster_name", "cluster")
            if tickers:
                await self._bulk_toggle_series_list(cluster_name, list(tickers))
        elif kind == "category":
            category_name = data.get("name")
            if category_name:
                await self._bulk_toggle_category(category_name)

    async def _bulk_toggle_series(self, series_ticker: str) -> None:
        """Stage-tick every active event in the series, or untick all if
        every event is already ticked/staged-ticked. Fetches events lazily
        if the series hasn't been drilled into yet."""
        if self._discovery is None:
            return
        events = await self._discovery.get_events_for_series(series_ticker)
        if not events:
            self.notify(
                f"No active events to select under {series_ticker}.",
                severity="warning",
            )
            return
        tickers = list(events.keys())
        action, toggled = self._apply_bulk_toggle(tickers)
        self._find_and_relabel_event_nodes(set(tickers))
        self.notify(
            f"{series_ticker}: {action} {toggled}/{len(tickers)} events.",
            severity="information",
        )

    async def _bulk_toggle_category(self, category_name: str) -> None:
        """Bulk-toggle every visible series in the category."""
        if self._discovery is None:
            return
        cat = self._discovery.categories.get(category_name)
        if cat is None:
            return
        visible = self._visible_series(cat)
        if not visible:
            self.notify(
                f"No visible series under {category_name}.",
                severity="warning",
            )
            return
        await self._bulk_toggle_series_list(
            category_name, [s.ticker for s in visible]
        )

    async def _bulk_toggle_series_list(
        self, label: str, series_tickers: list[str]
    ) -> None:
        """Bulk-toggle every active event across the given series tickers.

        Fail-closed: if ANY per-series fetch fails (rate limit, network
        blip, parse error), the whole bulk toggle is aborted with a
        user-visible warning. Partial success would otherwise persist as
        "you ticked 18 of 25 series silently" which the user couldn't
        detect until commit time. Retry surfaces transient failures
        without changing engine state.
        """
        import asyncio as _asyncio

        if self._discovery is None or not series_tickers:
            return
        self.notify(
            f"{label}: fetching events for {len(series_tickers)} series…",
            severity="information",
        )
        results = await _asyncio.gather(
            *(self._discovery.get_events_for_series(t) for t in series_tickers),
            return_exceptions=True,
        )

        # Identify failures BEFORE staging any toggles. Collecting per-ticker
        # results lets us point to specific series in the failure toast so
        # the user knows which to retry rather than just "something failed".
        failures: list[tuple[str, BaseException]] = []
        all_tickers: list[str] = []
        for series_ticker, res in zip(series_tickers, results, strict=True):
            if isinstance(res, BaseException):
                failures.append((series_ticker, res))
            elif isinstance(res, dict):
                all_tickers.extend(res.keys())

        if failures:
            sample = ", ".join(f"{t} ({type(e).__name__})" for t, e in failures[:3])
            extra = f" +{len(failures) - 3} more" if len(failures) > 3 else ""
            self.notify(
                f"{label}: aborted — {len(failures)} series failed to fetch "
                f"({sample}{extra}). Press space again to retry.",
                severity="error",
            )
            return

        if not all_tickers:
            self.notify(
                f"{label}: no active events found.",
                severity="warning",
            )
            return

        action, toggled = self._apply_bulk_toggle(all_tickers)
        self._find_and_relabel_event_nodes(set(all_tickers))
        self.notify(
            f"{label}: {action} {toggled}/{len(all_tickers)} events.",
            severity="information",
        )

    def _apply_bulk_toggle(self, tickers: list[str]) -> tuple[str, int]:
        """Apply a tick/untick sweep over a batch of event tickers.

        Rule: if EVERY ticker's current effective state is "checked", we
        untick all of them; otherwise we tick all of them (skipping ones
        already ticked). Mirrors how filesystem "Select All" toggles
        between select-all and select-none.

        Returns (action_label, count_toggled) for the user-facing toast.
        """
        states = {et: self._effective_state(et) for et in tickers}
        all_checked = all(s == "checked" for s in states.values())

        toggled = 0
        if all_checked:
            for et in tickers:
                if states[et] == "checked":
                    self.toggle_event_by_ticker(et)
                    toggled += 1
            return ("unticked", toggled)

        for et in tickers:
            if states[et] != "checked":
                self.toggle_event_by_ticker(et)
                toggled += 1
        return ("ticked", toggled)

    def _find_and_relabel_event_nodes(self, event_tickers: set[str]) -> None:
        """Walk the tree and refresh labels for any event nodes whose
        ticker is in the given set, then propagate the aggregate state
        upward by relabeling each changed node's ancestors. Used after
        bulk operations so glyphs update without tearing down the tree."""
        tree = self.query_one("#tree", Tree)
        dirty_ancestors: set[int] = set()

        def walk(node: TreeNode) -> None:
            data = node.data if isinstance(node.data, dict) else {}
            if data.get("kind") == "event":
                et = data.get("ticker")
                if et in event_tickers:
                    self._refresh_node_label(node, et)
                    p = node.parent
                    while p is not None and id(p) not in dirty_ancestors:
                        dirty_ancestors.add(id(p))
                        self._relabel_node(p)
                        p = p.parent
            for child in node.children:
                walk(child)

        walk(tree.root)

    def _relabel_series_node(self, node: TreeNode, series_ticker: str) -> None:
        """After a drill-in, update the series node's label and propagate
        the fresh count upward through any cluster → category ancestors.
        Unlike before, we don't assume parent==category; clusters now sit
        between them.
        """
        if self._discovery is None:
            return
        for cat in self._discovery.categories.values():
            if series_ticker in cat.series:
                node.set_label(
                    self._series_label(series_ticker, cat.series[series_ticker])
                )
                p = node.parent
                while p is not None:
                    self._relabel_node(p)
                    p = p.parent
                return

    def _relabel_node(self, node: TreeNode) -> None:
        """Generic relabel for any node based on its kind.

        Category → _category_label; cluster → _cluster_label; series →
        _series_label; event → _refresh_node_label. Used by both the
        drill-in and bulk-toggle paths to propagate state upward without
        each caller having to know what sits above the current row.
        """
        if self._discovery is None:
            return
        data = node.data if isinstance(node.data, dict) else {}
        kind = data.get("kind")
        if kind == "category":
            cat = self._discovery.categories.get(data.get("name", ""))
            if cat:
                node.set_label(self._category_label(cat.name, cat))
        elif kind == "cluster":
            cat = self._discovery.categories.get(data.get("category", ""))
            if cat is None:
                return
            tickers = data.get("tickers") or []
            members = [cat.series[t] for t in tickers if t in cat.series]
            node.set_label(self._cluster_label(data.get("cluster_name", ""), members))
        elif kind == "series":
            ticker = data.get("ticker", "")
            for cat in self._discovery.categories.values():
                if ticker in cat.series:
                    node.set_label(self._series_label(ticker, cat.series[ticker]))
                    return
        elif kind == "event":
            ticker = data.get("ticker", "")
            if ticker:
                self._refresh_node_label(node, ticker)

    def _refresh_node_label(self, node: TreeNode, kalshi_event_ticker: str) -> None:
        """Update an event leaf's glyph to match the current (post-toggle)
        state. Ancestor propagation is handled by the caller
        (_find_and_relabel_event_nodes walks upward from each changed leaf).
        """
        state = self._effective_state(kalshi_event_ticker)
        glyph = self._glyph_for_state(state)
        data = node.data if isinstance(node.data, dict) else {}
        ticker = data.get("ticker", kalshi_event_ticker)
        node.set_label(f"{glyph} {ticker}")

    def action_commit_changes(self) -> None:
        """Run the commit flow — pushes staged changes through Engine.

        Dispatches to a worker because commit() awaits push_screen_wait()
        for the SchedulePopup. The earlier `exclusive=True` would cancel
        an in-flight commit on a second `c` press; cancellation through
        an async commit chain (restore_game → adjuster → resolve_batch →
        feed.subscribe) leaves engine state half-mutated unless every
        layer's rollback catches BaseException, which is fragile. Instead
        we make commit non-reentrant: a second `c` while a commit is in
        flight is rejected with a toast.
        """
        if self.staged_changes.is_empty():
            self.notify("No staged changes to commit.", severity="information")
            return
        if getattr(self, "_commit_in_flight", False):
            self.notify(
                "Commit already in progress — wait for it to finish.",
                severity="warning",
            )
            return
        self._commit_in_flight = True
        # If run_worker itself raises synchronously (screen unmounted, app
        # shutting down, worker creation error), the worker's `finally`
        # block never runs and _commit_in_flight stays True forever — every
        # subsequent commit would be rejected as "in progress" until the
        # screen was recreated. Clear the flag on synchronous failure.
        try:
            self.run_worker(self._commit_worker())
        except Exception as exc:
            self._commit_in_flight = False
            self.notify(
                f"Commit could not start ({type(exc).__name__}): {exc}",
                severity="error",
            )
            raise

    async def _commit_worker(self) -> None:
        try:
            completed = await self.commit()
            if not completed:
                return
            self.notify("Commit complete.", severity="information")
            self._rebuild_tree()
        finally:
            self._commit_in_flight = False
