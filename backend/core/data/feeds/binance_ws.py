"""
Binance WebSocket market data feed.

Subscribes to combined streams (bookTicker + aggTrade + markPrice) for
multiple symbols. Handles reconnection with exponential backoff and
reconnect-storm circuit breaker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections import deque
from typing import Callable

import certifi
import websockets
from websockets.exceptions import ConnectionClosed

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

from backend.core.data.normalizer import AggTrade, BookTick, MarkPrice, Normalizer

logger = logging.getLogger(__name__)

_FUTURES_WS = "wss://fstream.binance.com/stream"
_SPOT_WS = "wss://stream.binance.com:9443/stream"


def _build_url(venue: str, symbols: list[str], streams: list[str]) -> str:
    base = _FUTURES_WS if "usdm_futures" in venue else _SPOT_WS
    stream_list: list[str] = []
    for sym in symbols:
        s = sym.lower()
        for stream in streams:
            if stream == "bookTicker":
                stream_list.append(f"{s}@bookTicker")
            elif stream == "aggTrade":
                stream_list.append(f"{s}@aggTrade")
            elif stream == "markPrice_1s":
                stream_list.append(f"{s}@markPrice@1s")
    return f"{base}?streams={'/'.join(stream_list)}"


class ReconnectStormGuard:
    """
    Circuit breaker that triggers if too many reconnects happen
    within a sliding time window.
    """

    def __init__(self, max_reconnects: int = 5, window_minutes: int = 10):
        self.max_reconnects = max_reconnects
        self.window_s = window_minutes * 60
        self._history: deque[float] = deque()

    def record(self) -> bool:
        """Record a reconnect attempt. Returns True if storm detected."""
        now = time.time()
        self._history.append(now)
        # Prune events outside the sliding window
        while self._history and self._history[0] < now - self.window_s:
            self._history.popleft()
        return len(self._history) >= self.max_reconnects


class BinanceWebSocketFeed:
    """
    Async WebSocket client for Binance market data.

    Emits BookTick and AggTrade events to the registered callback.
    All parsing happens synchronously in the hot path — no I/O.
    DB writes are the caller's responsibility (batched, out-of-band).
    """

    def __init__(
        self,
        venue: str,
        symbols: list[str],
        streams: list[str],
        on_event: Callable[[BookTick | AggTrade | MarkPrice], None],
        max_reconnects: int = 5,
        reconnect_window_min: int = 10,
    ):
        self.url = _build_url(venue, symbols, streams)
        self.on_event = on_event
        self._normalizer = Normalizer()
        self._storm_guard = ReconnectStormGuard(max_reconnects, reconnect_window_min)
        self._running = False
        self._ws = None  # live ws handle — used by stop() for immediate close

    async def run(self) -> None:
        self._running = True
        backoff = 1.0

        while self._running:
            try:
                logger.info("Connecting to Binance WS: %s", self.url)
                async with websockets.connect(
                    self.url,
                    ssl=_SSL_CONTEXT,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=2**20,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0  # reset on successful connect
                    logger.info("Binance WS connected")
                    try:
                        async for raw in ws:
                            if not self._running:
                                break
                            self._handle_raw(raw)
                    finally:
                        self._ws = None

            except ConnectionClosed as e:
                logger.warning("WS connection closed: %s", e)
            except Exception as e:
                logger.error("WS error: %s", e)

            if not self._running:
                break

            if self._storm_guard.record():
                logger.critical(
                    "Reconnect storm detected — halting feed. Manual restart required."
                )
                self._running = False
                break

            logger.info("Reconnecting in %.1fs...", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _handle_raw(self, raw: str) -> None:
        """Hot path: parse and dispatch. No I/O allowed."""
        try:
            msg = json.loads(raw)
            # Combined stream wraps payload: {"stream": "...", "data": {...}}
            stream = msg.get("stream", "")
            data = msg.get("data", msg)
            event = self._normalizer.normalize(stream, data)
            if event is not None:
                self.on_event(event)
        except Exception as e:
            logger.debug("Failed to parse WS message: %s", e)

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()
