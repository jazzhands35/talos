# Talos

Kalshi arbitrage trading system. Manual-first with progressive automation.

## Setup

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows (Git Bash)
pip install -e ".[dev]"
```

## Development

```bash
pytest                    # run tests
ruff check src/ tests/    # lint
ruff format src/ tests/   # format
pyright                   # type check
```
