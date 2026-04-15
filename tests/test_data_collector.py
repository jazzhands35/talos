"""Tests for the DataCollector write-only SQLite pipeline."""

from __future__ import annotations

from pathlib import Path

from talos.data_collector import DataCollector


def _make_collector(tmp_path: Path) -> DataCollector:
    return DataCollector(tmp_path / "test.db")


class TestSchema:
    def test_creates_all_tables(self, tmp_path: Path) -> None:
        dc = _make_collector(tmp_path)
        cur = dc._db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {row[0] for row in cur.fetchall()}
        expected = {
            "scan_results",
            "scan_events",
            "game_adds",
            "orders",
            "fills",
            "market_snapshots",
            "settlements",
            "event_outcomes",
        }
        assert expected.issubset(tables)
        dc.close()

    def test_wal_mode(self, tmp_path: Path) -> None:
        dc = _make_collector(tmp_path)
        mode = dc._db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        dc.close()

    def test_idempotent_init(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        dc1 = DataCollector(db_path)
        dc1.log_game_add(event_ticker="EVT-1", source="test")
        dc1.close()
        # Re-open — should not crash or lose data
        dc2 = DataCollector(db_path)
        count = dc2._db.execute("SELECT COUNT(*) FROM game_adds").fetchone()[0]
        assert count == 1
        dc2.close()


class TestLogScan:
    def test_log_scan_basic(self, tmp_path: Path) -> None:
        dc = _make_collector(tmp_path)
        dc.log_scan(
            events_found=50,
            events_eligible=30,
            events_selected=10,
            series_scanned=20,
            duration_ms=5000,
        )
        row = dc._db.execute("SELECT * FROM scan_results").fetchone()
        assert row is not None
        # id, ts, events_found, events_eligible, events_selected, series_scanned, duration_ms
        assert row[2] == 50  # events_found
        assert row[4] == 10  # events_selected
        dc.close()

    def test_log_scan_with_events(self, tmp_path: Path) -> None:
        dc = _make_collector(tmp_path)
        dc.log_scan(
            events_found=2,
            events_eligible=2,
            events_selected=1,
            series_scanned=1,
            duration_ms=100,
            events=[
                {
                    "event_ticker": "EVT-1",
                    "series_ticker": "SER-1",
                    "sport": "HOC",
                    "league": "NHL",
                    "title": "Game 1",
                    "sub_title": "BOS at WSH",
                    "volume_a": 1000,
                    "volume_b": 500,
                    "no_bid_a": 40,
                    "no_ask_a": 42,
                    "no_bid_b": 55,
                    "no_ask_b": 57,
                    "edge": 3.2,
                    "selected": 1,
                },
            ],
        )
        scan_count = dc._db.execute("SELECT COUNT(*) FROM scan_results").fetchone()[0]
        event_count = dc._db.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0]
        assert scan_count == 1
        assert event_count == 1
        dc.close()


class TestLogGameAdd:
    def test_log_game_add(self, tmp_path: Path) -> None:
        dc = _make_collector(tmp_path)
        dc.log_game_add(
            event_ticker="EVT-1",
            series_ticker="SER-1",
            sport="HOC",
            league="NHL",
            source="scan",
            ticker_a="TK-A",
            ticker_b="TK-B",
            volume_a=1000,
            volume_b=500,
            fee_type="quadratic_with_maker_fees",
            fee_rate=0.0175,
        )
        row = dc._db.execute("SELECT event_ticker, source FROM game_adds").fetchone()
        assert row == ("EVT-1", "scan")
        dc.close()


class TestLogOrder:
    def test_log_order(self, tmp_path: Path) -> None:
        dc = _make_collector(tmp_path)
        dc.log_order(
            event_ticker="EVT-1",
            order_id="ORD-1",
            ticker="TK-A",
            side="no",
            status="resting",
            price=45,
            initial_count=20,
            remaining_count=20,
            source="auto_accept",
        )
        row = dc._db.execute("SELECT order_id, price, source FROM orders").fetchone()
        assert row == ("ORD-1", 45, "auto_accept")
        dc.close()


class TestLogFill:
    def test_log_fill(self, tmp_path: Path) -> None:
        dc = _make_collector(tmp_path)
        dc.log_fill(
            event_ticker="EVT-1",
            trade_id="TRADE-1",
            order_id="ORD-1",
            ticker="TK-A",
            side="no",
            price=45,
            count=5,
            fee_cost=2,
            is_taker=False,
            post_position=5,
            queue_position=100,
            time_since_order=30.5,
        )
        row = dc._db.execute(
            "SELECT trade_id, count, is_taker, time_since_order FROM fills"
        ).fetchone()
        assert row == ("TRADE-1", 5, 0, 30.5)
        dc.close()


class TestLogMarketSnapshots:
    def test_bulk_insert(self, tmp_path: Path) -> None:
        dc = _make_collector(tmp_path)
        dc.log_market_snapshots(
            [
                {
                    "event_ticker": "EVT-1",
                    "ticker_a": "TK-A",
                    "ticker_b": "TK-B",
                    "no_a": 40,
                    "no_b": 55,
                    "edge": 3.2,
                    "volume_a": 1000,
                    "volume_b": 500,
                    "open_interest_a": 200,
                    "open_interest_b": 150,
                    "game_state": "pre",
                    "status": "Bidding",
                    "filled_a": 0,
                    "filled_b": 0,
                    "resting_a": 20,
                    "resting_b": 20,
                },
                {
                    "event_ticker": "EVT-2",
                    "ticker_a": "TK-C",
                    "ticker_b": "TK-D",
                    "no_a": 30,
                    "no_b": 65,
                    "edge": 1.5,
                    "volume_a": 2000,
                    "volume_b": 1000,
                    "open_interest_a": 300,
                    "open_interest_b": 250,
                    "game_state": "live",
                    "status": "Balanced",
                    "filled_a": 20,
                    "filled_b": 20,
                    "resting_a": 0,
                    "resting_b": 0,
                },
            ]
        )
        count = dc._db.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
        assert count == 2
        dc.close()


class TestLogSettlement:
    def test_log_settlement(self, tmp_path: Path) -> None:
        dc = _make_collector(tmp_path)
        dc.log_settlement(
            event_ticker="EVT-1",
            ticker="TK-A",
            event_type="determined",
            result="no",
            settlement_value=100,
            total_pnl=500,
        )
        row = dc._db.execute("SELECT event_type, result, total_pnl FROM settlements").fetchone()
        assert row == ("determined", "no", 500)
        dc.close()


class TestLogEventOutcome:
    def test_balanced_position(self, tmp_path: Path) -> None:
        dc = _make_collector(tmp_path)
        dc.log_event_outcome(
            event_ticker="EVT-1",
            filled_a=20,
            filled_b=20,
            total_pnl=500,
        )
        row = dc._db.execute("SELECT trapped, trap_side, trap_delta FROM event_outcomes").fetchone()
        assert row == (0, None, 0)
        dc.close()

    def test_trapped_position(self, tmp_path: Path) -> None:
        dc = _make_collector(tmp_path)
        dc.log_event_outcome(
            event_ticker="EVT-1",
            filled_a=20,
            filled_b=5,
            total_pnl=-300,
        )
        row = dc._db.execute("SELECT trapped, trap_side, trap_delta FROM event_outcomes").fetchone()
        assert row == (1, "A", 15)
        dc.close()

    def test_trap_side_b(self, tmp_path: Path) -> None:
        dc = _make_collector(tmp_path)
        dc.log_event_outcome(
            event_ticker="EVT-1",
            filled_a=5,
            filled_b=20,
            total_pnl=-300,
        )
        row = dc._db.execute("SELECT trapped, trap_side, trap_delta FROM event_outcomes").fetchone()
        assert row == (1, "B", 15)
        dc.close()

    def test_zero_fills_not_trapped(self, tmp_path: Path) -> None:
        dc = _make_collector(tmp_path)
        dc.log_event_outcome(
            event_ticker="EVT-1",
            filled_a=0,
            filled_b=0,
        )
        row = dc._db.execute("SELECT trapped, trap_delta FROM event_outcomes").fetchone()
        assert row == (0, 0)
        dc.close()


class TestInsertFailure:
    def test_bad_insert_does_not_crash(self, tmp_path: Path) -> None:
        dc = _make_collector(tmp_path)
        # Close the DB to force an error
        dc._db.close()
        # Should not raise — logs warning and returns 0
        dc.log_game_add(event_ticker="EVT-1", source="test")
