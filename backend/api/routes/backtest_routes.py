from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import load_trading_config
from backend.core.backtester.fill_model import FillModel
from backend.core.backtester.tick_replayer import TickReplayer
from backend.core.strategy.microstructure.advanced_momentum import BacktestTrade, AdvancedMomentumStrategy
from backend.db.session import get_session

router = APIRouter()

# Max ticks to load per backtest request — keeps memory safe on small servers
MAX_TICKS = 50_000
# Default lookback if no date range given
DEFAULT_HOURS = 1


class BacktestRequest(BaseModel):
    symbol: str = "BTCUSDT"
    start: str | None = None
    end: str | None = None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _build_result(trades: list[BacktestTrade], config: dict, tick_count: int, capped: bool) -> dict:
    base: dict = {"tick_count": tick_count}
    if capped:
        base["message"] = f"Loaded last {DEFAULT_HOURS}h of data ({tick_count:,} ticks). Use start/end to specify a range."

    if not trades:
        return {**base, "total_trades": 0, "message": base.get("message", "No trades generated — adjust thresholds")}

    wins = [t for t in trades if t.net_pnl_usd > 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += float(t.net_pnl_usd)
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    return {
        **base,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(trades) - len(wins),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "avg_hold_ms": round(sum(t.hold_ms for t in trades) / len(trades)),
        "avg_gross_bps": round(float(sum(t.gross_pnl_bps for t in trades) / len(trades)), 2),
        "total_fees_usd": round(float(sum(t.fees_usd for t in trades)), 4),
        "net_pnl_usd": round(float(sum(t.net_pnl_usd for t in trades)), 4),
        "max_drawdown_usd": round(max_dd, 4),
        "exit_reasons": reasons,
        "config": config["strategy"],
        "trades": [
            {
                "side": t.side,
                "entry_price": float(t.entry_price),
                "exit_price": float(t.exit_price),
                "qty": float(t.qty),
                "hold_ms": t.hold_ms,
                "exit_reason": t.exit_reason,
                "net_pnl_usd": round(float(t.net_pnl_usd), 4),
                "gross_pnl_bps": round(float(t.gross_pnl_bps), 2),
            }
            for t in trades
        ],
    }


def _run_strategy(ticks: list, config: dict) -> list[BacktestTrade]:
    """CPU-bound — runs in a thread so it doesn't block the event loop."""
    strategy = AdvancedMomentumStrategy(config, FillModel())
    for event in ticks:
        strategy.on_event(event)
    return strategy.trades


@router.post("/backtest")
async def run_backtest(req: BacktestRequest, session: AsyncSession = Depends(get_session)):
    config = load_trading_config()

    start = _parse_dt(req.start)
    end = _parse_dt(req.end)
    capped = False

    # Default to last DEFAULT_HOURS if no range given — prevents loading millions of rows
    if start is None and end is None:
        start = datetime.now(timezone.utc) - timedelta(hours=DEFAULT_HOURS)
        capped = True

    # Load ticks from DB (async I/O) — limit pushed to SQL to avoid loading millions of rows
    replayer = TickReplayer(session)
    ticks: list = []
    async for event in replayer.replay(req.symbol, start, end, limit=MAX_TICKS):
        ticks.append(event)

    # Run CPU-bound strategy loop in a thread with a hard timeout
    try:
        trades = await asyncio.wait_for(
            asyncio.to_thread(_run_strategy, ticks, config),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        return {"total_trades": 0, "message": "Backtest timed out (>30s). Use a narrower date range."}

    return _build_result(trades, config, len(ticks), capped)
