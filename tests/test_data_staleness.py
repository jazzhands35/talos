"""Tests for data staleness query on OrderBookManager."""

import time

from talos.models.ws import OrderBookSnapshot
from talos.orderbook import OrderBookManager


def test_most_recent_update_no_books():
    mgr = OrderBookManager()
    assert mgr.most_recent_update() == 0.0


def test_most_recent_update_after_snapshot():
    mgr = OrderBookManager()
    snap = OrderBookSnapshot(
        market_ticker="TEST-TICK",
        market_id="test-id",
        yes=[[50, 100]],
        no=[[50, 100]],
    )
    mgr.apply_snapshot("TEST-TICK", snap)
    ts = mgr.most_recent_update()
    assert ts > 0.0
    assert time.time() - ts < 2.0
