"""Entry point: python -m talos."""

from talos.ui.app import TalosApp


def main() -> None:
    """Launch the Talos dashboard."""
    app = TalosApp()
    app.run()


if __name__ == "__main__":
    main()
