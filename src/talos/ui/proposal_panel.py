"""ProposalPanel — collapsible sidebar for pending proposals."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

from talos.models.proposal import ProposalKey
from talos.proposal_queue import ProposalQueue


class ProposalPanel(Vertical):
    """Collapsible right sidebar showing pending proposals."""

    DEFAULT_CSS = """
    ProposalPanel {
        dock: right;
        width: 50;
        background: #313244;
        border-left: solid #45475a;
        padding: 1;
        overflow-y: auto;
    }
    ProposalPanel .proposal-header {
        color: #89b4fa;
        text-style: bold;
        margin: 0 0 1 0;
    }
    ProposalPanel .proposal-row {
        padding: 0 1;
        margin: 0 0 0 0;
        height: 1;
    }
    ProposalPanel .proposal-row.--selected {
        background: #45475a;
    }
    ProposalPanel .proposal-row.--stale {
        opacity: 0.4;
    }
    ProposalPanel .proposal-detail {
        color: #a6adc8;
        padding: 0 1;
        margin: 0 0 1 0;
        height: 1;
    }
    ProposalPanel .proposal-hint {
        color: #6c7086;
        margin: 1 0 0 0;
    }
    """

    selected_index: reactive[int] = reactive(0)

    class Approved(Message):
        """Fired when operator approves a proposal."""

        def __init__(self, key: ProposalKey) -> None:
            super().__init__()
            self.key = key

    class Rejected(Message):
        """Fired when operator rejects a proposal."""

        def __init__(self, key: ProposalKey) -> None:
            super().__init__()
            self.key = key

    def __init__(
        self,
        queue: ProposalQueue,
        *,
        name: str | None = None,
        id: str | None = None,  # noqa: A002
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._queue = queue
        self._keys: list[ProposalKey] = []

    def compose(self) -> ComposeResult:
        yield Static("PROPOSALS", classes="proposal-header")
        yield Static("[Y] approve  [N] reject  [\u2191\u2193] select", classes="proposal-hint")

    def refresh_proposals(self) -> None:
        """Re-render from current queue state. Called by parent on timer."""
        pending = self._queue.pending()
        self.display = len(pending) > 0
        if not pending:
            self._keys = []
            return

        self._keys = [p.key for p in pending]
        # Clamp selected_index
        if self.selected_index >= len(self._keys):
            self.selected_index = max(0, len(self._keys) - 1)

        # Remove old dynamic rows (everything except header and hint)
        for child in list(self.children):
            if "proposal-row" in child.classes or "proposal-detail" in child.classes:
                child.remove()

        # Insert new rows before the hint widget
        hint = None
        for child in self.children:
            if "proposal-hint" in child.classes:
                hint = child
                break

        for i, proposal in enumerate(pending):
            classes = "proposal-row"
            if i == self.selected_index:
                classes += " --selected"
            if proposal.stale:
                classes += " --stale"
            row = Static(f"[{i + 1}] {proposal.summary}", classes=classes)
            detail = Static(f"    {proposal.detail}", classes="proposal-detail")
            if hint:
                self.mount(row, before=hint)
                self.mount(detail, before=hint)
            else:
                self.mount(row)
                self.mount(detail)

    def key_up(self) -> None:
        """Move selection up."""
        if self._keys:
            self.selected_index = max(0, self.selected_index - 1)
            self.refresh_proposals()

    def key_down(self) -> None:
        """Move selection down."""
        if self._keys:
            self.selected_index = min(len(self._keys) - 1, self.selected_index + 1)
            self.refresh_proposals()

    def key_y(self) -> None:
        """Approve selected proposal."""
        if self._keys and 0 <= self.selected_index < len(self._keys):
            self.post_message(self.Approved(self._keys[self.selected_index]))

    def key_n(self) -> None:
        """Reject selected proposal."""
        if self._keys and 0 <= self.selected_index < len(self._keys):
            self.post_message(self.Rejected(self._keys[self.selected_index]))
