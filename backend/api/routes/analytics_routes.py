"""
Analytics endpoints — equity curve, trade breakdown, hourly performance.

These power the dashboard charts and give actionable insight into strategy behavior.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, extract, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.session import get_session
from backend.models.paper_trade import PaperTrade

router = APIRouter()


@router.get("/analytics/equity-curve")
async def equity_curve(
    symbol: Optional[str] = Query(None),
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """Cumulative net P&L over time, ordered by exit time."""
    cutoff_ms = int(
        (datetime.now(timezone.utc).timestamp() - days * 86400) * 1000
    )

    filters = [PaperTrade.exit_time_ms >= cutoff_ms]
    if symbol:
        filters.append(PaperTrade.symbol == symbol)

    from sqlalchemy import and_

    q = (
        select(
            PaperTrade.exit_time_ms,
            PaperTrade.net_pnl_usd,
            PaperTrade.gross_pnl_bps,
            PaperTrade.symbol,
            PaperTrade.side,
            PaperTrade.exit_reason,
        )
        .where(and_(*filters))
        .order_by(PaperTrade.exit_time_ms.asc())
    )

    rows = (await session.execute(q)).all()

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    points = []

    for r in rows:
        cumulative += float(r.net_pnl_usd)
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

        points.append({
            "ts": r.exit_time_ms,
            "pnl": round(cumulative, 6),
            "trade_pnl": round(float(r.net_pnl_usd), 6),
            "bps": round(float(r.gross_pnl_bps), 2),
            "symbol": r.symbol,
            "side": r.side,
            "exit_reason": r.exit_reason,
            "drawdown": round(peak - cumulative, 6),
        })

    return {
        "points": points,
        "summary": {
            "total_trades": len(points),
            "net_pnl_usd": round(cumulative, 6),
            "max_drawdown_usd": round(max_dd, 6),
            "peak_pnl_usd": round(peak, 6),
        },
    }


@router.get("/analytics/breakdown")
async def trade_breakdown(
    symbol: Optional[str] = Query(None),
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """Trade breakdown by exit reason, side, and symbol."""
    cutoff_ms = int(
        (datetime.now(timezone.utc).timestamp() - days * 86400) * 1000
    )

    filters = [PaperTrade.exit_time_ms >= cutoff_ms]
    if symbol:
        filters.append(PaperTrade.symbol == symbol)

    from sqlalchemy import and_

    condition = and_(*filters)

    # By exit reason
    reason_q = (
        select(
            PaperTrade.exit_reason,
            func.count().label("count"),
            func.sum(PaperTrade.net_pnl_usd).label("net_pnl"),
            func.avg(PaperTrade.gross_pnl_bps).label("avg_bps"),
            func.avg(PaperTrade.hold_ms).label("avg_hold_ms"),
            func.sum(case((PaperTrade.net_pnl_usd > 0, 1), else_=0)).label("wins"),
        )
        .where(condition)
        .group_by(PaperTrade.exit_reason)
    )

    # By side
    side_q = (
        select(
            PaperTrade.side,
            func.count().label("count"),
            func.sum(PaperTrade.net_pnl_usd).label("net_pnl"),
            func.avg(PaperTrade.gross_pnl_bps).label("avg_bps"),
            func.sum(case((PaperTrade.net_pnl_usd > 0, 1), else_=0)).label("wins"),
        )
        .where(condition)
        .group_by(PaperTrade.side)
    )

    # By symbol
    symbol_q = (
        select(
            PaperTrade.symbol,
            func.count().label("count"),
            func.sum(PaperTrade.net_pnl_usd).label("net_pnl"),
            func.avg(PaperTrade.gross_pnl_bps).label("avg_bps"),
            func.sum(case((PaperTrade.net_pnl_usd > 0, 1), else_=0)).label("wins"),
        )
        .where(condition)
        .group_by(PaperTrade.symbol)
    )

    reason_rows = (await session.execute(reason_q)).all()
    side_rows = (await session.execute(side_q)).all()
    symbol_rows = (await session.execute(symbol_q)).all()

    def _fmt(rows):
        return [
            {
                "label": r[0],
                "count": r.count,
                "net_pnl": round(float(r.net_pnl or 0), 6),
                "avg_bps": round(float(r.avg_bps or 0), 2),
                "wins": int(r.wins or 0),
                "win_rate": round(int(r.wins or 0) / r.count * 100, 1) if r.count > 0 else 0,
            }
            for r in rows
        ]

    return {
        "by_exit_reason": _fmt(reason_rows),
        "by_side": _fmt(side_rows),
        "by_symbol": _fmt(symbol_rows),
    }


@router.get("/analytics/hourly")
async def hourly_performance(
    symbol: Optional[str] = Query(None),
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """Trade performance bucketed by hour of day (UTC)."""
    cutoff_ms = int(
        (datetime.now(timezone.utc).timestamp() - days * 86400) * 1000
    )

    filters = [PaperTrade.exit_time_ms >= cutoff_ms]
    if symbol:
        filters.append(PaperTrade.symbol == symbol)

    from sqlalchemy import and_

    q = (
        select(
            PaperTrade.entry_time_ms,
            PaperTrade.net_pnl_usd,
            PaperTrade.gross_pnl_bps,
        )
        .where(and_(*filters))
        .order_by(PaperTrade.entry_time_ms.asc())
    )

    rows = (await session.execute(q)).all()

    hours: dict[int, dict] = {h: {"count": 0, "net_pnl": 0.0, "wins": 0, "total_bps": 0.0} for h in range(24)}

    for r in rows:
        hour = (r.entry_time_ms // 1000 // 3600) % 24
        hours[hour]["count"] += 1
        hours[hour]["net_pnl"] += float(r.net_pnl_usd)
        hours[hour]["total_bps"] += float(r.gross_pnl_bps)
        if float(r.net_pnl_usd) > 0:
            hours[hour]["wins"] += 1

    return [
        {
            "hour": h,
            "count": d["count"],
            "net_pnl": round(d["net_pnl"], 6),
            "avg_bps": round(d["total_bps"] / d["count"], 2) if d["count"] > 0 else 0,
            "win_rate": round(d["wins"] / d["count"] * 100, 1) if d["count"] > 0 else 0,
        }
        for h, d in sorted(hours.items())
    ]
