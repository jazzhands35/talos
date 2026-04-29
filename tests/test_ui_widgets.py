"""Tests for proposer-table display rendering."""

from __future__ import annotations


def test_id_cell_renders_yy_mm_nnn() -> None:
    """The # column must format the int talos_id as YY.MM.NNN."""
    from talos.ui.widgets import _format_id_cell

    assert _format_id_cell(2604188) == "26.04.188"


def test_id_cell_renders_unassigned_as_dash() -> None:
    from talos.ui.widgets import _format_id_cell

    assert _format_id_cell(0) == "—"
