"""Catppuccin Mocha theme for Talos TUI."""

from __future__ import annotations

# Catppuccin Mocha palette
BASE = "#1e1e2e"
MANTLE = "#181825"
CRUST = "#11111b"
SURFACE0 = "#313244"
SURFACE1 = "#45475a"
SURFACE2 = "#585b70"
OVERLAY0 = "#6c7086"
TEXT = "#cdd6f4"
SUBTEXT0 = "#a6adc8"
SUBTEXT1 = "#bac2de"
BLUE = "#89b4fa"
GREEN = "#a6e3a1"
RED = "#f38ba8"
YELLOW = "#f9e2af"
MAUVE = "#cba6f7"
PEACH = "#fab387"
LAVENDER = "#b4befe"

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

#opportunities-table {{
    height: 1fr;
    min-height: 8;
    border: solid {SURFACE1};
    background: {BASE};
}}

#bottom-panels {{
    height: auto;
    max-height: 12;
    layout: horizontal;
}}

#account-panel {{
    width: 2fr;
    border: solid {SURFACE1};
    background: {SURFACE0};
    padding: 0 1;
    height: auto;
    max-height: 12;
}}

#order-log {{
    width: 3fr;
    border: solid {SURFACE1};
    background: {SURFACE0};
    padding: 0 1;
    height: auto;
    max-height: 12;
}}

.panel-title {{
    color: {BLUE};
    text-style: bold;
}}

.dim-row {{
    color: {OVERLAY0};
}}

.edge-positive {{
    color: {GREEN};
}}

.status-connected {{
    color: {GREEN};
}}

.status-disconnected {{
    color: {RED};
}}

.order-filled {{
    color: {GREEN};
}}

.order-open {{
    color: {YELLOW};
}}

.order-cancelled {{
    color: {RED};
}}

/* Modal styling */
ModalScreen {{
    align: center middle;
}}

#modal-dialog {{
    width: 60;
    height: auto;
    max-height: 80%;
    border: thick {SURFACE1};
    background: {SURFACE0};
    padding: 1 2;
}}

#modal-dialog Label {{
    width: 100%;
    margin: 0 0 1 0;
}}

#modal-dialog TextArea {{
    height: 8;
    margin: 0 0 1 0;
}}

#modal-dialog Input {{
    margin: 0 0 1 0;
}}

#modal-buttons {{
    layout: horizontal;
    height: auto;
    align: right middle;
}}

#modal-buttons Button {{
    margin: 0 0 0 1;
}}

.modal-title {{
    color: {BLUE};
    text-style: bold;
    margin: 0 0 1 0;
}}

.modal-error {{
    color: {RED};
    margin: 0 0 1 0;
}}
"""
