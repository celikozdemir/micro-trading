"""
Parameter grid search for Burst Momentum strategy.

Loads ticks from DB ONCE, then sweeps every parameter combination in memory.
Prints a ranked table sorted by net P&L.

Usage:
    python -m workers.grid_search --symbol BTCUSDT
    python -m workers.grid_search --symbol ETHUSDT --top 20
    python -m workers.grid_search --symbol BTCUSDT --min-trades 5
    python -m workers.grid_search --symbol BTCUSDT --start 2026-03-01T16:57:00 --end 2026-03-01T17:57:00
    python -m workers.grid_search --symbol BTCUSDT --max-ticks 50000
"""

from __future__ import annotations

import argparse
import itertools
import logging
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone

import uvloop

from backend.core.backtester.fill_model import FillModel
from backend.core.backtester.tick_replayer import TickReplayer
from backend.core.data.normalizer import AggTrade, BookTick
from backend.core.strategy.microstructure.burst_momentum import BurstMomentumStrategy
from backend.db.session import AsyncSessionLocal

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

# ── Fee constants ─────────────────────────────────────────────────────────────
# Round-trip cost floor: (fee_bps + slippage_bps) * 2 sides
# Default: (4.0 + 1.5) * 2 = 11.0 bps — TP must exceed this to be profitable
ROUND_TRIP_COST_BPS = 11.0


# ── Parameter grid ────────────────────────────────────────────────────────────
# Adjust these ranges based on your DiagnosticAnalyzer output.
# Rule of thumb:
#   trade_count_trigger → p75 to p99 of your window trade-count distribution
#   move_bps_trigger    → 1.0–5.0 (must see real price movement)
#   take_profit_bps     → MUST be > 11 bps to beat fees; 12–25 is the sweet spot
#   stop_loss_bps       → 5–15 (asymmetric stop keeps R:R reasonable)
#   max_hold_ms         → 200–2000 (shorter = cleaner, longer = more exits)

GRID = {
    "window_ms":            [250],           # keep fixed — it's the microstructure window
    "trade_count_trigger":  [5, 10, 20, 40, 80],
    "move_bps_trigger":     [1.0, 2.0, 3.5, 5.0],
    "take_profit_bps":      [12.0, 15.0, 20.0, 30.0],
    "stop_loss_bps":        [5.0, 8.0, 12.0],
    "max_hold_ms":          [300, 600, 1000, 2000],
    "cooldown_ms":          [500],           # keep fixed
}


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class GridResult:
    params: dict
    n_trades: int
    win_rate: float
    avg_gross_bps: float
    net_pnl_usd: float
    max_dd_usd: float
    avg_hold_ms: float
    pct_timeout: float


def _run_once(ticks: list[BookTick | AggTrade], params: dict, base_config: dict) -> GridResult:
    """Replay cached ticks with the given param override."""
    config = {
        **base_config,
        "strategy": {
            **base_config["strategy"],
            "window_ms":           params["window_ms"],
            "trade_count_trigger": params["trade_count_trigger"],
            "move_bps_trigger":    params["move_bps_trigger"],
            "cooldown_ms":         params["cooldown_ms"],
            "exit": {
                "take_profit_bps": params["take_profit_bps"],
                "stop_loss_bps":   params["stop_loss_bps"],
                "max_hold_ms":     params["max_hold_ms"],
            },
        },
    }

    strategy = BurstMomentumStrategy(config, FillModel())
    for event in ticks:
        strategy.on_event(event)

    trades = strategy.trades
    n = len(trades)
    if n == 0:
        return GridResult(
            params=params, n_trades=0, win_rate=0.0, avg_gross_bps=0.0,
            net_pnl_usd=0.0, max_dd_usd=0.0, avg_hold_ms=0.0, pct_timeout=0.0,
        )

    wins = sum(1 for t in trades if t.net_pnl_usd > 0)
    net_pnl = float(sum(t.net_pnl_usd for t in trades))
    avg_gross = float(sum(t.gross_pnl_bps for t in trades)) / n
    avg_hold = sum(t.hold_ms for t in trades) / n
    pct_timeout = sum(1 for t in trades if t.exit_reason == "timeout") / n

    # Max drawdown
    equity = Decimal("0")
    peak = Decimal("0")
    max_dd = Decimal("0")
    for t in trades:
        equity += t.net_pnl_usd
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    return GridResult(
        params=params,
        n_trades=n,
        win_rate=wins / n,
        avg_gross_bps=avg_gross,
        net_pnl_usd=net_pnl,
        max_dd_usd=float(max_dd),
        avg_hold_ms=avg_hold,
        pct_timeout=pct_timeout,
    )


def _print_table(results: list[GridResult], top: int) -> None:
    results = sorted(results, key=lambda r: r.net_pnl_usd, reverse=True)
    results = [r for r in results if r.n_trades > 0][:top]

    if not results:
        print("\n  No parameter combinations generated any trades.")
        print("  → Collect more data (run the recorder for several hours)")
        print("  → Or run --diagnose first to see actual threshold distributions\n")
        return

    W = 110
    print()
    print("=" * W)
    print(f"  Grid Search Results (top {len(results)}, sorted by net P&L)")
    print("=" * W)
    hdr = (
        f"{'#':>3}  "
        f"{'cnt_trig':>8}  "
        f"{'mov_bps':>7}  "
        f"{'TP':>6}  "
        f"{'SL':>6}  "
        f"{'hold_ms':>7}  "
        f"{'trades':>6}  "
        f"{'win%':>5}  "
        f"{'avg_gross':>9}  "
        f"{'net_pnl':>8}  "
        f"{'max_dd':>8}  "
        f"{'timeout%':>8}  "
        f"{'note'}"
    )
    print(f"  {hdr}")
    print("  " + "-" * (W - 2))

    for i, r in enumerate(results, 1):
        p = r.params
        note = ""
        if r.avg_gross_bps < ROUND_TRIP_COST_BPS:
            note = "⚠ avg gross < fees"
        elif r.win_rate < 0.40:
            note = "⚠ low win rate"
        elif r.pct_timeout > 0.70:
            note = "⚠ mostly timeout exits"
        elif r.net_pnl_usd > 0 and r.win_rate >= 0.45:
            note = "✓ candidate"

        row = (
            f"{i:>3}  "
            f"{p['trade_count_trigger']:>8}  "
            f"{p['move_bps_trigger']:>7.1f}  "
            f"{p['take_profit_bps']:>6.1f}  "
            f"{p['stop_loss_bps']:>6.1f}  "
            f"{p['max_hold_ms']:>7}  "
            f"{r.n_trades:>6}  "
            f"{r.win_rate*100:>5.1f}  "
            f"{r.avg_gross_bps:>9.2f}  "
            f"${r.net_pnl_usd:>7.4f}  "
            f"${r.max_dd_usd:>7.4f}  "
            f"{r.pct_timeout*100:>8.1f}  "
            f"{note}"
        )
        print(f"  {row}")

    print("=" * W)
    print()
    print(f"  Round-trip cost floor: {ROUND_TRIP_COST_BPS} bps (fee 4×2 + slippage 1.5×2)")
    print("  TP must beat this floor on average. avg_gross > 11 bps is the minimum signal.")
    print()

    # Best candidate summary
    best = results[0]
    if best.net_pnl_usd > 0:
        p = best.params
        print("  Best candidate config snippet (paste into configs/default.yaml):")
        print()
        print("  strategy:")
        print(f"    window_ms: {p['window_ms']}")
        print(f"    trade_count_trigger: {p['trade_count_trigger']}")
        print(f"    move_bps_trigger: {p['move_bps_trigger']}")
        print(f"    cooldown_ms: {p['cooldown_ms']}")
        print("    exit:")
        print(f"      take_profit_bps: {p['take_profit_bps']}")
        print(f"      stop_loss_bps: {p['stop_loss_bps']}")
        print(f"      max_hold_ms: {p['max_hold_ms']}")
    else:
        print("  ⚠  No profitable combination found in this dataset.")
        print("  → The edge may not be present in this recording session.")
        print("  → Collect data during higher-volatility windows (e.g. US market open).")
    print()


async def run(symbol: str, top: int, min_trades: int, start: datetime | None, end: datetime | None, max_ticks: int = 100_000) -> None:
    # Import here to avoid circular import at module level
    from backend.config import load_trading_config

    base_config = load_trading_config()

    # ── Load ticks once ──────────────────────────────────────────────────────
    log.info(f"Loading ticks for {symbol} from DB (once, cap={max_ticks:,})…")
    async with AsyncSessionLocal() as session:
        replayer = TickReplayer(session)
        ticks: list[BookTick | AggTrade] = []
        async for event in replayer.replay(symbol, start=start, end=end, limit=max_ticks):
            ticks.append(event)
    log.info(f"Loaded {len(ticks):,} ticks into memory")

    if not ticks:
        print(f"\n  No ticks found for {symbol}. Start the recorder first.\n")
        return

    # ── Build parameter combinations ─────────────────────────────────────────
    keys = list(GRID.keys())
    combos = list(itertools.product(*[GRID[k] for k in keys]))
    total = len(combos)
    log.info(f"Sweeping {total:,} parameter combinations…")

    results: list[GridResult] = []
    for i, values in enumerate(combos):
        params = dict(zip(keys, values))
        r = _run_once(ticks, params, base_config)
        if r.n_trades >= min_trades:
            results.append(r)
        if (i + 1) % 100 == 0:
            log.info(f"  {i+1}/{total} done, {len(results)} with ≥{min_trades} trades…")

    log.info(f"Done. {len(results)}/{total} combinations had ≥{min_trades} trades.")
    _print_table(results, top)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Burst momentum grid search")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--top", type=int, default=15, help="Show top N results")
    parser.add_argument("--min-trades", type=int, default=3, help="Minimum trades to include a result")
    parser.add_argument("--start", default=None, help="ISO datetime e.g. 2026-03-01T09:00:00")
    parser.add_argument("--end", default=None, help="ISO datetime e.g. 2026-03-01T10:00:00")
    parser.add_argument("--max-ticks", type=int, default=100_000, help="Max ticks to load (default 100k, prevents OOM)")
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc) if args.start else None
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else None

    await run(args.symbol, args.top, args.min_trades, start, end, args.max_ticks)


if __name__ == "__main__":
    uvloop.run(main())
