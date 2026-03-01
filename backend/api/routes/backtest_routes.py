from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import load_trading_config
from backend.core.backtester.fill_model import FillModel
from backend.core.backtester.tick_replayer import TickReplayer
from backend.core.strategy.microstructure.burst_momentum import BacktestTrade, BurstMomentumStrategy
from backend.db.session import get_session

router = APIRouter()


class BacktestRequest(BaseModel):
    symbol: str = "BTCUSDT"
    start: str | None = None  # ISO datetime string
    end: str | None = None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _build_result(trades: list[BacktestTrade], config: dict) -> dict:
    if not trades:
        return {"total_trades": 0, "message": "No trades generated — adjust thresholds"}

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


@router.post("/backtest")
async def run_backtest(req: BacktestRequest, session: AsyncSession = Depends(get_session)):
    config = load_trading_config()
    strategy = BurstMomentumStrategy(config, FillModel())
    replayer = TickReplayer(session)

    async for event in replayer.replay(req.symbol, _parse_dt(req.start), _parse_dt(req.end)):
        strategy.on_event(event)

    return _build_result(strategy.trades, config)
