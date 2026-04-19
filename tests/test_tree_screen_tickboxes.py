from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from textual.app import App
from textual.widgets import Tree

from talos.discovery import DiscoveryService
from talos.models.tree import CategoryNode, EventNode, MarketNode, SeriesNode
from talos.ui.tree_screen import TreeScreen


def _ds_with_event_and_market():
    ds = DiscoveryService()
    s = SeriesNode(
        ticker="KXFEDMENTION",
        title="...",
        category="Mentions",
        tags=[],
        frequency="one_off",
    )
    ev = EventNode(
        ticker="KXFEDMENTION-26APR",
        series_ticker="KXFEDMENTION",
        title="X",
    )
    ev.markets = [
        MarketNode(ticker="KXFEDMENTION-26APR-YIEL", title="Yield"),
    ]
    s.events = {"KXFEDMENTION-26APR": ev}
    ds.categories["Mentions"] = CategoryNode(
        name="Mentions",
        series_count=1,
        series={"KXFEDMENTION": s},
    )
    return ds


@pytest.mark.asyncio
async def test_tickbox_renders_empty_by_default():
    ds = _ds_with_event_and_market()

    class _App(App):
        def on_mount(self):
            self.push_screen(
                TreeScreen(
                    discovery=ds,
                    milestones=None,
                    metadata=None,
                    engine=None,
                ),
            )

    app = _App()
    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, TreeScreen)
        assert screen.staged_changes.is_empty()


@pytest.mark.asyncio
async def test_toggle_tickbox_stages_event():
    ds = _ds_with_event_and_market()

    class _App(App):
        def on_mount(self):
            self.push_screen(
                TreeScreen(
                    discovery=ds,
                    milestones=None,
                    metadata=None,
                    engine=None,
                ),
            )

    app = _App()
    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, TreeScreen)
        screen.toggle_event_by_ticker("KXFEDMENTION-26APR")
        assert len(screen.staged_changes.to_add) == 1
        assert screen.staged_changes.to_add[0].kalshi_event_ticker == "KXFEDMENTION-26APR"


@pytest.mark.asyncio
async def test_space_keybinding_toggles_current_event():
    """action_toggle_current_node on an event node routes to toggle_event_by_ticker.

    Drives the full Textual app harness, sets cursor via select_node, and
    asserts the action stages exactly one record for the event.
    """
    ds = _ds_with_event_and_market()

    class _App(App):
        def on_mount(self):
            self.push_screen(
                TreeScreen(
                    discovery=ds,
                    milestones=None,
                    metadata=None,
                    engine=None,
                ),
            )

    app = _App()
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, TreeScreen)
        tree = screen.query_one(Tree)

        # Inject an event node directly under root so we exercise the action
        # without relying on DiscoveryService's async fetch path (which would
        # hit the live Kalshi API in this test context).
        tree.root.remove_children()
        event_node = tree.root.add_leaf(
            "[ ] KXFEDMENTION-26APR   X",
            data={"kind": "event", "ticker": "KXFEDMENTION-26APR"},
        )
        tree.root.expand()
        await pilot.pause()
        tree.select_node(event_node)
        await pilot.pause()

        await screen.action_toggle_current_node()

        assert len(screen.staged_changes.to_add) == 1
        assert screen.staged_changes.to_add[0].kalshi_event_ticker == "KXFEDMENTION-26APR"


def _make_screen_with_engine(discovery_service, engine, metadata=None):
    screen = cast(Any, TreeScreen.__new__(TreeScreen))
    screen._discovery = discovery_service
    screen._milestones = None
    screen._metadata = metadata or MagicMock()
    screen._engine = engine
    from talos.models.tree import StagedChanges

    screen.staged_changes = StagedChanges.empty()
    screen._deferred_set_unticked = set()
    return screen


def _fake_engine_with_monitored(kalshi_event_ticker: str, pair_tickers: list[str]):
    engine = MagicMock()
    gm = MagicMock()
    gm._games = {}
    for pt in pair_tickers:
        pair = MagicMock()
        pair.event_ticker = pt
        pair.kalshi_event_ticker = kalshi_event_ticker
        gm._games[pt] = pair
    engine._game_manager = gm
    engine._winding_down = set()
    return engine


def test_current_state_checked_for_monitored_event():
    ds = _ds_with_event_and_market()
    engine = _fake_engine_with_monitored("KXFEDMENTION-26APR", ["KXFEDMENTION-26APR-YIEL"])
    md = MagicMock()
    md.is_deliberately_unticked.return_value = False
    screen = _make_screen_with_engine(ds, engine, md)
    assert screen._current_event_state("KXFEDMENTION-26APR") == "checked"


def test_current_state_deliberately_unticked():
    ds = _ds_with_event_and_market()
    engine = _fake_engine_with_monitored("KXFEDMENTION-26APR", [])  # not monitored
    md = MagicMock()
    md.is_deliberately_unticked.return_value = True
    screen = _make_screen_with_engine(ds, engine, md)
    assert screen._current_event_state("KXFEDMENTION-26APR") == "deliberately_unticked"


def test_current_state_winding_precedence():
    ds = _ds_with_event_and_market()
    engine = _fake_engine_with_monitored("KXFEDMENTION-26APR", ["KXFEDMENTION-26APR-YIEL"])
    engine._winding_down = {"KXFEDMENTION-26APR-YIEL"}
    md = MagicMock()
    md.is_deliberately_unticked.return_value = False
    screen = _make_screen_with_engine(ds, engine, md)
    assert screen._current_event_state("KXFEDMENTION-26APR") == "winding"


def test_toggle_checked_event_stages_remove_and_set_unticked():
    ds = _ds_with_event_and_market()
    engine = _fake_engine_with_monitored(
        "KXFEDMENTION-26APR",
        ["KXFEDMENTION-26APR-YIEL"],
    )
    md = MagicMock()
    md.is_deliberately_unticked.return_value = False
    screen = _make_screen_with_engine(ds, engine, md)

    screen.toggle_event_by_ticker("KXFEDMENTION-26APR")

    assert screen.staged_changes.to_add == []
    assert any(
        pt == "KXFEDMENTION-26APR-YIEL"
        for pt, _ in screen.staged_changes.to_remove
    )
    assert "KXFEDMENTION-26APR" in screen.staged_changes.to_set_unticked


def test_toggle_staged_remove_unstages():
    ds = _ds_with_event_and_market()
    engine = _fake_engine_with_monitored(
        "KXFEDMENTION-26APR",
        ["KXFEDMENTION-26APR-YIEL"],
    )
    md = MagicMock()
    md.is_deliberately_unticked.return_value = False
    screen = _make_screen_with_engine(ds, engine, md)

    screen.toggle_event_by_ticker("KXFEDMENTION-26APR")  # first tick -> stage remove
    assert any(
        pt == "KXFEDMENTION-26APR-YIEL"
        for pt, _ in screen.staged_changes.to_remove
    )

    screen.toggle_event_by_ticker("KXFEDMENTION-26APR")  # second tick -> unstage
    assert not any(
        pt == "KXFEDMENTION-26APR-YIEL"
        for pt, _ in screen.staged_changes.to_remove
    )
    assert "KXFEDMENTION-26APR" not in screen.staged_changes.to_set_unticked


def test_toggle_deliberately_unticked_stages_add_and_clear_unticked():
    ds = _ds_with_event_and_market()
    engine = _fake_engine_with_monitored("KXFEDMENTION-26APR", [])
    md = MagicMock()
    md.is_deliberately_unticked.return_value = True
    screen = _make_screen_with_engine(ds, engine, md)

    screen.toggle_event_by_ticker("KXFEDMENTION-26APR")

    assert len(screen.staged_changes.to_add) == 1
    assert screen.staged_changes.to_add[0].kalshi_event_ticker == "KXFEDMENTION-26APR"
    assert "KXFEDMENTION-26APR" in screen.staged_changes.to_clear_unticked
