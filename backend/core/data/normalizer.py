"""
Normalizes raw Binance WebSocket payloads into typed internal structs.
No I/O — pure transformation, safe to call in the hot path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


@dataclass(slots=True)
class BookTick:
    symbol: str
    timestamp_exchange_ms: int
    timestamp_local_ms: int
    bid_price: Decimal
    bid_qty: Decimal
    ask_price: Decimal
    ask_qty: Decimal

    @property
    def mid_price(self) -> Decimal:
        return (self.bid_price + self.ask_price) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask_price - self.bid_price

    @property
    def spread_bps(self) -> Decimal:
        return (self.spread / self.mid_price) * 10000

    @property
    def lag_ms(self) -> int:
        return self.timestamp_local_ms - self.timestamp_exchange_ms


@dataclass(slots=True)
class AggTrade:
    symbol: str
    trade_id: int
    timestamp_exchange_ms: int
    timestamp_local_ms: int
    price: Decimal
    qty: Decimal
    is_buyer_maker: bool  # True = sell aggressor, False = buy aggressor

    @property
    def side(self) -> str:
        return "SELL" if self.is_buyer_maker else "BUY"

    @property
    def lag_ms(self) -> int:
        return self.timestamp_local_ms - self.timestamp_exchange_ms


class Normalizer:
    """Converts raw Binance WS payloads into typed BookTick / AggTrade structs."""

    def normalize(self, stream: str, data: dict) -> BookTick | AggTrade | None:
        local_ms = _epoch_ms()

        # bookTicker: identified by having b/a/B/A keys
        if "bookTicker" in stream or ("b" in data and "a" in data and "u" in data):
            return self._book_ticker(data, local_ms)

        event_type = data.get("e", "")

        if event_type == "aggTrade":
            return self._agg_trade(data, local_ms)

        # markPrice — not stored but can be added later
        return None

    def _book_ticker(self, d: dict, local_ms: int) -> BookTick:
        # bookTicker has no exchange-side timestamp in spot.
        # Futures bookTicker has a "T" (transaction time) field.
        exchange_ms = d.get("T") or d.get("E") or local_ms
        return BookTick(
            symbol=d["s"],
            timestamp_exchange_ms=exchange_ms,
            timestamp_local_ms=local_ms,
            bid_price=Decimal(d["b"]),
            bid_qty=Decimal(d["B"]),
            ask_price=Decimal(d["a"]),
            ask_qty=Decimal(d["A"]),
        )

    def _agg_trade(self, d: dict, local_ms: int) -> AggTrade:
        return AggTrade(
            symbol=d["s"],
            trade_id=d["a"],
            timestamp_exchange_ms=d["T"],
            timestamp_local_ms=local_ms,
            price=Decimal(d["p"]),
            qty=Decimal(d["q"]),
            is_buyer_maker=d["m"],
        )
