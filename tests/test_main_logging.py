from __future__ import annotations

import io
from pathlib import Path

import structlog

from talos.__main__ import _close_logging, _configure_logging


def test_configure_logging_tees_to_file() -> None:
    stream = io.StringIO()
    log_path = Path("tests") / "_main_logging_test.log"
    log_path.unlink(missing_ok=True)
    try:
        _configure_logging(log_path=log_path, stderr=stream)
        structlog.get_logger().info("hello_world", sample=1)

        stderr_text = stream.getvalue()
        file_text = log_path.read_text(encoding="utf-8")
        assert stderr_text == ""
        assert "hello_world" in file_text
        assert "sample=1" in file_text
    finally:
        structlog.reset_defaults()
        _close_logging()
        log_path.unlink(missing_ok=True)
