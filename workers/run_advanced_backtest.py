"""
M2 — Strategy Simulator: Burst Momentum

Replays recorded ticks from TimescaleDB and runs Strategy A (Burst Momentum)
against them, printing a P&L report.

Usage:
    python -m workers.run_advanced_backtest
    python -m workers.run_advanced_backtest --symbol ETHUSDT --primary BTCUSDT
    python -m workers.run_advanced_backtest --symbol BTCUSDT --start 2026-03-01T09:00:00 --end 2026-03-01T10:00:00
    python -m workers.run_advanced_backtest --diagnose        # show threshold recommendations only
"""

from __future__ import annotations

import argparse
import logging
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal

import uvloop

from backend.config import load_trading_config
from backend.core.backtester.fill_model import FillModel
from backend.core.backtester.tick_replayer import TickReplayer
from backend.core.data.normalizer import AggTrade, BookTick
from backend.core.strategy.microstructure.advanced_momentum import BacktestTrade, AdvancedMomentumStrategy
from backend.db.session import AsyncSessionLocal

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

W = 56  # report column width


# ------------------------------------------------------------------ #
# Diagnostic analyser                                                  #
# ------------------------------------------------------------------ #


class DiagnosticAnalyzer:
    """
    Passively observes the same tick stream and records distributions of:
      - trade count per rolling window
      - |mid-price move| in bps per rolling window
      - spread in bps
    Then suggests calibrated trigger thresholds.
    """

    def __init__(self, window_ms: int = 250):
        self.window_ms = window_ms
        self._trade_window: deque[int] = deque()   # timestamps of trades in window
        self._mid_history: deque[tuple[int, Decimal]] = deque()
        self._last_book: BookTick | None = None

        self.trade_count_samples: list[int] = []
        self.mid_move_bps_samples: list[float] = []
        self.spread_bps_samples: list[float] = []

    def on_event(self, event: BookTick | AggTrade) -> None:
        if isinstance(event, BookTick):
            self._last_book = event
            ts = event.timestamp_exchange_ms
            self._mid_history.append((ts, event.mid_price))
            cutoff = ts - self.window_ms
            while self._mid_history and self._mid_history[0][0] < cutoff:
                self._mid_history.popleft()
            self.spread_bps_samples.append(float(event.spread_bps))

        elif isinstance(event, AggTrade):
            now_ms = event.timestamp_exchange_ms
            self._trade_window.append(now_ms)
            cutoff = now_ms - self.window_ms
            while self._trade_window and self._trade_window[0] < cutoff:
                self._trade_window.popleft()

            if self._last_book and len(self._mid_history) >= 2:
                self.trade_count_samples.append(len(self._trade_window))
                window_start_mid = self._mid_history[0][1]
                if window_start_mid > 0:
                    move = abs(float(
                        (self._last_book.mid_price - window_start_mid) / window_start_mid * 10000
                    ))
                    self.mid_move_bps_samples.append(move)

    def print_diagnostics(self, symbol: str) -> None:
        def pct(data: list, p: int) -> float:
            if not data:
                return 0.0
            return data[min(int(len(data) * p / 100), len(data) - 1)]

        sc = sorted(self.trade_count_samples)
        sm = sorted(self.mid_move_bps_samples)
        ss = sorted(self.spread_bps_samples)

        print()
        print("=" * W)
        print(f"  Threshold Diagnostics — {symbol}  (window={self.window_ms}ms)")
        print("=" * W)

        if not sc:
            print("  Not enough data.")
            print("=" * W)
            return

        def row(label: str, value: str) -> str:
            return f"  {label:<28}{value}"

        print()
        print(f"  Trade count per {self.window_ms}ms window  (n={len(sc):,})")
        print("  " + "-" * (W - 2))
        print(row("p50:", f"{pct(sc,50):.0f}"))
        print(row("p75:", f"{pct(sc,75):.0f}"))
        print(row("p90:", f"{pct(sc,90):.0f}"))
        print(row("p95:", f"{pct(sc,95):.0f}"))
        print(row("p99:", f"{pct(sc,99):.0f}"))

        print()
        print(f"  |Mid-price move| bps per window  (n={len(sm):,})")
        print("  " + "-" * (W - 2))
        print(row("p50:", f"{pct(sm,50):.2f} bps"))
        print(row("p75:", f"{pct(sm,75):.2f} bps"))
        print(row("p90:", f"{pct(sm,90):.2f} bps"))
        print(row("p95:", f"{pct(sm,95):.2f} bps"))
        print(row("p99:", f"{pct(sm,99):.2f} bps"))

        print()
        print(f"  Spread  (n={len(ss):,})")
        print("  " + "-" * (W - 2))
        print(row("p50:", f"{pct(ss,50):.2f} bps"))
        print(row("p95:", f"{pct(ss,95):.2f} bps"))

        # Suggest thresholds at p90 (selective but achievable)
        sug_count = max(2, int(pct(sc, 90)))
        sug_move = max(0.5, round(pct(sm, 90), 1)) if sm else 3.0

        print()
        print("  Suggested thresholds  (fires ~top 10% of activity)")
        print("  " + "-" * (W - 2))
        print(row("trade_count_trigger:", str(sug_count)))
        print(row("move_bps_trigger:", f"{sug_move}"))
        print()
        print("  Update configs/default.yaml with these values, then re-run.")
        print("=" * W)
        print()


# ------------------------------------------------------------------ #
# P&L report                                                           #
# ------------------------------------------------------------------ #


def print_report(trades: list[BacktestTrade], symbol: str, config: dict) -> None:
    if not trades:
        return

    wins = [t for t in trades if t.net_pnl_usd > 0]
    total_net_pnl = sum(t.net_pnl_usd for t in trades)
    total_fees = sum(t.fees_usd for t in trades)
    avg_hold_ms = sum(t.hold_ms for t in trades) / len(trades)
    avg_gross_bps = sum(t.gross_pnl_bps for t in trades) / len(trades)

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

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    s = config["strategy"]
    ex = s["exit"]

    def row(label: str, value: str) -> str:
        return f"  {label:<26}{value}"

    print()
    print("=" * W)
    print(f"  Burst Momentum Backtest — {symbol}")
    print("=" * W)
    print()
    print("  Config")
    print("  " + "-" * (W - 2))
    print(row("window_ms:", str(s["window_ms"])))
    print(row("trade_count_trigger:", str(s["trade_count_trigger"])))
    print(row("move_bps_trigger:", str(s["move_bps_trigger"])))
    print(row("take_profit_bps:", str(ex["take_profit_bps"])))
    print(row("stop_loss_bps:", str(ex["stop_loss_bps"])))
    print(row("max_hold_ms:", str(ex["max_hold_ms"])))
    print(row("slippage_bps:", "1.5 (conservative)"))
    print(row("fee_bps:", "4.0 per side (taker)"))
    print()
    print("  Results")
    print("  " + "-" * (W - 2))
    print(row("Total trades:", str(len(trades))))
    print(row("Wins / Losses:", f"{len(wins)} / {len(trades) - len(wins)}"))
    print(row("Win rate:", f"{len(wins) / len(trades) * 100:.1f}%"))
    print(row("Avg hold:", f"{avg_hold_ms:.0f} ms"))
    print(row("Avg gross P&L:", f"{float(avg_gross_bps):.2f} bps"))
    print(row("Total fees:", f"${float(total_fees):.4f}"))
    print(row("Net P&L:", f"${float(total_net_pnl):.4f}"))
    print(row("Max drawdown:", f"${float(max_dd):.4f}"))
    print()
    print("  Exit reasons")
    print("  " + "-" * (W - 2))
    for reason, count in sorted(reasons.items()):
        print(row(f"  {reason}:", f"{count}  ({count / len(trades) * 100:.0f}%)"))
    print()
    print("=" * W)
    if len(wins) / len(trades) < 0.45:
        print("  Note: win rate < 45% — consider raising move_bps_trigger")
    if reasons.get("timeout", 0) / len(trades) > 0.6:
        print("  Note: >60% timeout exits — consider reducing max_hold_ms")
    print()


# ------------------------------------------------------------------ #
# Runner                                                               #
# ------------------------------------------------------------------ #


async def run(
    symbol: str,
    primary_symbol: str,
    start: datetime | None,
    end: datetime | None,
    diagnose: bool,
    max_ticks: int = 100_000,
) -> None:
    config = load_trading_config()
    window_ms = config["strategy"]["window_ms"]

    # Maker execution lowers fees significantly (e.g. 2 bps per side vs 4-5)
    from decimal import Decimal as D
    fill_model = FillModel(slippage_bps=D("0.0"), fee_bps=D("2.0"))
    strategy = AdvancedMomentumStrategy(config, fill_model, primary_symbol=primary_symbol)
    analyzer = DiagnosticAnalyzer(window_ms=window_ms)

    async with AsyncSessionLocal() as session:
        replayer = TickReplayer(session)
        tick_count = 0
        
        symbols_to_load = [symbol]
        if symbol != primary_symbol:
            symbols_to_load.append(primary_symbol)

        if start is not None and end is not None:
            log.info(f"Loading ALL ticks for {symbols_to_load} in [{start} → {end}]...")
            replay_kwargs: dict = {}
        else:
            book_cap = min(20_000, max_ticks // 5)
            trade_cap = max_ticks - book_cap
            log.info(f"Loading ticks for {symbols_to_load} with limit max_ticks={max_ticks:,} (book_cap={book_cap:,}, trade_cap={trade_cap:,})...")
            log.info("  (Provide both --start and --end to load a full, un-capped time range.)")
            replay_kwargs = {"book_limit": book_cap, "trade_limit": trade_cap}

        async for event in replayer.replay(symbols_to_load, start=start, end=end, **replay_kwargs):
            strategy.on_event(event)
            analyzer.on_event(event)
            tick_count += 1

    log.info(f"Replayed {tick_count:,} ticks  →  {len(strategy.trades)} trades generated")

    # Always show diagnostics when 0 trades, or when --diagnose flag is set
    if diagnose or len(strategy.trades) == 0:
        analyzer.print_diagnostics(symbol)

    if len(strategy.trades) > 0:
        print_report(strategy.trades, symbol, config)
    elif not diagnose:
        print(f"\n  0 trades — update configs/default.yaml with the suggested")
        print("  thresholds above and re-run.\n")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Advanced Burst momentum backtest")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--primary", default="BTCUSDT", help="Primary symbol for correlation filter")
    parser.add_argument("--start", default=None, help="ISO datetime e.g. 2026-03-01T09:00:00")
    parser.add_argument("--end", default=None, help="ISO datetime e.g. 2026-03-01T10:00:00")
    parser.add_argument("--max-ticks", type=int, default=100_000, help="Max ticks to load (default 100k, prevents DB timeouts)")
    parser.add_argument("--diagnose", action="store_true", help="Show threshold diagnostics")
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc) if args.start else None
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else None

    await run(args.symbol, args.primary, start, end, args.diagnose, args.max_ticks)


if __name__ == "__main__":
    uvloop.run(main())
