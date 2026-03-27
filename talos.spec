# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Talos.exe — single-file distributable."""

from PyInstaller.utils.hooks import collect_data_files

textual_datas = collect_data_files("textual")

a = Analysis(
    ["src/talos/__main__.py"],
    pathex=["src"],
    datas=textual_datas,
    hiddenimports=[
        # Talos core
        "talos", "talos.auth", "talos.config", "talos.errors",
        "talos.engine", "talos.scanner", "talos.game_manager",
        "talos.bid_adjuster", "talos.rebalance", "talos.fees",
        "talos.persistence", "talos.orderbook", "talos.rest_client",
        "talos.ws_client", "talos.game_status", "talos.automation_config",
        "talos.market_feed", "talos.ticker_feed", "talos.portfolio_feed",
        "talos.position_feed", "talos.lifecycle_feed",
        "talos.top_of_market", "talos.position_ledger",
        "talos.opportunity_proposer", "talos.proposal_queue",
        "talos.auto_accept", "talos.auto_accept_log",
        "talos.suggestion_log", "talos.data_collector",
        "talos.settlement_tracker", "talos.cpm",
        # Talos UI
        "talos.ui", "talos.ui.app", "talos.ui.theme",
        "talos.ui.widgets", "talos.ui.screens", "talos.ui.first_run",
        "talos.ui.proposal_panel", "talos.ui.event_review",
        # Talos models
        "talos.models", "talos.models.market", "talos.models.order",
        "talos.models.portfolio", "talos.models.strategy", "talos.models.ws",
        # Dependencies
        "structlog", "httpx", "httpx._transports",
        "pydantic", "pydantic._internal", "websockets",
        # Textual internals
        "textual", "textual.css", "textual.widgets", "textual.screen",
        "textual._xterm_parser", "textual._animator",
        # Cryptography (RSA signing)
        "cryptography.hazmat.primitives.asymmetric.padding",
        "cryptography.hazmat.primitives.asymmetric.rsa",
        "cryptography.hazmat.primitives.hashes",
        "cryptography.hazmat.primitives.serialization",
    ],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="Talos",
    icon="icon.ico",
    console=True,
    onefile=True,
)
