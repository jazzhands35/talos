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

        screen.action_toggle_current_node()

        assert len(screen.staged_changes.to_add) == 1
        assert screen.staged_changes.to_add[0].kalshi_event_ticker == "KXFEDMENTION-26APR"
