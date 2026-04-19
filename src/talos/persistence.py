"""Game list persistence — saves/loads event tickers to a JSON file."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

import structlog

logger = structlog.get_logger()


# Current schema version for games_full.json. Bump when changing the
# wire format in a way that requires migration. v0 = bare list (legacy);
# v1 = {"schema_version": 1, "games": [...]} with safety-critical fields.
GAMES_FULL_SCHEMA_VERSION = 1


class GamesFullCorruptError(Exception):
    """games_full.json exists but cannot be parsed or fails validation.

    Raised by load_saved_games_full when the snapshot is present but
    structurally invalid. Callers should fail-closed (refuse to silently
    fall back to the ticker-only file) because partial restore could
    miss safety-critical fields like engine_state and resurrect a
    winding-down pair as freely tradable.
    """


# Backwards-compatible alias — earlier callers may already import this.
GamesFullCorrupt = GamesFullCorruptError


def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` via temp file + os.replace.

    Same-directory temp file ensures os.replace is atomic on POSIX and
    Windows (both require source/dest on the same filesystem). fsync
    before replace so a power loss after replace can't yield a renamed-
    but-empty file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Cleanup the orphan temp file so retries don't accumulate.
        with contextlib.suppress(Exception):
            tmp_path.unlink(missing_ok=True)
        raise

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
        _atomic_write_text(games_file, json.dumps(tickers, indent=2) + "\n")
    except Exception:
        logger.debug("save_games_failed", path=str(games_file))


def save_games_full(games: list[dict[str, str | float | None]], path: Path | None = None) -> None:
    """Save full game data so startup can skip REST calls.

    Wraps the games list in a versioned envelope so future schema
    changes can be detected on load. Atomic write via temp file +
    os.replace prevents a torn save from silently downgrading restart
    state to the legacy ticker-only file.
    """
    games_file = _games_full_file(path)
    envelope = {
        "schema_version": GAMES_FULL_SCHEMA_VERSION,
        "games": games,
    }
    try:
        _atomic_write_text(games_file, json.dumps(envelope, indent=2) + "\n")
    except Exception:
        logger.debug("save_games_full_failed", path=str(games_file))


def load_saved_games_full(
    path: Path | None = None,
) -> list[dict[str, str | float]] | None:
    """Load full game data.

    Return value semantics:
      - None: file does not exist (legitimate first run).
      - list[dict]: parsed successfully.
      - GamesFullCorrupt raised: file exists but cannot be parsed or
        fails validation. Callers should fail-closed and refuse to
        silently fall back to the ticker-only legacy file — that
        fallback drops the engine_state and source fields and would
        resurrect a winding-down pair as freely tradable.

    Accepts both the legacy bare-list format (schema v0) and the
    current versioned envelope (schema v1+). Bare-list saves are
    auto-migrated in-memory; the next save_games_full() rewrites in
    the new format.
    """
    games_file = _games_full_file(path)
    if not games_file.is_file():
        return None
    try:
        raw = json.loads(games_file.read_text())
    except json.JSONDecodeError as exc:
        logger.warning(
            "load_saved_games_full_unparseable",
            path=str(games_file),
            exc_msg=str(exc),
        )
        raise GamesFullCorrupt(
            f"games_full.json exists at {games_file} but is not valid JSON: {exc}"
        ) from exc

    # Versioned envelope
    if isinstance(raw, dict):
        version = raw.get("schema_version")
        games = raw.get("games")
        if not isinstance(version, int) or not isinstance(games, list):
            raise GamesFullCorrupt(
                f"games_full.json at {games_file} has invalid envelope shape"
            )
        if version > GAMES_FULL_SCHEMA_VERSION:
            raise GamesFullCorrupt(
                f"games_full.json schema_version={version} is newer than "
                f"this build supports (max {GAMES_FULL_SCHEMA_VERSION})"
            )
        if not all(isinstance(g, dict) for g in games):
            raise GamesFullCorrupt(
                f"games_full.json at {games_file} has non-dict game entries"
            )
        # Safety-critical field validation. engine_state became load-bearing
        # in v1 — a v1 save without it is corrupt, not a silent-default
        # candidate. We can't infer whether the pair was winding_down or
        # active, so refusing to start is safer than guessing wrong.
        if version >= 1:
            for idx, g in enumerate(games):
                if "engine_state" not in g:
                    raise GamesFullCorrupt(
                        f"games_full.json at {games_file} entry {idx} "
                        f"(event_ticker={g.get('event_ticker', '?')}) is "
                        f"missing engine_state — cannot safely restore"
                    )
        return games  # type: ignore[return-value]

    # Legacy bare-list format (schema v0). Accept for backward compat;
    # v0 predates the winding_down concept so missing engine_state means
    # "was active before we started persisting it" → safe to default.
    # rewriting on next save migrates to the versioned envelope.
    if isinstance(raw, list):
        if not raw:
            return None
        if not isinstance(raw[0], dict):
            raise GamesFullCorrupt(
                f"games_full.json at {games_file} has non-dict first entry"
            )
        logger.info("load_saved_games_full_legacy_migrated", path=str(games_file))
        # Stamp engine_state on v0 entries so the v1 invariant holds after
        # auto-migration — restore_game then sees the field explicitly.
        for entry in raw:
            if isinstance(entry, dict) and "engine_state" not in entry:
                entry["engine_state"] = "active"
        return raw  # type: ignore[return-value]

    raise GamesFullCorrupt(
        f"games_full.json at {games_file} has unrecognized top-level type "
        f"({type(raw).__name__})"
    )


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
        _atomic_write_text(settings_file, json.dumps(settings, indent=2) + "\n")
    except Exception:
        logger.debug("save_settings_failed", path=str(settings_file))


# ---------------------------------------------------------------------------
# Tree metadata persistence
# ---------------------------------------------------------------------------
def _tree_metadata_file(path: Path | None = None) -> Path:
    return path or (get_data_dir() / "tree_metadata.json")


_TREE_METADATA_DEFAULTS: dict[str, object] = {
    "version": 1,
    "event_first_seen": {},
    "event_reviewed_at": {},
    "manual_event_start": {},
    "deliberately_unticked": [],
    "deliberately_unticked_pending": [],
}


def _default_copy(v: object) -> object:
    """Copy mutable defaults (dict/list) to avoid shared-reference bugs."""
    if isinstance(v, dict):
        return {}
    if isinstance(v, list):
        return []
    return v


def load_tree_metadata(path: Path | None = None) -> dict[str, object]:
    """Load tree_metadata.json. Returns defaults if missing or corrupt.

    Forward-compatible: any missing keys from older versions are backfilled
    with their default value, so tests / callers can assume all keys exist.
    """
    f = _tree_metadata_file(path)
    if not f.is_file():
        return {k: _default_copy(v) for k, v in _TREE_METADATA_DEFAULTS.items()}
    try:
        parsed = json.loads(f.read_text())
        if not isinstance(parsed, dict):
            raise ValueError("tree_metadata must be a JSON object")
        data: dict[str, object] = parsed
    except Exception:
        logger.warning("load_tree_metadata_failed", path=str(f))
        return {k: _default_copy(v) for k, v in _TREE_METADATA_DEFAULTS.items()}

    # Backfill missing keys
    for k, default in _TREE_METADATA_DEFAULTS.items():
        if k not in data:
            data[k] = _default_copy(default)
    return data


def save_tree_metadata(data: dict[str, object], path: Path | None = None) -> None:
    """Persist tree_metadata.json."""
    f = _tree_metadata_file(path)
    try:
        _atomic_write_text(f, json.dumps(data, indent=2) + "\n")
    except Exception:
        logger.debug("save_tree_metadata_failed", path=str(f))
