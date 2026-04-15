"""pytest configuration: route structlog through stdlib logging so caplog works.

This conftest overrides pytest's built-in caplog fixture to also configure
structlog to emit via stdlib logging. Tests that do not request caplog are
unaffected — structlog keeps its default (WriteLoggerFactory) configuration.
"""
import logging

import pytest
import structlog


@pytest.fixture()
def caplog(caplog):  # type: ignore[override]
    """Extended caplog that routes structlog through stdlib logging.

    Structlog is reconfigured for the duration of the test so that its output
    flows through Python's standard logging module, making it visible to the
    standard pytest caplog fixture. The configuration is restored afterwards.
    """
    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    with caplog.at_level(logging.DEBUG):
        yield caplog
    structlog.reset_defaults()
