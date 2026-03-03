from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.session import get_session
from backend.models.paper_trade import PaperTrade

router = APIRouter()


@router.get("/paper-trades")
async def list_paper_trades(
    symbol: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    filters = []
    if symbol:
        filters.append(PaperTrade.symbol == symbol)

    count_q = select(func.count()).select_from(PaperTrade)
    trades_q = select(PaperTrade).order_by(PaperTrade.entry_time_ms.desc()).offset(offset).limit(limit)
    if filters:
        from sqlalchemy import and_
        condition = and_(*filters)
        count_q = count_q.where(condition)
        trades_q = trades_q.where(condition)

    total = (await session.execute(count_q)).scalar() or 0
    rows = (await session.execute(trades_q)).scalars().all()

    return {
        "trades": [
            {
                "id": t.id,
                "symbol": t.symbol,
                "side": t.side,
                "entry_time_ms": t.entry_time_ms,
                "exit_time_ms": t.exit_time_ms,
                "entry_price": float(t.entry_price),
                "exit_price": float(t.exit_price),
                "qty": float(t.qty),
                "exit_reason": t.exit_reason,
                "hold_ms": t.hold_ms,
                "gross_pnl_bps": float(t.gross_pnl_bps),
                "gross_pnl_usd": float(t.gross_pnl_usd),
                "fees_usd": float(t.fees_usd),
                "net_pnl_usd": float(t.net_pnl_usd),
            }
            for t in rows
        ],
        "total": total,
    }


@router.get("/paper-trades/stats")
async def paper_trade_stats(
    symbol: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    now = datetime.now(timezone.utc)
    today_midnight_ms = int(
        datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp() * 1000
    )

    async def _agg(extra=None) -> dict:
        from sqlalchemy import and_
        base_filters = []
        if symbol:
            base_filters.append(PaperTrade.symbol == symbol)
        if extra is not None:
            base_filters.append(extra)

        wins_col = func.sum(case((PaperTrade.net_pnl_usd > 0, 1), else_=0))
        q = select(
            func.count().label("total"),
            wins_col.label("wins"),
            func.sum(PaperTrade.net_pnl_usd).label("net_pnl"),
        )
        if base_filters:
            q = q.where(and_(*base_filters))

        row = (await session.execute(q)).fetchone()
        total = row.total or 0
        wins = int(row.wins or 0)
        net_pnl = float(row.net_pnl or 0)
        return {
            "total_trades": total,
            "wins": wins,
            "win_rate": round(wins / total * 100, 1) if total > 0 else 0.0,
            "net_pnl_usd": round(net_pnl, 4),
        }

    all_time = await _agg()
    today = await _agg(PaperTrade.entry_time_ms >= today_midnight_ms)

    return {"all_time": all_time, "today": today}


@router.delete("/paper-trades")
async def clear_paper_trades(session: AsyncSession = Depends(get_session)):
    result = await session.execute(delete(PaperTrade))
    await session.commit()
    return {"deleted": result.rowcount}
