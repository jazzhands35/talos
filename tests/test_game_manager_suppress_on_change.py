from unittest.mock import MagicMock

from talos.game_manager import GameManager


def _make_game_manager() -> GameManager:
    # Minimal fixture — only need .on_change and suppress_on_change to work
    return GameManager.__new__(GameManager)


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
