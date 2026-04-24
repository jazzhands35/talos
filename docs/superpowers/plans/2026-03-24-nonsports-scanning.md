# Non-Sports Event Scanning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable Talos to discover and scan non-sports events (weather, crypto, politics, etc.) via broad paginated Kalshi API query with client-side category and close-time filtering.

**Architecture:** Add `get_all_events()` to REST client for cursor pagination. Modify `GameManager.scan_events()` to run a second non-sports scan path (broad query + filter) alongside the existing per-series sports path. Settings control which categories and time window. ScanScreen adapts rows for non-sports date/time/labels.

**Tech Stack:** Python 3.12+, httpx (async), Pydantic v2, Textual (TUI), pytest

**Spec:** `docs/superpowers/specs/2026-03-24-nonsports-scanning-design.md`

---

### Task 1: Add `series_ticker` to `ArbPair` model

**Files:**
- Modify: `src/talos/models/strategy.py:8-20`
- Modify: `src/talos/__main__.py:115-131` (persistence serialization)
- Test: `tests/test_game_manager.py`

- [ ] **Step 1: Add `series_ticker` field to `ArbPair`**

In `src/talos/models/strategy.py`, add one field to the `ArbPair` class:

```python
class ArbPair(BaseModel):
    """Two mutually exclusive markets within a game event."""

    event_ticker: str
    ticker_a: str
    ticker_b: str
    side_a: str = "no"
    side_b: str = "no"
    kalshi_event_ticker: str = ""
    series_ticker: str = ""  # NEW — for volume refresh and category display
    fee_type: str = "quadratic_with_maker_fees"
    fee_rate: float = 0.0175
    close_time: str | None = None
    expected_expiration_time: str | None = None
```

- [ ] **Step 2: Set `series_ticker` in `GameManager.add_game()` and `add_market_as_pair()`**

In `src/talos/game_manager.py`, update the `ArbPair(...)` construction in both methods to include `series_ticker=event.series_ticker`.

In `add_game()` (~line 251):
```python
pair = ArbPair(
    event_ticker=event.event_ticker,
    ticker_a=ticker_a,
    ticker_b=ticker_b,
    series_ticker=event.series_ticker,  # NEW
    fee_type=fee_type,
    ...
)
```

In `add_market_as_pair()` (~line 323):
```python
pair = ArbPair(
    event_ticker=market.ticker,
    ticker_a=market.ticker,
    ticker_b=market.ticker,
    side_a="yes",
    side_b="no",
    kalshi_event_ticker=event.event_ticker,
    series_ticker=event.series_ticker,  # NEW
    fee_type=fee_type,
    ...
)
```

- [ ] **Step 3: Add `series_ticker` to `games_full.json` serialization**

In `src/talos/__main__.py`, update `_persist_games()` (~line 113-131) to include the new field:

```python
{
    ...
    "kalshi_event_ticker": p.kalshi_event_ticker,
    "series_ticker": p.series_ticker,  # NEW
}
```

- [ ] **Step 4: Fix `refresh_volumes()` to use `pair.series_ticker`**

In `src/talos/game_manager.py`, update `refresh_volumes()` (~line 503-510):

```python
async def refresh_volumes(self) -> None:
    """Re-fetch 24h volume for all monitored markets, batched by series."""
    series_tickers: set[str] = set()
    for pair in self.active_games:
        st = pair.series_ticker or pair.event_ticker.split("-")[0]  # fallback for old data
        series_tickers.add(st)
    ...
```

- [ ] **Step 5: Update `restore_game()` to read `series_ticker` from persisted data**

In `src/talos/game_manager.py`, update `restore_game()` (~line 394) to read the new field:

```python
kalshi_event_ticker = str(data.get("kalshi_event_ticker", ""))
series_ticker = str(data.get("series_ticker", ""))  # NEW
```

And pass it to `ArbPair(...)` (~line 396):

```python
pair = ArbPair(
    event_ticker=event_ticker,
    ticker_a=ticker_a,
    ticker_b=ticker_b,
    side_a=side_a,
    side_b=side_b,
    kalshi_event_ticker=kalshi_event_ticker,
    series_ticker=series_ticker,  # NEW
    fee_type=str(data.get("fee_type", "quadratic_with_maker_fees")),
    ...
)
```

- [ ] **Step 7: Verify existing tests still pass**

Run: `.venv/Scripts/python -m pytest tests/test_game_manager.py -v`
Expected: All existing tests PASS (new field has default `""`, backward compatible).

- [ ] **Step 8: Commit**

```bash
git add src/talos/models/strategy.py src/talos/game_manager.py src/talos/__main__.py
git commit -m "feat: add series_ticker to ArbPair for volume refresh and category display"
```

---

### Task 2: Add `get_all_events()` to REST client

**Files:**
- Modify: `src/talos/rest_client.py:85-107`
- Test: `tests/test_rest_client.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_rest_client.py`:

```python
class TestGetAllEvents:
    """Tests for paginated get_all_events()."""

    async def test_single_page(
        self, client: KalshiRESTClient, mock_auth: KalshiAuth
    ) -> None:
        """Single page of results — no cursor returned."""
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request.return_value = _mock_response(200, {
            "events": [
                {
                    "event_ticker": "EVT-1",
                    "series_ticker": "SER-1",
                    "title": "Test",
                    "category": "Crypto",
                    "markets": [],
                }
            ],
            "cursor": "",
        })
        events = await client.get_all_events(status="open")
        assert len(events) == 1
        assert events[0].event_ticker == "EVT-1"

    async def test_multi_page(
        self, client: KalshiRESTClient, mock_auth: KalshiAuth
    ) -> None:
        """Two pages of results — follows cursor."""
        page1 = _mock_response(200, {
            "events": [
                {"event_ticker": "EVT-1", "series_ticker": "S", "title": "A", "category": "Crypto", "markets": []},
                {"event_ticker": "EVT-2", "series_ticker": "S", "title": "B", "category": "Crypto", "markets": []},
            ],
            "cursor": "abc123",
        })
        page2 = _mock_response(200, {
            "events": [
                {"event_ticker": "EVT-3", "series_ticker": "S", "title": "C", "category": "Crypto", "markets": []},
            ],
            "cursor": "",
        })
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request.side_effect = [page1, page2]
        events = await client.get_all_events(status="open", page_size=2)
        assert len(events) == 3
        assert [e.event_ticker for e in events] == ["EVT-1", "EVT-2", "EVT-3"]

    async def test_max_pages_safeguard(
        self, client: KalshiRESTClient, mock_auth: KalshiAuth
    ) -> None:
        """Stops after max_pages even if cursor keeps coming."""
        def _page(*args: object, **kwargs: object) -> httpx.Response:
            return _mock_response(200, {
                "events": [
                    {"event_ticker": "EVT", "series_ticker": "S", "title": "T", "category": "C", "markets": []},
                ],
                "cursor": "more",
            })
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request.side_effect = _page
        events = await client.get_all_events(status="open", max_pages=3)
        assert len(events) == 3
        assert client._http.request.call_count == 3

    async def test_empty_result(
        self, client: KalshiRESTClient, mock_auth: KalshiAuth
    ) -> None:
        """No events returned."""
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request.return_value = _mock_response(200, {
            "events": [],
            "cursor": "",
        })
        events = await client.get_all_events(status="open")
        assert events == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_rest_client.py::TestGetAllEvents -v`
Expected: FAIL — `get_all_events` does not exist yet.

- [ ] **Step 3: Implement `get_all_events()`**

Add to `src/talos/rest_client.py` after `get_events()` (~line 107):

```python
async def get_all_events(
    self,
    *,
    status: str | None = None,
    series_ticker: str | None = None,
    with_nested_markets: bool = False,
    min_close_ts: int | None = None,
    page_size: int = 200,
    max_pages: int = 20,
) -> list[Event]:
    """Fetch all events by paginating through cursor-based results.

    Stops when the cursor is empty, fewer results than page_size are
    returned, or max_pages is reached (safeguard against runaway queries).
    """
    all_events: list[Event] = []
    cursor: str | None = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"limit": page_size}
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/events", params=params)
        events = [Event.model_validate(e) for e in data["events"]]
        all_events.extend(events)
        cursor = data.get("cursor")
        if not cursor or len(events) < page_size:
            break
    return all_events
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_rest_client.py::TestGetAllEvents -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/talos/rest_client.py tests/test_rest_client.py
git commit -m "feat: add get_all_events() with cursor pagination and max_pages safeguard"
```

---

### Task 3: Add category label mapping to `widgets.py`

**Files:**
- Modify: `src/talos/ui/widgets.py:77+`
- Modify: `src/talos/ui/screens.py:24+`

- [ ] **Step 1: Add `_CATEGORY_SHORT` mapping to `widgets.py`**

Add after the `_SPORT_LEAGUE` dict (~line 130):

```python
# API category -> short label for non-sports display
_CATEGORY_SHORT: dict[str, str] = {
    "Climate and Weather": "Clim",
    "Crypto": "Cryp",
    "Companies": "Comp",
    "Politics": "Pol",
    "Science and Technology": "Sci",
    "Mentions": "Ment",
    "Entertainment": "Ent",
    "World": "Wrld",
    "Elections": "Elec",
    "Health": "Hlth",
}
```

- [ ] **Step 2: Add the same mapping to `screens.py`**

The `_SPORT_LEAGUE` dict is duplicated in `screens.py` to avoid circular imports (noted at line 23). Add `_CATEGORY_SHORT` there too, right after the `_SPORT_LEAGUE` duplicate (~line 70):

```python
_CATEGORY_SHORT: dict[str, str] = {
    "Climate and Weather": "Clim",
    "Crypto": "Cryp",
    "Companies": "Comp",
    "Politics": "Pol",
    "Science and Technology": "Sci",
    "Mentions": "Ment",
    "Entertainment": "Ent",
    "World": "Wrld",
    "Elections": "Elec",
    "Health": "Hlth",
}
```

- [ ] **Step 3: Commit**

```bash
git add src/talos/ui/widgets.py src/talos/ui/screens.py
git commit -m "feat: add _CATEGORY_SHORT mapping for non-sports display labels"
```

---

### Task 4: Add non-sports scan path to `GameManager.scan_events()`

**Files:**
- Modify: `src/talos/game_manager.py:100-105, 150-173, 532-571`
- Test: `tests/test_game_manager.py`

This is the core task. Modifies the `GameManager` constructor to accept category/max_days config, adds the broad-query non-sports scan path, and removes the empty `NON_SPORTS_SERIES` list.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_game_manager.py`:

```python
from datetime import datetime, timedelta, timezone


def _make_nonsports_event(
    event_ticker: str,
    series_ticker: str,
    category: str,
    close_time: str | None,
    *,
    market_count: int = 1,
    status: str = "active",
) -> Event:
    """Helper to create non-sports events for scan tests."""
    markets = [
        Market(
            ticker=f"{event_ticker}-MKT{i}",
            event_ticker=event_ticker,
            title=f"Market {i}",
            status=status,
            close_time=close_time,
        )
        for i in range(market_count)
    ]
    return Event(
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        title=f"Test {event_ticker}",
        category=category,
        status="open",
        markets=markets,
    )


class TestNonSportsScan:
    """Tests for non-sports scan path in scan_events()."""

    @pytest.fixture()
    def mock_rest(self) -> KalshiRESTClient:
        rest = MagicMock(spec=KalshiRESTClient)
        rest.get_event = AsyncMock()  # needed for add_game() in already-monitored test
        rest.get_events = AsyncMock(return_value=[])
        rest.get_all_events = AsyncMock(return_value=[])
        rest.get_series = AsyncMock(
            return_value=Series(
                series_ticker="S", title="S", category="Crypto",
                fee_type="quadratic_with_maker_fees", fee_multiplier=0.0175,
            )
        )
        return rest

    @pytest.fixture()
    def manager(self, mock_rest: KalshiRESTClient) -> GameManager:
        feed = MagicMock(spec=MarketFeed)
        feed.subscribe = AsyncMock()
        feed.subscribe_bulk = AsyncMock()
        scanner = MagicMock(spec=ArbitrageScanner)
        return GameManager(
            rest=mock_rest,
            feed=feed,
            scanner=scanner,
            sports_enabled=False,
            nonsports_categories=["Crypto", "Politics"],
            nonsports_max_days=7,
        )

    async def test_filters_by_category(
        self, manager: GameManager, mock_rest: KalshiRESTClient
    ) -> None:
        close = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        mock_rest.get_all_events.return_value = [  # type: ignore[union-attr]
            _make_nonsports_event("E1", "KXBTC", "Crypto", close),
            _make_nonsports_event("E2", "KXWX", "Climate and Weather", close),
        ]
        events = await manager.scan_events()
        tickers = [e.event_ticker for e in events]
        assert "E1" in tickers
        assert "E2" not in tickers  # Climate not in enabled categories

    async def test_filters_by_time_window(
        self, manager: GameManager, mock_rest: KalshiRESTClient
    ) -> None:
        within = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        beyond = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
        mock_rest.get_all_events.return_value = [  # type: ignore[union-attr]
            _make_nonsports_event("E1", "KXBTC", "Crypto", within),
            _make_nonsports_event("E2", "KXBTC2", "Crypto", beyond),
        ]
        events = await manager.scan_events()
        tickers = [e.event_ticker for e in events]
        assert "E1" in tickers
        assert "E2" not in tickers  # Beyond 7-day window

    async def test_excludes_sports_series(
        self, manager: GameManager, mock_rest: KalshiRESTClient
    ) -> None:
        close = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        mock_rest.get_all_events.return_value = [  # type: ignore[union-attr]
            _make_nonsports_event("E1", "KXNHLGAME", "Crypto", close),
        ]
        events = await manager.scan_events()
        assert len(events) == 0  # KXNHLGAME is in _SPORTS_SET

    async def test_excludes_no_active_markets(
        self, manager: GameManager, mock_rest: KalshiRESTClient
    ) -> None:
        close = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        mock_rest.get_all_events.return_value = [  # type: ignore[union-attr]
            _make_nonsports_event("E1", "KXBTC", "Crypto", close, status="closed"),
        ]
        events = await manager.scan_events()
        assert len(events) == 0

    async def test_excludes_null_close_time(
        self, manager: GameManager, mock_rest: KalshiRESTClient
    ) -> None:
        mock_rest.get_all_events.return_value = [  # type: ignore[union-attr]
            _make_nonsports_event("E1", "KXBTC", "Crypto", None),
        ]
        events = await manager.scan_events()
        assert len(events) == 0

    async def test_excludes_already_monitored(
        self, manager: GameManager, mock_rest: KalshiRESTClient
    ) -> None:
        close = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        ev = _make_nonsports_event("E1", "KXBTC", "Crypto", close)
        mock_rest.get_all_events.return_value = [ev]  # type: ignore[union-attr]
        mock_rest.get_event.return_value = ev  # type: ignore[union-attr]
        await manager.add_game("E1")
        events = await manager.scan_events()
        assert len(events) == 0

    async def test_empty_categories_disables_scan(
        self, mock_rest: KalshiRESTClient
    ) -> None:
        feed = MagicMock(spec=MarketFeed)
        scanner = MagicMock(spec=ArbitrageScanner)
        mgr = GameManager(
            rest=mock_rest, feed=feed, scanner=scanner,
            sports_enabled=False, nonsports_categories=[], nonsports_max_days=7,
        )
        events = await mgr.scan_events()
        assert events == []
        mock_rest.get_all_events.assert_not_called()  # type: ignore[union-attr]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_game_manager.py::TestNonSportsScan -v`
Expected: FAIL — `GameManager.__init__()` doesn't accept `nonsports_categories` yet.

- [ ] **Step 3: Define default categories constant**

Add to `src/talos/game_manager.py` after `SCAN_SERIES` (~line 105):

```python
DEFAULT_NONSPORTS_CATEGORIES: list[str] = [
    "Climate and Weather",
    "Crypto",
    "Companies",
    "Politics",
    "Science and Technology",
    "Mentions",
    "Entertainment",
    "World",
]
```

- [ ] **Step 4: Update `GameManager.__init__()` to accept new params**

Modify the constructor (~line 156):

```python
def __init__(
    self,
    rest: KalshiRESTClient,
    feed: MarketFeed,
    scanner: ArbitrageScanner,
    *,
    sports_enabled: bool = True,
    nonsports_categories: list[str] | None = None,
    nonsports_max_days: int = 7,
) -> None:
    self._rest = rest
    self._feed = feed
    self._scanner = scanner
    self._sports_enabled = sports_enabled
    self._nonsports_categories: set[str] = set(
        nonsports_categories if nonsports_categories is not None
        else DEFAULT_NONSPORTS_CATEGORIES
    )
    self._nonsports_max_days = nonsports_max_days
    self._games: dict[str, ArbPair] = {}
    self._labels: dict[str, str] = {}
    self._subtitles: dict[str, str] = {}
    self._leg_labels: dict[str, tuple[str, str]] = {}
    self._volumes_24h: dict[str, int] = {}
    self.on_change: Callable[[], None] | None = None
```

- [ ] **Step 5: Add time-window helper**

Add a module-level helper function before the `GameManager` class:

```python
from datetime import datetime, timedelta, timezone


def _has_market_closing_within(event: Event, max_days: int) -> bool:
    """Check if any active market on the event closes within max_days from now."""
    cutoff = datetime.now(timezone.utc) + timedelta(days=max_days)
    for m in event.markets:
        if m.status != "active" or not m.close_time:
            continue
        try:
            close_dt = datetime.fromisoformat(m.close_time.replace("Z", "+00:00"))
            if close_dt <= cutoff:
                return True
        except (ValueError, TypeError):
            continue
    return False
```

- [ ] **Step 6: Update `scan_events()` with non-sports path**

Replace the existing `scan_events()` method (~line 532-571):

```python
async def scan_events(self) -> list[Event]:
    """Discover all open arb-eligible events not already monitored."""
    active_tickers = {p.event_ticker for p in self.active_games}
    # Also exclude by kalshi_event_ticker (non-sports pairs key on market ticker)
    active_kalshi_tickers = {p.kalshi_event_ticker for p in self.active_games if p.kalshi_event_ticker}
    all_active = active_tickers | active_kalshi_tickers

    sem = asyncio.Semaphore(4)

    # --- Sports path (unchanged) ---
    sports_events: list[Event] = []
    if self._sports_enabled:
        async def fetch_series(series: str) -> list[Event]:
            async with sem:
                try:
                    return await self._rest.get_events(
                        series_ticker=series,
                        status="open",
                        with_nested_markets=True,
                        limit=200,
                    )
                except Exception:
                    logger.warning("scan_series_failed", series=series, exc_info=True)
                    return []

        all_results = await asyncio.gather(*(fetch_series(s) for s in SPORTS_SERIES))
        for batch in all_results:
            for event in batch:
                if event.event_ticker in all_active:
                    continue
                active_mkts = [m for m in event.markets if m.status == "active"]
                if len(active_mkts) != 2:
                    continue
                sports_events.append(event)

    # --- Non-sports path (new) ---
    nonsports_events: list[Event] = []
    if self._nonsports_categories:
        min_close_ts = int(datetime.now(timezone.utc).timestamp())
        try:
            raw_events = await self._rest.get_all_events(
                status="open",
                with_nested_markets=True,
                min_close_ts=min_close_ts,
            )
        except Exception:
            logger.warning("nonsports_scan_failed", exc_info=True)
            raw_events = []

        for event in raw_events:
            if event.event_ticker in all_active:
                continue
            if event.series_ticker in _SPORTS_SET:
                continue
            if event.category not in self._nonsports_categories:
                continue
            active_mkts = [m for m in event.markets if m.status == "active"]
            if len(active_mkts) == 0:
                continue
            if not _has_market_closing_within(event, self._nonsports_max_days):
                continue
            nonsports_events.append(event)

    return sports_events + nonsports_events
```

- [ ] **Step 7: Remove `NON_SPORTS_SERIES` and update `scan_events` references**

Delete from `game_manager.py`:
- Line 100: `NON_SPORTS_SERIES: list[str] = []`
- The old `series_list.extend(NON_SPORTS_SERIES)` line is already replaced by the new scan path above.

Keep `SCAN_SERIES = SPORTS_SERIES` alias (still used by data collector logging).

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_game_manager.py -v`
Expected: All tests PASS (old + new).

- [ ] **Step 9: Commit**

```bash
git add src/talos/game_manager.py tests/test_game_manager.py
git commit -m "feat: add non-sports scan path with category and time-window filtering"
```

---

### Task 5: Wire settings into `__main__.py`

**Files:**
- Modify: `src/talos/__main__.py:79-87`
- Modify: `src/talos/game_manager.py` (import `DEFAULT_NONSPORTS_CATEGORIES`)

- [ ] **Step 1: Update GameManager construction in `__main__.py`**

Modify the GameManager instantiation (~line 87):

```python
from talos.game_manager import GameManager, DEFAULT_NONSPORTS_CATEGORIES

# ... existing settings loading (line 79-80) ...
nonsports_categories = settings.get("nonsports_categories", DEFAULT_NONSPORTS_CATEGORIES)
nonsports_max_days = int(settings.get("nonsports_max_days", 7))  # type: ignore[arg-type]
game_mgr = GameManager(
    rest, feed, scanner,
    sports_enabled=auto_config.sports_enabled,
    nonsports_categories=nonsports_categories,  # type: ignore[arg-type]
    nonsports_max_days=nonsports_max_days,
)
```

- [ ] **Step 2: Run full test suite to verify nothing breaks**

Run: `.venv/Scripts/python -m pytest -x`
Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add src/talos/__main__.py
git commit -m "feat: wire nonsports_categories and nonsports_max_days settings into GameManager"
```

---

### Task 6: Adapt ScanScreen for non-sports events

**Files:**
- Modify: `src/talos/ui/screens.py:269-387`
- Modify: `src/talos/ui/app.py:656-693` (data collector logging)

- [ ] **Step 1: Update ScanScreen `on_mount()` to handle non-sports rows**

In `screens.py`, modify the `on_mount()` loop (~line 340-369). The key changes are:
1. For non-sports events (series not in `_SPORT_LEAGUE`), use `_CATEGORY_SHORT` for the Spt column and series prefix for the Lg column.
2. For date/time, fall back to parsing `close_time` from the first active market when no `GameStatus` and no ticker date match.

```python
def on_mount(self) -> None:
    table = self.query_one("#scan-table", DataTable)
    table.cursor_type = "row"
    table.zebra_stripes = True

    r = "right"
    table.add_column("✓", width=2)
    table.add_column("Spt", width=4)
    table.add_column("Lg", width=5)
    table.add_column(RichText("Date", justify=r), width=6)
    table.add_column(RichText("Time", justify=r), width=8)
    table.add_column("Event")
    table.add_column(RichText("24h A", justify=r), width=7)
    table.add_column(RichText("24h B", justify=r), width=7)

    rows: list[tuple[float, str, tuple[str, ...]]] = []
    for ev in self._events:
        ticker = ev.event_ticker
        prefix = ev.series_ticker or ticker.split("-")[0]
        sport_league = _SPORT_LEAGUE.get(prefix)

        if sport_league:
            sport, league = sport_league
        else:
            # Non-sports: use category short label + series prefix
            sport = _CATEGORY_SHORT.get(ev.category, ev.category[:4])
            league = prefix.removeprefix("KX")[:5]

        # Date and time from game status (sports)
        gs = self._statuses.get(ticker)
        sort_ts = 0.0
        date_str = "—"
        time_str = "—"
        if gs is not None and gs.scheduled_start is not None:
            pt = gs.scheduled_start.astimezone(_PT)
            date_str = pt.strftime("%m/%d")
            time_str = pt.strftime("%I:%M %p").lstrip("0")
            sort_ts = gs.scheduled_start.timestamp()
        else:
            raw_date = _extract_date_from_ticker(ticker)
            if raw_date is not None:
                date_str = f"{raw_date[4:6]}/{raw_date[6:8]}"
            else:
                # Non-sports fallback: use earliest market close_time
                close_times = [
                    m.close_time for m in ev.markets
                    if m.status == "active" and m.close_time
                ]
                if close_times:
                    earliest = min(close_times)
                    try:
                        ct = datetime.fromisoformat(earliest.replace("Z", "+00:00"))
                        pt = ct.astimezone(_PT)
                        date_str = pt.strftime("%m/%d")
                        time_str = pt.strftime("%I:%M %p").lstrip("0")
                        sort_ts = ct.timestamp()
                    except (ValueError, TypeError):
                        pass

        # Event label
        label = ev.sub_title or ev.title
        if "(" in label:
            label = label[: label.rfind("(")].strip()

        # Volume
        active_mkts = [m for m in ev.markets if m.status == "active"]
        vol_a = _fmt_vol_compact(active_mkts[0].volume_24h or 0) if active_mkts else "—"
        vol_b = _fmt_vol_compact(active_mkts[1].volume_24h or 0) if len(active_mkts) > 1 else "—"

        rows.append((sort_ts, ticker, (sport, league, date_str, time_str, label, vol_a, vol_b)))

    rows.sort(key=lambda r: r[0])

    self._row_tickers = []
    for _, ticker, (sport, league, date_str, time_str, label, vol_a, vol_b) in rows:
        self._row_tickers.append(ticker)
        table.add_row(
            "",
            sport,
            league,
            RichText(date_str, justify="right"),
            RichText(time_str, justify="right"),
            label,
            RichText(vol_a, justify="right"),
            RichText(vol_b, justify="right"),
            key=ticker,
        )
```

Note: The volume section now uses `active_mkts` instead of `ev.markets` directly — this is more robust for events with finalized markets mixed in.

- [ ] **Step 2: Update data collector logging in `app.py`**

In `src/talos/ui/app.py` (~line 656-693), the `series_scanned` metric currently uses `len(SCAN_SERIES)`. Update to reflect non-sports:

Change the import line (~line 659):
```python
from talos.game_manager import SCAN_SERIES, DEFAULT_NONSPORTS_CATEGORIES
```

Change the `series_scanned` line (~line 690):
```python
series_scanned=len(SCAN_SERIES) + (1 if len(DEFAULT_NONSPORTS_CATEGORIES) > 0 else 0),
```

Also update the sport/league derivation for scan events to handle non-sports (~line 663-666):

```python
from talos.ui.widgets import _SPORT_LEAGUE, _CATEGORY_SHORT

sport_league = _SPORT_LEAGUE.get(prefix)
if sport_league:
    sport, league = sport_league
else:
    sport = _CATEGORY_SHORT.get(ev.category, ev.category[:4])
    league = prefix.removeprefix("KX")[:5]
```

- [ ] **Step 3: Run full test suite**

Run: `.venv/Scripts/python -m pytest -x`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/talos/ui/screens.py src/talos/ui/app.py
git commit -m "feat: adapt ScanScreen and data collector for mixed sports/non-sports results"
```

---

### Task 7: Integration test and cleanup

**Files:**
- Modify: `tests/test_game_manager.py` (import cleanup)
- Verify: all files touched

- [ ] **Step 1: Update any broken imports**

The removal of `NON_SPORTS_SERIES` may break imports. Check:

```bash
.venv/Scripts/python -m ruff check src/ tests/ --select F401
```

Fix any unused/missing import references to `NON_SPORTS_SERIES`.

- [ ] **Step 2: Run full test suite**

Run: `.venv/Scripts/python -m pytest -v`
Expected: All tests PASS.

- [ ] **Step 3: Run lint and type check**

```bash
.venv/Scripts/python -m ruff check src/ tests/
.venv/Scripts/python -m pyright
```

Expected: Clean (or only pre-existing issues).

- [ ] **Step 4: Commit any cleanup**

```bash
git add -A
git commit -m "chore: cleanup imports and lint after non-sports scanning feature"
```

---

## File Map Summary

| File | Action | Responsibility |
|------|--------|----------------|
| `src/talos/models/strategy.py` | Modify | Add `series_ticker` field to `ArbPair` |
| `src/talos/rest_client.py` | Modify | Add `get_all_events()` with pagination |
| `src/talos/game_manager.py` | Modify | Constructor params, non-sports scan path, `_has_market_closing_within()`, `refresh_volumes()` fix, remove `NON_SPORTS_SERIES` |
| `src/talos/__main__.py` | Modify | Thread settings, add `series_ticker` to persistence |
| `src/talos/ui/screens.py` | Modify | ScanScreen non-sports row handling (category labels, close_time date/time) |
| `src/talos/ui/app.py` | Modify | Data collector logging for non-sports |
| `src/talos/ui/widgets.py` | Modify | `_CATEGORY_SHORT` mapping |
| `tests/test_rest_client.py` | Modify | `TestGetAllEvents` tests |
| `tests/test_game_manager.py` | Modify | `TestNonSportsScan` tests |
