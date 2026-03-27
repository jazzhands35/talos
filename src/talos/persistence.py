"""Game list persistence — saves/loads event tickers to a JSON file."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Configurable data directory
# ---------------------------------------------------------------------------
_data_dir: Path | None = None


def set_data_dir(path: Path | None) -> None:
    """Override the base directory for all runtime files.

    Call before any other persistence function. Pass None to reset.
    """
    global _data_dir
    _data_dir = path


def get_data_dir() -> Path:
    """Return the data directory.

    Resolution order:
    1. Explicitly set via set_data_dir()
    2. PyInstaller frozen → directory containing the exe
    3. Development → two parents up from this file (project root)
    """
    if _data_dir is not None:
        return _data_dir
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Path helpers (resolve against get_data_dir at call time, not import time)
# ---------------------------------------------------------------------------
def _games_file(path: Path | None = None) -> Path:
    return path or (get_data_dir() / "games.json")


def _settings_file(path: Path | None = None) -> Path:
    return path or (get_data_dir() / "settings.json")


def _games_full_file(path: Path | None = None) -> Path:
    return path or (get_data_dir() / "games_full.json")


# ---------------------------------------------------------------------------
# Games persistence
# ---------------------------------------------------------------------------
def load_saved_games(path: Path | None = None) -> list[str]:
    """Load saved game event tickers from disk."""
    games_file = _games_file(path)
    if not games_file.is_file():
        return []
    try:
        data = json.loads(games_file.read_text())
        if isinstance(data, list):
            return [str(t) for t in data if isinstance(t, str)]
    except Exception:
        logger.debug("load_saved_games_failed", path=str(games_file))
    return []


def save_games(tickers: list[str], path: Path | None = None) -> None:
    """Save game event tickers to disk (legacy format)."""
    games_file = _games_file(path)
    try:
        games_file.write_text(json.dumps(tickers, indent=2) + "\n")
    except Exception:
        logger.debug("save_games_failed", path=str(games_file))


def save_games_full(
    games: list[dict[str, str | float | None]], path: Path | None = None
) -> None:
    """Save full game data so startup can skip REST calls."""
    games_file = _games_full_file(path)
    try:
        games_file.write_text(json.dumps(games, indent=2) + "\n")
    except Exception:
        logger.debug("save_games_full_failed", path=str(games_file))


def load_saved_games_full(
    path: Path | None = None,
) -> list[dict[str, str | float]] | None:
    """Load full game data. Returns None if not available (fallback to tickers)."""
    games_file = _games_full_file(path)
    if not games_file.is_file():
        return None
    try:
        data = json.loads(games_file.read_text())
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data
    except Exception:
        logger.debug("load_saved_games_full_failed", path=str(games_file))
    return None


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------
def load_settings(path: Path | None = None) -> dict[str, object]:
    """Load persisted settings from disk."""
    settings_file = _settings_file(path)
    if not settings_file.is_file():
        return {}
    try:
        data = json.loads(settings_file.read_text())
        if isinstance(data, dict):
            return data
    except Exception:
        logger.debug("load_settings_failed", path=str(settings_file))
    return {}


def save_settings(settings: dict[str, object], path: Path | None = None) -> None:
    """Save settings to disk."""
    settings_file = _settings_file(path)
    try:
        settings_file.write_text(json.dumps(settings, indent=2) + "\n")
    except Exception:
        logger.debug("save_settings_failed", path=str(settings_file))
