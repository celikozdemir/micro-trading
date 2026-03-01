from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.session import get_session
from backend.models.market_data import AggTrade as AggTradeModel
from backend.models.market_data import BookTick as BookTickModel

router = APIRouter()


@router.get("/stats")
async def get_stats(session: AsyncSession = Depends(get_session)):
    symbols = ["BTCUSDT", "ETHUSDT"]
    result: dict = {}

    for sym in symbols:
        at = await session.execute(
            select(
                func.count(AggTradeModel.id),
                func.min(AggTradeModel.timestamp_exchange),
                func.max(AggTradeModel.timestamp_exchange),
            ).where(AggTradeModel.symbol == sym)
        )
        at_row = at.one()

        bt = await session.execute(
            select(func.count(BookTickModel.id)).where(BookTickModel.symbol == sym)
        )
        bt_count = bt.scalar()

        result[sym] = {
            "agg_trades": at_row[0],
            "book_ticks": bt_count,
            "earliest": at_row[1].isoformat() if at_row[1] else None,
            "latest": at_row[2].isoformat() if at_row[2] else None,
        }

    return result
