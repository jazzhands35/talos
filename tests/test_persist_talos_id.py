"""Regression: assigned talos_id must round-trip through game_manager._games.

Before the fix, scanner.add_pair() returned None. game_manager constructed
its own ArbPair with talos_id=0 and stored it in self._games. The scanner
had the real assigned id (e.g., 1) but game_manager's parallel ArbPair kept
talos_id=0, so every save tick persisted zeros to games_full.json.

Fix: scanner.add_pair() returns the assigned int; game_manager re-stamps the
locally-constructed ArbPair via model_copy so self._games carries the real id.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from talos.game_manager import GameManager
from talos.market_feed import MarketFeed
from talos.orderbook import OrderBookManager
from talos.rest_client import KalshiRESTClient
from talos.scanner import ArbitrageScanner


def _make_real_scanner() -> ArbitrageScanner:
    """Construct a real ArbitrageScanner (not a mock) so id assignment is live."""
    books = OrderBookManager()
    return ArbitrageScanner(books)


class TestPersistTalosId:
    """Verify talos_id assigned by scanner is visible on the game_manager pair."""

    def test_restore_game_pair_has_nonzero_talos_id(self) -> None:
        """restore_game() path: the ArbPair stored in game_manager._games must
        carry the talos_id assigned by the real scanner — NOT 0."""
        scanner = _make_real_scanner()
        rest = MagicMock(spec=KalshiRESTClient)
        feed = MagicMock(spec=MarketFeed)
        gm = GameManager(rest=rest, feed=feed, scanner=scanner)

        event_ticker = "SOME-EVENT-TICKER"
        gm.restore_game(
            {
                "event_ticker": event_ticker,
                "ticker_a": "SOME-EVENT-TICKER-A",
                "ticker_b": "SOME-EVENT-TICKER-B",
            }
        )

        pair = gm._games[event_ticker]
        # Bug: before fix, pair.talos_id == 0 even though scanner assigned 1
        assert pair.talos_id != 0, (
            f"pair.talos_id is 0 — the persist-zero bug is present. "
            f"scanner has id={scanner.get_talos_id(event_ticker)}"
        )
        # The id must also agree with what the scanner tracks
        assert scanner.get_talos_id(event_ticker) == pair.talos_id

    def test_restore_game_with_explicit_talos_id_round_trips(self) -> None:
        """When the persisted record already has a talos_id (e.g., restart from
        games_full.json), the same id must appear on the game_manager pair."""
        scanner = _make_real_scanner()
        rest = MagicMock(spec=KalshiRESTClient)
        feed = MagicMock(spec=MarketFeed)
        gm = GameManager(rest=rest, feed=feed, scanner=scanner)

        event_ticker = "RESTORE-WITH-ID"
        gm.restore_game(
            {
                "event_ticker": event_ticker,
                "ticker_a": "RESTORE-WITH-ID-A",
                "ticker_b": "RESTORE-WITH-ID-B",
                "talos_id": 42,
            }
        )

        pair = gm._games[event_ticker]
        assert pair.talos_id == 42
        assert scanner.get_talos_id(event_ticker) == 42

    def test_second_restore_is_idempotent(self) -> None:
        """Calling restore_game twice for the same event_ticker returns the
        cached ArbPair and does not corrupt the id."""
        scanner = _make_real_scanner()
        rest = MagicMock(spec=KalshiRESTClient)
        feed = MagicMock(spec=MarketFeed)
        gm = GameManager(rest=rest, feed=feed, scanner=scanner)

        event_ticker = "IDEMPOTENT-EVT"
        data: dict[str, str | float] = {
            "event_ticker": event_ticker,
            "ticker_a": "IDEMPOTENT-EVT-A",
            "ticker_b": "IDEMPOTENT-EVT-B",
        }
        gm.restore_game(data)
        first_id = gm._games[event_ticker].talos_id

        gm.restore_game(data)  # second call — should hit early-return
        second_id = gm._games[event_ticker].talos_id

        assert first_id != 0
        assert first_id == second_id
