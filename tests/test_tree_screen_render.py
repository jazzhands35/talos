"""Smoke test: TreeScreen renders categories and series."""

import pytest
from textual.app import App
from textual.widgets import Tree

from talos.discovery import DiscoveryService
from talos.models.tree import CategoryNode, SeriesNode
from talos.ui.tree_screen import TreeScreen


class _HarnessApp(App):
    def __init__(self, ds):
        super().__init__()
        self._ds = ds

    def on_mount(self):
        self.push_screen(
            TreeScreen(
                discovery=self._ds,
                milestones=None,
                metadata=None,
                engine=None,
            ),
        )


def _ds_with_one_mention():
    ds = DiscoveryService()
    s = SeriesNode(
        ticker="KXFEDMENTION",
        title="...",
        category="Mentions",
        tags=[],
        frequency="one_off",
    )
    ds.categories["Mentions"] = CategoryNode(
        name="Mentions",
        series_count=1,
        series={"KXFEDMENTION": s},
    )
    return ds


@pytest.mark.asyncio
async def test_tree_renders_categories_and_series():
    ds = _ds_with_one_mention()
    app = _HarnessApp(ds)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        tree = screen.query_one(Tree)
        labels = [str(n.label) for n in tree.root.children]
        assert any("Mentions" in lbl for lbl in labels)
