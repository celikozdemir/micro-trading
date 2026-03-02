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
# Taker (default): (4.0 + 1.5) * 2 = 11.0 bps round-trip
# Maker (--maker):  (2.0 + 0.0) * 2 =  4.0 bps round-trip
TAKER_ROUND_TRIP_BPS = 11.0
MAKER_ROUND_TRIP_BPS = 4.0


# ── Parameter grid ────────────────────────────────────────────────────────────
# Calibrated from diagnostic data (2026-03-02):
#   Typical spike burst = 6–9 bps avg_gross during 7k trades/min events.
#   TP must be BELOW avg_gross to actually trigger — use 3–8 bps range.
#   SL must be tight (2–5 bps) to cut losses quickly in the hot path.
#   With maker fills (round-trip = 4 bps): TP > 4 bps is break-even floor.
#   With taker fills (round-trip = 11 bps): no TP in this grid beats fees.

GRID = {
    "window_ms":               [250],                    # keep fixed — microstructure window
    "trade_count_trigger":     [5, 10, 20, 40, 80],
    "move_bps_trigger":        [1.0, 2.0, 3.5, 5.0],
    "take_profit_bps":         [4.0, 5.0, 6.0, 8.0],   # calibrated: spike avg_gross ~6 bps
    "stop_loss_bps":           [2.0, 3.0, 5.0],         # tight: cut fast if momentum reverses
    "max_hold_ms":             [500, 1000, 2000],        # removed 300ms — too short for 6 bps
    "cooldown_ms":             [500],                    # keep fixed
    # Regime gate: min trades in a 10s intensity window before entry is allowed.
    # 0 = disabled. 300 ≈ 5 trades/s, 600 ≈ 10 trades/s (spike threshold).
    "intensity_filter_trades": [0, 300, 600, 1000],
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


def _run_once(ticks: list[BookTick | AggTrade], params: dict, base_config: dict, fill_model: FillModel) -> GridResult:
    """Replay cached ticks with the given param override."""
    config = {
        **base_config,
        "strategy": {
            **base_config["strategy"],
            "window_ms":                params["window_ms"],
            "trade_count_trigger":      params["trade_count_trigger"],
            "move_bps_trigger":         params["move_bps_trigger"],
            "cooldown_ms":              params["cooldown_ms"],
            "intensity_filter_trades":  params.get("intensity_filter_trades", 0),
            "intensity_filter_window_ms": 10_000,
            "exit": {
                "take_profit_bps": params["take_profit_bps"],
                "stop_loss_bps":   params["stop_loss_bps"],
                "max_hold_ms":     params["max_hold_ms"],
            },
        },
    }

    strategy = BurstMomentumStrategy(config, fill_model)
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


def _print_table(results: list[GridResult], top: int, round_trip_bps: float = TAKER_ROUND_TRIP_BPS) -> None:
    results = sorted(results, key=lambda r: r.net_pnl_usd, reverse=True)
    results = [r for r in results if r.n_trades > 0][:top]

    if not results:
        print("\n  No parameter combinations generated any trades.")
        print("  → Collect more data (run the recorder for several hours)")
        print("  → Or run --diagnose first to see actual threshold distributions\n")
        return

    W = 122
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
        f"{'intens':>6}  "
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
        if r.avg_gross_bps < round_trip_bps:
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
            f"{p.get('intensity_filter_trades', 0):>6}  "
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
    if round_trip_bps == MAKER_ROUND_TRIP_BPS:
        print(f"  Round-trip cost floor: {round_trip_bps} bps (maker fee 2×2 + slippage 0)")
        print("  [--maker mode] avg_gross > 4 bps is the minimum signal.")
    else:
        print(f"  Round-trip cost floor: {round_trip_bps} bps (taker fee 4×2 + slippage 1.5×2)")
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
        print(f"    intensity_filter_trades: {p.get('intensity_filter_trades', 0)}")
        print(f"    intensity_filter_window_ms: 10000")
        print("    exit:")
        print(f"      take_profit_bps: {p['take_profit_bps']}")
        print(f"      stop_loss_bps: {p['stop_loss_bps']}")
        print(f"      max_hold_ms: {p['max_hold_ms']}")
    else:
        print("  ⚠  No profitable combination found in this dataset.")
        print("  → The edge may not be present in this recording session.")
        print("  → Collect data during higher-volatility windows (e.g. US market open).")
    print()


async def run(symbol: str, top: int, min_trades: int, start: datetime | None, end: datetime | None, max_ticks: int = 100_000, maker: bool = False) -> None:
    # Import here to avoid circular import at module level
    from backend.config import load_trading_config

    base_config = load_trading_config()

    # ── Load ticks once ──────────────────────────────────────────────────────
    # When both start and end are given, load all ticks in the window (no limit).
    # Both streams will be synchronized — essential for correct backtesting.
    # Keep the window ≤ 5 minutes during volatile periods to avoid OOM.
    # When only start is given (no end), fall back to the tick cap with a
    # trade-heavy split so at least agg_trades cover a useful time range.
    if start is not None and end is not None:
        log.info(f"Loading ALL ticks for {symbol} in [{start} → {end}] (no limit)…")
        replay_kwargs: dict = {}
    else:
        book_cap = min(20_000, max_ticks // 5)
        trade_cap = max_ticks - book_cap
        log.info(f"Loading ticks for {symbol} from DB (book_cap={book_cap:,}, trade_cap={trade_cap:,})…")
        replay_kwargs = {"book_limit": book_cap, "trade_limit": trade_cap}

    async with AsyncSessionLocal() as session:
        replayer = TickReplayer(session)
        ticks: list[BookTick | AggTrade] = []
        async for event in replayer.replay(symbol, start=start, end=end, **replay_kwargs):
            ticks.append(event)
    log.info(f"Loaded {len(ticks):,} ticks into memory")

    if not ticks:
        print(f"\n  No ticks found for {symbol}. Start the recorder first.\n")
        return

    # ── Build fill model ─────────────────────────────────────────────────────
    if maker:
        from decimal import Decimal as D
        fill_model = FillModel(slippage_bps=D("0.0"), fee_bps=D("2.0"))
        round_trip_bps = MAKER_ROUND_TRIP_BPS
        log.info("Fill model: MAKER (fee=2 bps/side, slippage=0)")
    else:
        fill_model = FillModel()
        round_trip_bps = TAKER_ROUND_TRIP_BPS
        log.info("Fill model: TAKER (fee=4 bps/side, slippage=1.5 bps)")

    # ── Build parameter combinations ─────────────────────────────────────────
    keys = list(GRID.keys())
    combos = list(itertools.product(*[GRID[k] for k in keys]))
    total = len(combos)
    log.info(f"Sweeping {total:,} parameter combinations…")

    results: list[GridResult] = []
    for i, values in enumerate(combos):
        params = dict(zip(keys, values))
        r = _run_once(ticks, params, base_config, fill_model)
        if r.n_trades >= min_trades:
            results.append(r)
        if (i + 1) % 100 == 0:
            log.info(f"  {i+1}/{total} done, {len(results)} with ≥{min_trades} trades…")

    log.info(f"Done. {len(results)}/{total} combinations had ≥{min_trades} trades.")
    _print_table(results, top, round_trip_bps)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Burst momentum grid search")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--top", type=int, default=15, help="Show top N results")
    parser.add_argument("--min-trades", type=int, default=3, help="Minimum trades to include a result")
    parser.add_argument("--start", default=None, help="ISO datetime e.g. 2026-03-01T09:00:00")
    parser.add_argument("--end", default=None, help="ISO datetime e.g. 2026-03-01T10:00:00")
    parser.add_argument("--max-ticks", type=int, default=100_000, help="Max ticks to load (default 100k, prevents OOM)")
    parser.add_argument("--maker", action="store_true", help="Use maker fill model (fee=2 bps/side, slippage=0) instead of taker")
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc) if args.start else None
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else None

    await run(args.symbol, args.top, args.min_trades, start, end, args.max_ticks, args.maker)


if __name__ == "__main__":
    uvloop.run(main())
