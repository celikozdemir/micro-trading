"""
Loads recorded book_ticks and agg_trades from the DB and replays them
in exchange-timestamp order for offline strategy simulation.
"""

from __future__ import annotations

import heapq
import itertools
from datetime import datetime
from typing import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.data.normalizer import AggTrade, BookTick
from backend.models.market_data import AggTrade as AggTradeModel
from backend.models.market_data import BookTick as BookTickModel


def _book_tick_from_row(row: BookTickModel) -> BookTick:
    return BookTick(
        symbol=row.symbol,
        timestamp_exchange_ms=int(row.timestamp_exchange.timestamp() * 1000),
        timestamp_local_ms=int(row.timestamp_local.timestamp() * 1000),
        bid_price=row.bid_price,
        bid_qty=row.bid_qty,
        ask_price=row.ask_price,
        ask_qty=row.ask_qty,
    )


def _agg_trade_from_row(row: AggTradeModel) -> AggTrade:
    return AggTrade(
        symbol=row.symbol,
        trade_id=row.trade_id,
        timestamp_exchange_ms=int(row.timestamp_exchange.timestamp() * 1000),
        timestamp_local_ms=int(row.timestamp_local.timestamp() * 1000),
        price=row.price,
        qty=row.qty,
        is_buyer_maker=row.is_buyer_maker,
    )


class TickReplayer:
    """
    Merge-sorts book_ticks and agg_trades from the DB and yields
    them in chronological order by exchange timestamp.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def replay(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        book_limit: int | None = None,
        trade_limit: int | None = None,
    ) -> AsyncIterator[BookTick | AggTrade]:
        # Per-stream limits take precedence; fallback to equal split of `limit`
        half = (limit // 2) if limit else None
        book_ticks = await self._load_book_ticks(symbol, start, end, book_limit if book_limit is not None else half)
        agg_trades = await self._load_agg_trades(symbol, start, end, trade_limit if trade_limit is not None else half)

        # Heap entries: (timestamp_ms, stream_priority, seq, event)
        # stream_priority: 0=book 1=trade — book tick goes first at same ms
        # seq: unique counter prevents comparison of event objects
        heap: list[tuple[int, int, int, BookTick | AggTrade]] = []
        counter = itertools.count()

        for bt in book_ticks:
            heapq.heappush(heap, (bt.timestamp_exchange_ms, 0, next(counter), bt))
        for at in agg_trades:
            heapq.heappush(heap, (at.timestamp_exchange_ms, 1, next(counter), at))

        while heap:
            _, _, _, event = heapq.heappop(heap)
            yield event

    async def _load_book_ticks(
        self, symbol: str, start: datetime | None, end: datetime | None, limit: int | None
    ) -> list[BookTick]:
        stmt = (
            select(BookTickModel)
            .where(BookTickModel.symbol == symbol)
            .order_by(BookTickModel.timestamp_exchange)
        )
        if start:
            stmt = stmt.where(BookTickModel.timestamp_exchange >= start)
        if end:
            stmt = stmt.where(BookTickModel.timestamp_exchange <= end)
        if limit:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return [_book_tick_from_row(r) for r in result.scalars()]

    async def _load_agg_trades(
        self, symbol: str, start: datetime | None, end: datetime | None, limit: int | None
    ) -> list[AggTrade]:
        stmt = (
            select(AggTradeModel)
            .where(AggTradeModel.symbol == symbol)
            .order_by(AggTradeModel.timestamp_exchange)
        )
        if start:
            stmt = stmt.where(AggTradeModel.timestamp_exchange >= start)
        if end:
            stmt = stmt.where(AggTradeModel.timestamp_exchange <= end)
        if limit:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return [_agg_trade_from_row(r) for r in result.scalars()]
