"""Tests for DRIP config modal parsing."""

from __future__ import annotations

from talos.drip import DripConfig
from talos.ui.screens import DripConfigScreen


def test_drip_config_screen_parse_valid_values() -> None:
    assert DripConfigScreen.parse_config("1", "1", "5.0") == DripConfig(
        drip_size=1,
        max_drips=1,
        blip_delta_min=5.0,
    )


def test_drip_config_screen_rejects_invalid_values() -> None:
    assert DripConfigScreen.parse_config("abc", "1", "5.0") is None
    assert DripConfigScreen.parse_config("1", "0", "5.0") is None
    assert DripConfigScreen.parse_config("1", "2", "5.0") is None
    assert DripConfigScreen.parse_config("1", "1", "-1") is None


def test_drip_config_screen_strips_whitespace() -> None:
    assert DripConfigScreen.parse_config(" 2 ", " 1 ", " 3.5 ") == DripConfig(
        drip_size=2,
        max_drips=1,
        blip_delta_min=3.5,
    )
