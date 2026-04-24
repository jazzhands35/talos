"""pytest configuration: route structlog through stdlib logging so caplog works.

This conftest overrides pytest's built-in caplog fixture to also configure
structlog to emit via stdlib logging. Tests that do not request caplog are
unaffected — structlog keeps its default (WriteLoggerFactory) configuration.
"""
import logging
from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

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
    try:
        with caplog.at_level(logging.DEBUG):
            yield caplog
    finally:
        structlog.reset_defaults()


@pytest.fixture()
def engine_fixture():
    """Minimal TradingEngine with a shape-aware ``_rest.get_market`` stub.

    The REST stub returns a ``Market`` whose shape varies by ticker prefix:
      * ``KXF-`` → fractional_trading_enabled=True (admission rejects)
      * ``KXS-`` → sub-cent tick (admission rejects)
      * anything else → ordinary cent-tick, open status (admission admits)

    Every other collaborator (game_manager, adjuster, feed, GSR,
    data_collector, persist) is stubbed out just enough to let
    ``add_pairs_from_selection`` run end-to-end without touching disk
    or the network. Pattern mirrors ``tests/test_engine_add_pairs_from_selection.py``.
    """
    from talos.engine import TradingEngine
    from talos.models.market import Market
    from talos.models.strategy import ArbPair

    e = cast(Any, TradingEngine.__new__(TradingEngine))

    def _restore(record):
        return ArbPair(
            event_ticker=record["event_ticker"],
            ticker_a=record["ticker_a"],
            ticker_b=record["ticker_b"],
            side_a=record.get("side_a", "yes"),
            side_b=record.get("side_b", "no"),
            source=record.get("source"),
        )

    gm = MagicMock()
    gm.restore_game = MagicMock(side_effect=_restore)
    gm.subtitles = {}
    gm.volumes_24h = {}
    gm._volumes_24h = gm.volumes_24h
    gm._games = {}

    @contextmanager
    def _suppress():
        yield

    gm.suppress_on_change = MagicMock(side_effect=_suppress)
    gm.remove_game = AsyncMock()
    e._game_manager = gm

    e._adjuster = MagicMock()
    e._game_status_resolver = MagicMock()
    e._game_status_resolver.resolve_batch = AsyncMock(return_value={})
    e._game_status_resolver.get = MagicMock(return_value=None)
    e._feed = MagicMock()
    e._feed.subscribe = AsyncMock()
    e._feed.unsubscribe = AsyncMock()
    e._data_collector = None
    e._persist_active_games = MagicMock()

    async def _get_market(ticker: str) -> Market:
        if ticker.startswith("KXF-"):
            return Market(
                ticker=ticker,
                event_ticker=ticker.rsplit("-", 1)[0],
                title=f"Fractional {ticker}",
                status="open",
                fractional_trading_enabled=True,
            )
        if ticker.startswith("KXS-"):
            return Market.model_validate({
                "ticker": ticker,
                "event_ticker": ticker.rsplit("-", 1)[0],
                "title": f"Sub-cent {ticker}",
                "status": "open",
                "price_ranges": [
                    {
                        "min_price_dollars": "0.01",
                        "max_price_dollars": "0.99",
                        "tick_dollars": "0.001",
                    }
                ],
            })
        return Market(
            ticker=ticker,
            event_ticker=ticker.rsplit("-", 1)[0] if "-" in ticker else ticker,
            title=f"Market {ticker}",
            status="open",
        )

    e._rest = MagicMock()
    e._rest.get_market = AsyncMock(side_effect=_get_market)
    return e
