"""Tests for the analyze_stale_recovery debugging tool — parses recovery-event cycles from log
output.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path("tools/analyze_stale_recovery.py")
    spec = importlib.util.spec_from_file_location("analyze_stale_recovery", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_cycles_reads_recovery_events():
    module = _load_module()
    log = Path("tests") / "_stale_recovery_sample.log"
    try:
        log.write_text(
            "\n".join(
                [
                    "2026-04-04 event=other thing=1",
                    (
                        "2026-04-04 stale_book_recovery_cycle stale_count=4 "
                        "active_stale_count=3 attempted_count=2 skipped_cooldown_count=1 "
                        "recovered_count=2 failed_count=0 elapsed_ms=17"
                    ),
                ]
            ),
            encoding="utf-8",
        )

        cycles = module._parse_cycles(log)

        assert cycles == [
            {
                "stale_count": 4,
                "active_stale_count": 3,
                "attempted_count": 2,
                "skipped_cooldown_count": 1,
                "recovered_count": 2,
                "failed_count": 0,
                "elapsed_ms": 17,
            }
        ]
    finally:
        log.unlink(missing_ok=True)
