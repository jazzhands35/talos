import pytest
from textual.app import App

from talos.ui.tree_screen import TreeScreen


class _HarnessApp(App):
    def on_mount(self):
        self.push_screen(
            TreeScreen(discovery=None, milestones=None, metadata=None, engine=None),
        )


@pytest.mark.asyncio
async def test_tree_screen_can_be_instantiated():
    app = _HarnessApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert True
