"""Game list persistence — saves/loads event tickers to a JSON file."""

from __future__ import annotations

import json
from pathlib import Path

import structlog

logger = structlog.get_logger()

_GAMES_FILE = Path(__file__).resolve().parents[2] / "games.json"
_SETTINGS_FILE = Path(__file__).resolve().parents[2] / "settings.json"


def load_saved_games(path: Path | None = None) -> list[str]:
    """Load saved game event tickers from disk."""
    games_file = path or _GAMES_FILE
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
    games_file = path or _GAMES_FILE
    try:
        games_file.write_text(json.dumps(tickers, indent=2) + "\n")
    except Exception:
        logger.debug("save_games_failed", path=str(games_file))


_GAMES_FULL_FILE = Path(__file__).resolve().parents[2] / "games_full.json"


def save_games_full(
    games: list[dict[str, str | float | None]], path: Path | None = None
) -> None:
    """Save full game data so startup can skip REST calls."""
    games_file = path or _GAMES_FULL_FILE
    try:
        games_file.write_text(json.dumps(games, indent=2) + "\n")
    except Exception:
        logger.debug("save_games_full_failed", path=str(games_file))


def load_saved_games_full(
    path: Path | None = None,
) -> list[dict[str, str | float]] | None:
    """Load full game data. Returns None if not available (fallback to tickers)."""
    games_file = path or _GAMES_FULL_FILE
    if not games_file.is_file():
        return None
    try:
        data = json.loads(games_file.read_text())
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data
    except Exception:
        logger.debug("load_saved_games_full_failed", path=str(games_file))
    return None


def load_settings(path: Path | None = None) -> dict[str, object]:
    """Load persisted settings from disk."""
    settings_file = path or _SETTINGS_FILE
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
    settings_file = path or _SETTINGS_FILE
    try:
        settings_file.write_text(json.dumps(settings, indent=2) + "\n")
    except Exception:
        logger.debug("save_settings_failed", path=str(settings_file))
