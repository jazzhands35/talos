"""WebSocket client for Kalshi real-time data feeds."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Any

import structlog
import websockets

from talos.auth import KalshiAuth
from talos.config import KalshiConfig
from talos.models.ws import (
    OrderBookDelta,
    OrderBookSnapshot,
    TickerMessage,
    TradeMessage,
    WSError,
    WSSubscribed,
)

logger = structlog.get_logger()

# Maps WS message type strings to their Pydantic model
_MESSAGE_MODELS: dict[str, type] = {
    "orderbook_snapshot": OrderBookSnapshot,
    "orderbook_delta": OrderBookDelta,
    "ticker": TickerMessage,
    "trade": TradeMessage,
}


class KalshiWSClient:
    """WebSocket client for Kalshi real-time feeds.

    Manages connection, subscriptions, keepalive, and message dispatch.
    """

    def __init__(self, auth: KalshiAuth, config: KalshiConfig) -> None:
        self._auth = auth
        self._ws_url = config.ws_url
        self._ws: Any = None
        self._next_id = 1
        self._callbacks: dict[str, Callable[..., Coroutine[Any, Any, None]]] = {}
        self._sid_to_channel: dict[int, str] = {}
        self._sid_to_seq: dict[int, int] = {}

    def _next_message_id(self) -> int:
        msg_id = self._next_id
        self._next_id += 1
        return msg_id

    def _build_subscribe(self, channel: str, market_ticker: str) -> dict[str, Any]:
        return {
            "id": self._next_message_id(),
            "cmd": "subscribe",
            "params": {
                "channels": [channel],
                "market_ticker": market_ticker,
            },
        }

    def _build_unsubscribe(self, sids: list[int]) -> dict[str, Any]:
        return {
            "id": self._next_message_id(),
            "cmd": "unsubscribe",
            "params": {"sids": sids},
        }

    def on_message(self, channel: str, callback: Callable[..., Coroutine[Any, Any, None]]) -> None:
        """Register a callback for messages on a specific channel."""
        self._callbacks[channel] = callback

    async def connect(self) -> None:
        """Open the WebSocket connection with auth headers."""
        headers = self._auth.headers("GET", "/")
        self._ws = await websockets.connect(self._ws_url, additional_headers=headers)
        logger.info("ws_connected", url=self._ws_url)

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        if self._ws:
            await self._ws.close()
            self._ws = None
            logger.info("ws_disconnected")

    async def subscribe(self, channel: str, market_ticker: str) -> None:
        """Subscribe to a channel for a specific market."""
        if not self._ws:
            msg = "WebSocket not connected"
            raise RuntimeError(msg)
        message = self._build_subscribe(channel, market_ticker)
        await self._ws.send(json.dumps(message))
        logger.debug("ws_subscribe_sent", channel=channel, market_ticker=market_ticker)

    async def unsubscribe(self, sids: list[int]) -> None:
        """Unsubscribe from subscriptions by sid."""
        if not self._ws:
            msg = "WebSocket not connected"
            raise RuntimeError(msg)
        message = self._build_unsubscribe(sids)
        await self._ws.send(json.dumps(message))
        logger.debug("ws_unsubscribe_sent", sids=sids)

    async def _dispatch(self, raw: dict[str, Any]) -> None:
        """Parse and route a WebSocket message to the appropriate callback."""
        msg_type = raw.get("type", "")

        # Handle subscription confirmations
        if msg_type == "subscribed":
            sub = WSSubscribed.model_validate(raw.get("msg", {}))
            self._sid_to_channel[sub.sid] = sub.channel
            self._sid_to_seq[sub.sid] = 0
            logger.debug("ws_subscribed", channel=sub.channel, sid=sub.sid)
            return

        # Handle errors
        if msg_type == "error":
            err = WSError.model_validate(raw.get("msg", {}))
            logger.error("ws_error", code=err.code, msg=err.msg)
            return

        # Route data messages by sid
        sid = raw.get("sid")
        if sid is None or sid not in self._sid_to_channel:
            return

        channel = self._sid_to_channel[sid]

        # Check seq continuity
        seq = raw.get("seq")
        if seq is not None:
            expected = self._sid_to_seq.get(sid, 0) + 1
            if seq != expected and self._sid_to_seq.get(sid, 0) > 0:
                logger.warning(
                    "ws_seq_gap",
                    sid=sid,
                    channel=channel,
                    expected=expected,
                    got=seq,
                )
            self._sid_to_seq[sid] = seq

        # Parse message into model
        msg_data = raw.get("msg", {})
        model_cls = _MESSAGE_MODELS.get(msg_type)
        parsed = model_cls.model_validate(msg_data) if model_cls else msg_data

        # Dispatch to callback
        callback = self._callbacks.get(channel)
        if callback:
            await callback(parsed, sid=sid, seq=seq or 0)

    async def listen(self) -> None:
        """Listen for messages and dispatch them. Blocks until disconnect."""
        if not self._ws:
            msg = "WebSocket not connected"
            raise RuntimeError(msg)

        async for raw_msg in self._ws:
            data = json.loads(raw_msg)
            logger.debug("ws_message", type=data.get("type"), sid=data.get("sid"))
            await self._dispatch(data)
