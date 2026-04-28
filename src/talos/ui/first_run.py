"""First-run setup — collects Kalshi credentials on initial launch."""

from __future__ import annotations

import json
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Footer, Header, Input, Label, Static

from talos.automation_config import DEFAULT_UNIT_SIZE
from talos.errors import KalshiAPIError
from talos.ui.theme import APP_CSS


def write_env_file(path: Path, *, key_id: str, key_path: str) -> None:
    """Write a .env file with Kalshi production credentials."""
    path.write_text(
        f"KALSHI_KEY_ID={key_id}\nKALSHI_PRIVATE_KEY_PATH={key_path}\nKALSHI_ENV=production\n"
    )


def write_default_settings(path: Path) -> None:
    """Write default settings.json for new installs."""
    path.write_text(
        json.dumps(
            {
                "unit_size": DEFAULT_UNIT_SIZE,
                "ticker_blacklist": [],
                "execution_mode": "automatic",
                "auto_stop_hours": None,
            },
            indent=2,
        )
        + "\n"
    )


class SetupScreen(Static):
    """Credential entry form for first-time setup."""

    def compose(self) -> ComposeResult:
        yield Label("Talos — First-Time Setup", classes="modal-title")
        yield Label("")
        yield Label("Kalshi API Key ID:")
        yield Input(placeholder="e.g. abc123-def456-...", id="key-id-input")
        yield Label("")
        yield Label("Path to RSA Private Key file:")
        yield Input(placeholder=r"e.g. C:\Users\you\kalshi.key", id="key-path-input")
        yield Label("")
        yield Label("", id="setup-error", classes="modal-error")
        yield Button("Save & Launch", id="save-btn", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "save-btn":
            return
        key_id = self.query_one("#key-id-input", Input).value.strip()
        key_path = self.query_one("#key-path-input", Input).value.strip()
        error_label = self.query_one("#setup-error", Label)

        if not key_id:
            error_label.update("API Key ID is required")
            return
        if not key_path:
            error_label.update("Private key path is required")
            return
        if not Path(key_path).is_file():
            error_label.update(f"File not found: {key_path}")
            return

        # Validate authentication
        error_label.update("Validating credentials...")
        self.app.call_later(self._validate_and_save, key_id, key_path)

    async def _validate_and_save(self, key_id: str, key_path: str) -> None:
        """Attempt auth, then write config files and exit."""
        error_label = self.query_one("#setup-error", Label)
        try:
            from talos.auth import KalshiAuth
            from talos.config import KalshiConfig, KalshiEnvironment
            from talos.rest_client import KalshiRESTClient

            auth = KalshiAuth(key_id, Path(key_path))
            config = KalshiConfig(
                environment=KalshiEnvironment.PRODUCTION,
                key_id=key_id,
                private_key_path=Path(key_path),
                rest_base_url="https://api.elections.kalshi.com/trade-api/v2",
                ws_url="wss://api.elections.kalshi.com/trade-api/ws/v2",
            )
            rest = KalshiRESTClient(auth, config)
            await rest.get_balance()
        except FileNotFoundError:
            error_label.update("Could not read private key file — check the path")
            return
        except (ValueError, OSError) as e:
            error_label.update(f"Could not read private key file: {e}")
            return
        except KalshiAPIError as e:
            if e.status_code in (401, 403):
                error_label.update("Authentication failed — check your API key ID")
            else:
                error_label.update(f"API error ({e.status_code}): {e}")
            return
        except Exception as e:
            error_label.update(f"Could not reach Kalshi — check your internet: {e}")
            return

        # Success — write config files
        from talos.persistence import get_data_dir

        data_dir = get_data_dir()
        write_env_file(data_dir / ".env", key_id=key_id, key_path=key_path)
        write_default_settings(data_dir / "settings.json")
        error_label.update("Setup complete — restarting...")
        self.app.exit()


class FirstRunApp(App[None]):
    """Minimal Textual app for first-run credential setup."""

    CSS = APP_CSS
    TITLE = "TALOS SETUP"

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="setup-container"):
            yield SetupScreen()
        yield Footer()
