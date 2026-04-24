"""Catppuccin Mocha theme for Drip TUI."""

from __future__ import annotations

# Catppuccin Mocha palette (shared with Talos)
BASE = "#1e1e2e"
MANTLE = "#181825"
SURFACE0 = "#313244"
SURFACE1 = "#45475a"
SURFACE2 = "#585b70"
TEXT = "#cdd6f4"
SUBTEXT0 = "#a6adc8"
GREEN = "#a6e3a1"
RED = "#f38ba8"
YELLOW = "#f9e2af"
BLUE = "#89b4fa"
MAUVE = "#cba6f7"

APP_CSS = f"""
Screen {{
    background: {BASE};
    color: {TEXT};
}}

Header {{
    background: {MANTLE};
    color: {BLUE};
    dock: top;
    height: 1;
}}

Footer {{
    background: {MANTLE};
    dock: bottom;
}}

#top-panels {{
    height: auto;
    max-height: 10;
    layout: horizontal;
}}

#side-a-panel {{
    width: 1fr;
    border: solid {SURFACE1};
    background: {SURFACE0};
    padding: 0 1;
    height: auto;
    max-height: 10;
}}

#side-b-panel {{
    width: 1fr;
    border: solid {SURFACE1};
    background: {SURFACE0};
    padding: 0 1;
    height: auto;
    max-height: 10;
}}

#balance-panel {{
    width: 2fr;
    border: solid {SURFACE1};
    background: {SURFACE0};
    padding: 0 1;
    height: auto;
    max-height: 10;
}}

#action-log {{
    height: 1fr;
    min-height: 6;
    border: solid {SURFACE1};
    border-title-color: {BLUE};
    background: {SURFACE0};
    padding: 0 1;
}}

.panel-title {{
    color: {BLUE};
    text-style: bold;
}}

.delta-ok {{
    color: {GREEN};
}}

.delta-warn {{
    color: {YELLOW};
}}

.delta-danger {{
    color: {RED};
}}
"""
