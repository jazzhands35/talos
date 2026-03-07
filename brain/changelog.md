# Brain Changelog

Dated entries recording what was added or changed in the brain vault.

## 2026-03-07

### Added
- **decisions.md**: `scenario_pnl` uses total costs, not per-contract averages — documents the P&L truncation bug root cause and fix rationale
- **decisions.md**: Bid modal falls back to `all_snapshots` — documents why `get_opportunity()` alone is insufficient for row click handlers
- **patterns.md**: "Financial calculation precision" — carry exact values through the pipeline, format only at display boundary; integer division truncation compounds linearly with contract count
- **changelog.md**: Created this file

### Fixed
- **decisions.md**: Broken internal link `[[principles#14. Test Purity Drives Architecture]]` → corrected to `#13`
- **codebase/index.md**: Updated `scenario_pnl` gotcha to reflect total-cost signature and link to new pattern
- **architecture.md**: Layer 4 status updated from freeform "in progress" to consistent `**IN PROGRESS**` format; noted bid modal `all_snapshots` fallback

### Updated
- **patterns.md**: TUI dependency injection example now includes `tracker` parameter
