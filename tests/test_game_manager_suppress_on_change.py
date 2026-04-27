"""Tests the suppress_on_change context manager: pauses callback, restores on exit (including on
exception), nested suppress restores correctly.
"""

from unittest.mock import MagicMock

from talos.game_manager import GameManager


def _make_game_manager() -> GameManager:
    # Minimal fixture — only need .on_change and suppress_on_change to work.
    # Round-7 plan: stack-based suppression requires _suppressed_on_change_stack.
    gm = GameManager.__new__(GameManager)
    gm._suppressed_on_change_stack = []
    gm.on_change = None
    return gm


def test_suppress_on_change_pauses_callback():
    gm = _make_game_manager()
    cb = MagicMock()
    gm.on_change = cb

    with gm.suppress_on_change():
        # Simulate an internal mutation that would fire on_change
        if gm.on_change:
            gm.on_change()  # should be None here → no call

    cb.assert_not_called()


def test_suppress_on_change_restores_callback():
    gm = _make_game_manager()
    cb = MagicMock()
    gm.on_change = cb

    with gm.suppress_on_change():
        pass

    assert gm.on_change is cb

    # And firing after the block works
    if gm.on_change:
        gm.on_change()
    cb.assert_called_once()


def test_suppress_on_change_restores_on_exception():
    gm = _make_game_manager()
    cb = MagicMock()
    gm.on_change = cb

    try:
        with gm.suppress_on_change():
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    assert gm.on_change is cb


def test_suppress_on_change_nested_restores_correctly():
    """Round-7 plan: stack-based suppression supports arbitrarily-deep
    nesting. Outer callback must be restored exactly after nested
    suppress blocks exit."""
    gm = _make_game_manager()
    cb = MagicMock()
    gm.on_change = cb

    with gm.suppress_on_change():
        assert gm.on_change is None
        with gm.suppress_on_change():
            assert gm.on_change is None
        assert gm.on_change is None
    assert gm.on_change is cb


def test_suppressed_on_change_finds_outer_callback_in_nested_suppress():
    """Round-7 plan Fix #2 (round-3 v0.1.1 finding #1): the bypass
    accessor walks the stack for the nearest non-None entry, so
    force_during_suppress=True works from inside a nested suppress
    block. Returning the top of stack would be None (because outer
    suppress already cleared on_change before inner pushed)."""
    gm = _make_game_manager()
    cb = MagicMock()
    gm.on_change = cb

    with gm.suppress_on_change():
        assert gm.suppressed_on_change is cb
        with gm.suppress_on_change():
            # Stack top is None (the outer's on_change=None pushed in),
            # but the accessor walks deeper to find cb.
            assert gm.suppressed_on_change is cb


def test_suppressed_on_change_returns_none_when_not_suppressed():
    gm = _make_game_manager()
    gm.on_change = MagicMock()
    assert gm.suppressed_on_change is None
