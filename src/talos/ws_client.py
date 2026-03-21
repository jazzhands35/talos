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
    FillMessage,
    MarketLifecycleMessage,
    MarketPositionMessage,
    OrderBookDelta,
    OrderBookSnapshot,
    TickerMessage,
    TradeMessage,
    UserOrderMessage,
    WSError,
    WSSubscribed,
)

logger = structlog.get_logger()

# Maps WS message type strings to their Pydantic model.
# Keys are message TYPE strings (from the "type" field), NOT channel names.
# e.g. channel "user_orders" sends type "user_order" (plural vs singular).
_MESSAGE_MODELS: dict[str, type] = {
    "orderbook_snapshot": OrderBookSnapshot,
    "orderbook_delta": OrderBookDelta,
    "ticker": TickerMessage,
    "trade": TradeMessage,
    "user_order": UserOrderMessage,
    "fill": FillMessage,
    "market_position": MarketPositionMessage,
    "market_lifecycle_v2": MarketLifecycleMessage,
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
        self._on_seq_gap: Callable[[int, str], Coroutine[Any, Any, None]] | None = None

    def _next_message_id(self) -> int:
        msg_id = self._next_id
        self._next_id += 1
        return msg_id

    def _build_subscribe(
        self,
        channel: str,
        market_ticker: str | None = None,
        market_tickers: list[str] | None = None,
        **extra_params: Any,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"channels": [channel]}
        if market_tickers is not None:
            params["market_tickers"] = market_tickers
        elif market_ticker is not None:
            params["market_ticker"] = market_ticker
        params.update(extra_params)
        return {
            "id": self._next_message_id(),
            "cmd": "subscribe",
            "params": params,
        }

    def _build_unsubscribe(self, sids: list[int]) -> dict[str, Any]:
        return {
            "id": self._next_message_id(),
            "cmd": "unsubscribe",
            "params": {"sids": sids},
        }

    def _build_update_subscription(
        self, sid: int, market_tickers: list[str], action: str
    ) -> dict[str, Any]:
        """Build an update_subscription command.

        Args:
            sid: Subscription ID to update.
            market_tickers: Tickers to add or remove.
            action: "add_markets" or "delete_markets".
        """
        return {
            "id": self._next_message_id(),
            "cmd": "update_subscription",
            "params": {
                "sids": [sid],
                "market_tickers": market_tickers,
                "action": action,
            },
        }

    def _build_list_subscriptions(self) -> dict[str, Any]:
        return {
            "id": self._next_message_id(),
            "cmd": "list_subscriptions",
            "params": {},
        }

    def on_message(self, channel: str, callback: Callable[..., Coroutine[Any, Any, None]]) -> None:
        """Register a callback for messages on a specific channel."""
        self._callbacks[channel] = callback

    def on_seq_gap(self, callback: Callable[[int, str], Coroutine[Any, Any, None]]) -> None:
        """Register a callback for sequence gap events.

        Callback receives (sid, channel) so the caller can trigger recovery
        (e.g., resubscribe to refresh state for the affected channel).
        """
        self._on_seq_gap = callback

    async def connect(self) -> None:
        """Open the WebSocket connection with auth headers."""
        # Clear stale subscription state from any prior connection
        self._sid_to_channel.clear()
        self._sid_to_seq.clear()
        headers = self._auth.headers("GET", "/trade-api/ws/v2")
        self._ws = await websockets.connect(
            self._ws_url,
            additional_headers=headers,
            open_timeout=10,     # fail fast if server unreachable
            ping_interval=None,  # disable client pings — Kalshi sends server pings every 10s
            ping_timeout=None,   # no client-side timeout
        )
        logger.info("ws_connected", url=self._ws_url)

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        if self._ws:
            await self._ws.close()
            self._ws = None
            logger.info("ws_disconnected")

    async def subscribe(
        self,
        channel: str,
        market_ticker: str | None = None,
        market_tickers: list[str] | None = None,
        **extra_params: Any,
    ) -> None:
        """Subscribe to a channel, optionally for a specific market.

        Args:
            channel: WS channel name (e.g. "orderbook_delta", "user_orders").
            market_ticker: Specific market. None for global subscription.
            market_tickers: Multiple markets in one command (bulk subscribe).
            **extra_params: Additional subscribe params (e.g. skip_ticker_ack).
        """
        if not self._ws:
            msg = "WebSocket not connected"
            raise RuntimeError(msg)
        message = self._build_subscribe(
            channel, market_ticker, market_tickers=market_tickers, **extra_params
        )
        await self._ws.send(json.dumps(message))
        logger.debug(
            "ws_subscribe_sent",
            channel=channel,
            market_ticker=market_ticker,
            market_tickers=market_tickers,
        )

    async def unsubscribe(self, sids: list[int]) -> None:
        """Unsubscribe from subscriptions by sid."""
        if not self._ws:
            msg = "WebSocket not connected"
            raise RuntimeError(msg)
        message = self._build_unsubscribe(sids)
        await self._ws.send(json.dumps(message))
        logger.debug("ws_unsubscribe_sent", sids=sids)

    async def update_subscription(
        self, sid: int, market_tickers: list[str], action: str = "add_markets"
    ) -> None:
        """Add or remove markets from an existing subscription.

        Args:
            sid: Subscription ID to update.
            market_tickers: Tickers to add or remove.
            action: "add_markets" or "delete_markets".
        """
        if not self._ws:
            msg = "WebSocket not connected"
            raise RuntimeError(msg)
        message = self._build_update_subscription(sid, market_tickers, action)
        await self._ws.send(json.dumps(message))
        logger.debug(
            "ws_update_subscription_sent",
            sid=sid,
            action=action,
            tickers=market_tickers,
        )

    async def list_subscriptions(self) -> None:
        """Send list_subscriptions command for debugging.

        Response arrives as a regular message through the listen loop.
        """
        if not self._ws:
            msg = "WebSocket not connected"
            raise RuntimeError(msg)
        message = self._build_list_subscriptions()
        await self._ws.send(json.dumps(message))
        logger.debug("ws_list_subscriptions_sent")

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
                if self._on_seq_gap is not None:
                    await self._on_seq_gap(sid, channel)
                return  # Don't dispatch the stale message; resubscribe will send a fresh snapshot
            self._sid_to_seq[sid] = seq

        # Parse message into model and dispatch to callback
        msg_data = raw.get("msg", {})
        model_cls = _MESSAGE_MODELS.get(msg_type)
        if model_cls is None:
            logger.debug("ws_unknown_type", msg_type=msg_type, sid=sid)
            return
        try:
            parsed = model_cls.model_validate(msg_data)
        except Exception:
            logger.warning(
                "ws_message_parse_error",
                msg_type=msg_type,
                channel=channel,
                sid=sid,
                exc_info=True,
            )
            return

        # Dispatch to callback
        callback = self._callbacks.get(channel)
        if callback:
            try:
                await callback(parsed, sid=sid, seq=seq or 0)
            except Exception:
                logger.warning(
                    "ws_callback_error",
                    msg_type=msg_type,
                    channel=channel,
                    sid=sid,
                    exc_info=True,
                )

    async def listen(self) -> None:
        """Listen for messages and dispatch them. Blocks until disconnect."""
        if not self._ws:
            msg = "WebSocket not connected"
            raise RuntimeError(msg)

        try:
            async for raw_msg in self._ws:
                data = json.loads(raw_msg)
                logger.debug("ws_message", type=data.get("type"), sid=data.get("sid"))
                await self._dispatch(data)
        except websockets.ConnectionClosed as e:
            logger.error("ws_connection_closed", code=e.code, reason=e.reason)
            raise
        except Exception as e:
            logger.error("ws_listen_error", error=str(e), error_type=type(e).__name__)
            raise
        finally:
            logger.error("ws_listen_loop_exited")
            self._ws = None
            # Clear subscription state — sids from dead connection are invalid
            self._sid_to_channel.clear()
            self._sid_to_seq.clear()
