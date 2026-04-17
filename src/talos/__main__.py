"""Entry point: python -m talos."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TextIO
from io import StringIO

import structlog

_LOG_FILE_HANDLE: TextIO | None = None


class _TeeTextIO:
    """Write text to multiple streams."""

    def __init__(self, *streams: TextIO) -> None:
        self._streams = streams

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def _configure_logging(*, log_path: Path | None = None, stderr: TextIO | None = None) -> None:
    """Configure structlog, optionally teeing output to a file."""
    global _LOG_FILE_HANDLE

    stream: TextIO = stderr or StringIO()
    tee_stderr = os.environ.get("TALOS_LOG_TEE_STDERR", "").strip() == "1"
    if log_path is None:
        raw = os.environ.get("TALOS_LOG_FILE", "").strip()
        if raw:
            log_path = Path(raw)

    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _LOG_FILE_HANDLE = log_path.open("a", encoding="utf-8")
        if tee_stderr:
            stream = _TeeTextIO(stream, _LOG_FILE_HANDLE)
        else:
            stream = _LOG_FILE_HANDLE

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        logger_factory=structlog.WriteLoggerFactory(file=stream),
        cache_logger_on_first_use=True,
    )


def _close_logging() -> None:
    """Close any file-backed startup logger stream."""
    global _LOG_FILE_HANDLE

    if _LOG_FILE_HANDLE is not None:
        _LOG_FILE_HANDLE.close()
        _LOG_FILE_HANDLE = None


def _load_dotenv() -> None:
    """Load .env file from data directory if it exists."""
    from talos.persistence import get_data_dir

    env_file = get_data_dir() / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def _run_first_time_setup() -> None:
    """Collect credentials via plain console prompts (Ctrl+V paste works)."""
    from talos.persistence import get_data_dir
    from talos.ui.first_run import write_default_settings, write_env_file

    data_dir = get_data_dir()

    print()
    print("=" * 50)
    print("  Talos — First-Time Setup")
    print("=" * 50)
    print()
    key_id = input("  Kalshi API Key ID: ").strip()
    if not key_id:
        print("Error: API Key ID is required")
        sys.exit(1)
    key_path = input("  Path to RSA private key file: ").strip()
    if not key_path:
        print("Error: Private key path is required")
        sys.exit(1)
    if not Path(key_path).is_file():
        print(f"Error: key file not found: {key_path}")
        sys.exit(1)

    write_env_file(data_dir / ".env", key_id=key_id, key_path=key_path)
    write_default_settings(data_dir / "settings.json")
    print()
    print(f"  Saved to {data_dir / '.env'}")
    print("  Starting Talos...")
    print()


async def _run_diagnostics() -> None:
    """Test credentials and print detailed results for debugging."""
    import time

    import httpx

    from talos.auth import KalshiAuth
    from talos.config import KalshiConfig

    print()
    print("=" * 55)
    print("  Talos Diagnostic")
    print("=" * 55)

    try:
        config = KalshiConfig.from_env()
    except ValueError as e:
        print(f"\n  [FAIL] Config: {e}")
        return

    print(f"\n  Environment: {config.environment.value}")
    print(f"  Key ID:      {config.key_id}")
    print(f"  Key path:    {config.private_key_path}")
    print(f"  Key exists:  {config.private_key_path.is_file()}")

    if not config.private_key_path.is_file():
        print("\n  [FAIL] Private key file not found!")
        return

    # Check key file format
    raw = config.private_key_path.read_bytes()
    print(f"  Key size:    {len(raw)} bytes")
    first_line = raw.split(b"\n")[0].strip()
    print(f"  Key header:  {first_line.decode(errors='replace')}")
    if raw[:3] == b"\xef\xbb\xbf":
        print("  [WARN] Key file has UTF-8 BOM — this may cause auth failures!")

    # Load key
    try:
        auth = KalshiAuth(config.key_id, config.private_key_path)
        print("  Key loaded:  OK")
    except Exception as e:
        print(f"\n  [FAIL] Key load error: {e}")
        return

    # Check system time
    local_ts = int(time.time() * 1000)
    print(f"\n  Local time (ms): {local_ts}")

    # Test REST API
    print(f"\n  Testing REST: {config.rest_base_url}/exchange/status ...")
    async with httpx.AsyncClient() as client:
        # Unauthenticated health check
        try:
            r = await client.get(f"{config.rest_base_url}/exchange/status")
            print(f"  Exchange status: HTTP {r.status_code} — {r.text[:200]}")
        except Exception as e:
            print(f"  [FAIL] Can't reach Kalshi: {e}")
            return

        # Authenticated call
        print("\n  Testing auth: GET /portfolio/balance ...")
        headers = auth.headers("GET", "/trade-api/v2/portfolio/balance")
        print(f"  Auth headers: KEY={headers['KALSHI-ACCESS-KEY'][:12]}...")
        print(f"  Timestamp:    {headers['KALSHI-ACCESS-TIMESTAMP']}")
        print(f"  Signature:    {headers['KALSHI-ACCESS-SIGNATURE'][:30]}...")

        try:
            r = await client.get(
                f"{config.rest_base_url}/portfolio/balance",
                headers=headers,
            )
            print(f"  Response:     HTTP {r.status_code}")
            print(f"  Body:         {r.text[:300]}")
            if r.status_code == 200:
                print("\n  [OK] Authentication works!")
            elif r.status_code == 401:
                print("\n  [FAIL] 401 Unauthorized — key ID and private key don't match,")
                print("         or the API key was revoked on Kalshi's dashboard.")
            elif r.status_code == 403:
                print("\n  [FAIL] 403 Forbidden — account may not have API access.")
            else:
                print(f"\n  [FAIL] Unexpected status {r.status_code}")
        except Exception as e:
            print(f"  [FAIL] Request error: {e}")

    print()
    input("  Press Enter to close...")


def main() -> None:
    """Launch the Talos dashboard."""
    _configure_logging()

    # Frozen mode (PyInstaller): set data dir to exe's directory
    if getattr(sys, "frozen", False):
        from talos.persistence import set_data_dir

        set_data_dir(Path(sys.executable).parent)

    _load_dotenv()

    # Diagnostic mode: --diag flag
    if "--diag" in sys.argv:
        import asyncio

        asyncio.run(_run_diagnostics())
        return

    # Production-only guard for frozen builds
    if getattr(sys, "frozen", False) and os.environ.get("KALSHI_ENV") != "production":
        os.environ["KALSHI_ENV"] = "production"

    from talos.config import KalshiConfig

    try:
        config = KalshiConfig.from_env()
    except ValueError:
        # No .env yet — launch first-run setup if frozen, else error out
        if getattr(sys, "frozen", False):
            _run_first_time_setup()
            # Reload .env and retry — exit if still broken
            _load_dotenv()
            try:
                config = KalshiConfig.from_env()
            except ValueError:
                print("Setup did not complete — exiting.")
                sys.exit(1)
        else:
            print("Configuration error — create a .env file (see .env.example)")
            sys.exit(1)

    from talos.auth import KalshiAuth
    from talos.automation_config import DEFAULT_UNIT_SIZE, AutomationConfig
    from talos.bid_adjuster import BidAdjuster
    from talos.data_collector import DataCollector
    from talos.engine import TradingEngine
    from talos.game_manager import DEFAULT_NONSPORTS_CATEGORIES, GameManager
    from talos.game_status import GameStatusResolver
    from talos.lifecycle_feed import LifecycleFeed
    from talos.market_feed import MarketFeed
    from talos.orderbook import OrderBookManager
    from talos.persistence import (
        load_saved_games,
        load_saved_games_full,
        load_settings,
        save_games,
        save_games_full,
        save_settings,
    )
    from talos.portfolio_feed import PortfolioFeed
    from talos.position_feed import PositionFeed
    from talos.rest_client import KalshiRESTClient
    from talos.scanner import ArbitrageScanner
    from talos.settlement_tracker import SettlementCache
    from talos.suggestion_log import SuggestionLog
    from talos.ticker_feed import TickerFeed
    from talos.top_of_market import TopOfMarketTracker
    from talos.ui.app import TalosApp
    from talos.ws_client import KalshiWSClient

    auth = KalshiAuth(config.key_id, config.private_key_path)
    rest = KalshiRESTClient(auth, config)
    ws = KalshiWSClient(auth, config)
    books = OrderBookManager()
    feed = MarketFeed(ws, books)
    scanner = ArbitrageScanner(books)
    tracker = TopOfMarketTracker(books)
    settings = load_settings()
    unit_size = int(settings.get("unit_size", DEFAULT_UNIT_SIZE))  # type: ignore[arg-type]
    from talos.persistence import get_data_dir

    _db_dir = get_data_dir()
    data_collector = DataCollector(_db_dir / "talos_data.db")
    adjuster = BidAdjuster(
        books, [], unit_size=unit_size, data_collector=data_collector
    )
    portfolio_feed = PortfolioFeed(ws_client=ws)
    ticker_feed = TickerFeed(ws_client=ws)
    lifecycle_feed = LifecycleFeed(ws_client=ws)
    position_feed = PositionFeed(ws_client=ws)
    auto_config = AutomationConfig()
    nonsports_categories = settings.get("nonsports_categories", DEFAULT_NONSPORTS_CATEGORIES)
    nonsports_max_days = int(settings.get("nonsports_max_days", 7))  # type: ignore[arg-type]
    ticker_blacklist = settings.get("ticker_blacklist", [])
    game_mgr = GameManager(
        rest,
        feed,
        scanner,
        sports_enabled=auto_config.sports_enabled,
        nonsports_categories=nonsports_categories,  # type: ignore[arg-type]
        nonsports_max_days=nonsports_max_days,
        ticker_blacklist=ticker_blacklist,  # type: ignore[arg-type]
    )
    game_status_resolver = GameStatusResolver()
    db_dir = _db_dir
    settlement_cache = SettlementCache(db_dir / "talos_data.db")

    # Wire scanner + tracker to book updates
    _app_ref: list[TalosApp] = []  # populated after app creation

    _engine_ref: list[TradingEngine] = []  # populated after engine creation

    def on_book_update(ticker: str) -> None:
        scanner.scan(ticker)
        affected_pairs = scanner.pairs_for_ticker(ticker)
        # Check both sides for each pair using this ticker
        for pair in affected_pairs:
            for side_str in {pair.side_a, pair.side_b}:
                tracker.check(ticker, side=side_str)
        # Mark affected events dirty for table refresh + imbalance check
        if _app_ref:
            for pair in affected_pairs:
                _app_ref[0].mark_event_dirty(pair.event_ticker)
        if _engine_ref:
            for pair in affected_pairs:
                _engine_ref[0].mark_event_dirty(pair.event_ticker)

    feed.on_book_update = on_book_update

    # Wire game persistence — save full pair data for instant startup
    saved_games_full = load_saved_games_full()
    saved_games = load_saved_games() if saved_games_full is None else []

    def _persist_games() -> None:
        save_games([p.event_ticker for p in game_mgr.active_games])
        games_data = []
        for p in game_mgr.active_games:
            entry: dict[str, object] = {
                "event_ticker": p.event_ticker,
                "ticker_a": p.ticker_a,
                "ticker_b": p.ticker_b,
                "fee_type": p.fee_type,
                "fee_rate": p.fee_rate,
                "close_time": p.close_time,
                "expected_expiration_time": p.expected_expiration_time,
                "label": game_mgr.labels.get(p.event_ticker, ""),
                "sub_title": game_mgr.subtitles.get(p.event_ticker, ""),
                "side_a": p.side_a,
                "side_b": p.side_b,
                "kalshi_event_ticker": p.kalshi_event_ticker,
                "series_ticker": p.series_ticker,
                "talos_id": p.talos_id,
            }
            # Phase 1: persist tree-mode durability fields.
            # - source is observability only; write only when set.
            # - engine_state is safety-critical; always write (default "active").
            if p.source is not None:
                entry["source"] = p.source
            entry["engine_state"] = p.engine_state
            # Persist volume data so it's available instantly on restart
            vol_a = game_mgr.volumes_24h.get(p.ticker_a)
            vol_b = game_mgr.volumes_24h.get(p.ticker_b)
            if vol_a is not None:
                entry["volume_a"] = vol_a
            if vol_b is not None:
                entry["volume_b"] = vol_b
            try:
                ledger = adjuster.get_ledger(p.event_ticker)
                entry["ledger"] = ledger.to_save_dict()
            except KeyError:
                pass
            games_data.append(entry)
        save_games_full(games_data)

    game_mgr.on_change = _persist_games

    engine = TradingEngine(
        scanner=scanner,
        game_manager=game_mgr,
        rest_client=rest,
        market_feed=feed,
        tracker=tracker,
        adjuster=adjuster,
        initial_games=saved_games,
        initial_games_full=saved_games_full,
        automation_config=auto_config,
        portfolio_feed=portfolio_feed,
        ticker_feed=ticker_feed,
        lifecycle_feed=lifecycle_feed,
        position_feed=position_feed,
        game_status_resolver=game_status_resolver,
        data_collector=data_collector,
        settlement_cache=settlement_cache,
    )

    startup_execution_mode = str(settings.get("execution_mode", "automatic"))
    startup_auto_stop_hours = settings.get("auto_stop_hours", None)
    if startup_auto_stop_hours is not None:
        startup_auto_stop_hours = float(startup_auto_stop_hours)  # type: ignore[arg-type]

    # Wire unit size persistence
    def _persist_unit_size(size: int) -> None:
        s = load_settings()
        s["unit_size"] = size
        save_settings(s)

    _engine_ref.append(engine)
    engine.on_unit_size_change = _persist_unit_size

    # Wire blacklist persistence
    def _persist_blacklist(blacklist: list[str]) -> None:
        s = load_settings()
        s["ticker_blacklist"] = blacklist
        save_settings(s)

    engine.on_blacklist_change = _persist_blacklist

    # Wire suggestion audit log
    log_path = get_data_dir() / "suggestions.log"
    suggestion_log = SuggestionLog(log_path)
    engine.proposal_queue.on_lifecycle = suggestion_log.log

    app = TalosApp(
        engine=engine,
        startup_execution_mode=startup_execution_mode,
        startup_auto_stop_hours=startup_auto_stop_hours,
    )

    # Restore persisted scan mode and wire persistence
    saved_scan_mode = settings.get("scan_mode", "sports")
    if isinstance(saved_scan_mode, str) and saved_scan_mode in ("sports", "nonsports"):
        app.set_scan_mode(saved_scan_mode)

    def _persist_scan_mode(mode: str) -> None:
        s = load_settings()
        s["scan_mode"] = mode
        save_settings(s)

    app.on_scan_mode_change = _persist_scan_mode

    _app_ref.append(app)
    try:
        app.run()
    except Exception:
        # Capture crash traceback so it's not lost when the window closes
        import traceback
        from datetime import UTC, datetime

        from talos.persistence import get_data_dir

        crash_log = get_data_dir() / "talos_crash.log"
        with open(crash_log, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"CRASH at {datetime.now(UTC).isoformat()}\n")
            f.write(f"{'='*60}\n")
            traceback.print_exc(file=f)
        raise


if __name__ == "__main__":
    main()
