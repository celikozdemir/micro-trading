"""
Market microstructure diagnostic.

Scans recorded tick data and reports:
  - How many sweep events occur per hour at each threshold
  - Distribution of 250ms burst magnitudes
  - Busiest time windows in your data (by trade count)
  - Buy vs sell flow balance

This tells you WHEN to run strategies and at WHAT threshold settings.

Usage:
    python -m workers.diagnose --symbol BTCUSDT
    python -m workers.diagnose --symbol BTCUSDT --start 2026-03-02T13:30:00 --end 2026-03-02T14:00:00
    python -m workers.diagnose --symbol BTCUSDT --window-ms 500
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import uvloop

from backend.core.backtester.tick_replayer import TickReplayer
from backend.core.data.normalizer import AggTrade, BookTick
from backend.db.session import AsyncSessionLocal

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def _percentile(sorted_data: list[float], pct: float) -> float:
    if not sorted_data:
        return 0.0
    idx = int(len(sorted_data) * pct)
    return sorted_data[min(idx, len(sorted_data) - 1)]


async def run(
    symbol: str,
    start: datetime | None,
    end: datetime | None,
    window_ms: int,
    max_ticks: int,
) -> None:
    from backend.config import load_trading_config
    load_trading_config()  # just to init settings

    if start is not None and end is not None:
        log.info(f"Loading ALL ticks for {symbol} in [{start} → {end}]…")
        replay_kwargs: dict = {}
    else:
        book_cap = min(30_000, max_ticks // 4)
        trade_cap = max_ticks - book_cap
        log.info(f"Loading ticks (book_cap={book_cap:,}, trade_cap={trade_cap:,})…")
        replay_kwargs = {"book_limit": book_cap, "trade_limit": trade_cap}

    async with AsyncSessionLocal() as session:
        replayer = TickReplayer(session)
        ticks: list[BookTick | AggTrade] = []
        async for event in replayer.replay(symbol, start=start, end=end, **replay_kwargs):
            ticks.append(event)

    if not ticks:
        print(f"\n  No ticks found for {symbol}.\n")
        return

    log.info(f"Loaded {len(ticks):,} ticks. Analyzing…")

    # ── Per-event analysis ────────────────────────────────────────────────────
    book_ticks = [t for t in ticks if isinstance(t, BookTick)]
    agg_trades = [t for t in ticks if isinstance(t, AggTrade)]

    if not book_ticks or not agg_trades:
        print("\n  Need both book_ticks and agg_trades. Record more data first.\n")
        return

    first_ms = min(ticks[0].timestamp_exchange_ms, ticks[-1].timestamp_exchange_ms)
    last_ms  = max(ticks[0].timestamp_exchange_ms, ticks[-1].timestamp_exchange_ms)
    duration_min = (last_ms - first_ms) / 60_000

    first_dt = datetime.fromtimestamp(first_ms / 1000, tz=timezone.utc)
    last_dt  = datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc)

    # ── Rolling window sweep detection ────────────────────────────────────────
    # Scan all events chronologically and compute:
    #   - max |mid_move_bps| seen in each window_ms window
    #   - trade intensity (trades per window)
    trade_window: deque = deque()
    mid_history: deque = deque()
    last_book: BookTick | None = None

    burst_magnitudes: list[float] = []       # |mid_move_bps| at each sweep check
    trade_counts_per_window: list[int] = []  # trade count at each agg_trade event
    buy_qty_total = Decimal("0")
    sell_qty_total = Decimal("0")

    # Per-minute trade count for busy-window analysis
    minute_trade_counts: dict[int, int] = defaultdict(int)  # minute_epoch → count

    for event in ticks:
        now_ms = event.timestamp_exchange_ms
        cutoff = now_ms - window_ms

        if isinstance(event, BookTick):
            last_book = event
            mid_history.append((now_ms, event.mid_price))
            while mid_history and mid_history[0][0] < cutoff:
                mid_history.popleft()

            if len(mid_history) >= 2 and last_book:
                window_start_mid = mid_history[0][1]
                if window_start_mid > 0:
                    move_bps = float(
                        abs((event.mid_price - window_start_mid) / window_start_mid * 10000)
                    )
                    burst_magnitudes.append(move_bps)

        elif isinstance(event, AggTrade):
            is_buy = not event.is_buyer_maker
            trade_window.append((now_ms, event.qty, is_buy))
            mid_history.append((now_ms, event.price))

            if is_buy:
                buy_qty_total += event.qty
            else:
                sell_qty_total += event.qty

            while trade_window and trade_window[0][0] < cutoff:
                trade_window.popleft()
            while mid_history and mid_history[0][0] < cutoff:
                mid_history.popleft()

            trade_counts_per_window.append(len(trade_window))

            # Track per-minute activity
            minute_key = int(now_ms // 60_000)
            minute_trade_counts[minute_key] += 1

    # ── Sweep event counts at various thresholds ──────────────────────────────
    burst_sorted = sorted(burst_magnitudes)
    thresholds = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0]
    sweep_counts = {t: sum(1 for b in burst_magnitudes if b >= t) for t in thresholds}

    # ── Trade count distribution ──────────────────────────────────────────────
    tc_sorted = sorted(trade_counts_per_window)

    # ── Busiest minutes ───────────────────────────────────────────────────────
    busiest = sorted(minute_trade_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    # ── Output ────────────────────────────────────────────────────────────────
    W = 70
    print()
    print("=" * W)
    print(f"  Microstructure Diagnostic — {symbol}")
    print("=" * W)
    print(f"  Period:      {first_dt.strftime('%Y-%m-%d %H:%M:%S')} → {last_dt.strftime('%H:%M:%S')} UTC")
    print(f"  Duration:    {duration_min:.1f} minutes")
    print(f"  Book ticks:  {len(book_ticks):,}  ({len(book_ticks)/duration_min:.0f}/min)")
    print(f"  Agg trades:  {len(agg_trades):,}  ({len(agg_trades)/duration_min:.0f}/min)")
    total_qty = buy_qty_total + sell_qty_total
    if total_qty > 0:
        buy_pct = float(buy_qty_total / total_qty * 100)
        print(f"  Flow balance: {buy_pct:.1f}% buy / {100-buy_pct:.1f}% sell (by qty)")
    print()

    print(f"  ── Sweep frequency ({window_ms}ms window) ──────────────────────────")
    print(f"  {'Threshold':>12}  {'Count':>8}  {'Per hour':>9}  {'% of checks':>12}")
    print("  " + "-" * 46)
    for t in thresholds:
        count = sweep_counts[t]
        per_hour = count / (duration_min / 60) if duration_min > 0 else 0
        pct = count / len(burst_magnitudes) * 100 if burst_magnitudes else 0
        arrow = " ←" if t in [2.0, 3.0, 5.0] else ""
        print(f"  {t:>10.1f} bps  {count:>8,}  {per_hour:>8.0f}/h  {pct:>11.2f}%{arrow}")
    print()

    print(f"  ── Burst magnitude distribution ({window_ms}ms window) ────────────")
    if burst_sorted:
        print(f"  p25:  {_percentile(burst_sorted, 0.25):6.2f} bps")
        print(f"  p50:  {_percentile(burst_sorted, 0.50):6.2f} bps")
        print(f"  p75:  {_percentile(burst_sorted, 0.75):6.2f} bps")
        print(f"  p90:  {_percentile(burst_sorted, 0.90):6.2f} bps")
        print(f"  p95:  {_percentile(burst_sorted, 0.95):6.2f} bps")
        print(f"  p99:  {_percentile(burst_sorted, 0.99):6.2f} bps")
        print(f"  max:  {burst_sorted[-1]:6.2f} bps")
    print()

    print(f"  ── Trade count distribution (per {window_ms}ms window) ─────────────")
    if tc_sorted:
        print(f"  p25:  {_percentile(tc_sorted, 0.25):6.0f} trades")
        print(f"  p50:  {_percentile(tc_sorted, 0.50):6.0f} trades")
        print(f"  p75:  {_percentile(tc_sorted, 0.75):6.0f} trades")
        print(f"  p90:  {_percentile(tc_sorted, 0.90):6.0f} trades")
        print(f"  p95:  {_percentile(tc_sorted, 0.95):6.0f} trades")
        print(f"  p99:  {_percentile(tc_sorted, 0.99):6.0f} trades")
        print(f"  max:  {tc_sorted[-1]:6.0f} trades")
    print()

    print(f"  ── Busiest minutes (top 10 by agg_trade count) ─────────────────")
    for minute_key, count in busiest:
        dt = datetime.fromtimestamp(minute_key * 60, tz=timezone.utc)
        print(f"  {dt.strftime('%H:%M')} UTC  →  {count:>4} trades/min")
    print()

    # ── Recommendation ────────────────────────────────────────────────────────
    print("  ── Recommended settings ─────────────────────────────────────────")
    p75_burst = _percentile(burst_sorted, 0.75) if burst_sorted else 0
    p90_burst = _percentile(burst_sorted, 0.90) if burst_sorted else 0
    p75_trades = _percentile(tc_sorted, 0.75) if tc_sorted else 0
    p90_trades = _percentile(tc_sorted, 0.90) if tc_sorted else 0
    sweeps_at_p75 = sweep_counts.get(
        min(thresholds, key=lambda t: abs(t - p75_burst)), 0
    )
    per_hour_at_p75 = sweeps_at_p75 / (duration_min / 60) if duration_min > 0 else 0

    print(f"  move_bps_trigger:     {p75_burst:.1f}  (p75 burst — fires ~{per_hour_at_p75:.0f}x/hour)")
    print(f"  trade_count_trigger:  {int(p75_trades)}  (p75 window trade count)")
    print(f"  Aggressive (more signals):")
    print(f"    move_bps_trigger:   {p90_burst / 2:.1f}  (p90/2)")
    print(f"    trade_count_trigger:{int(p90_trades // 2)}")
    print()

    if per_hour_at_p75 < 10:
        print("  ⚠  Very few sweep events (<10/hour). This window was quiet.")
        print("     → Try a window with a known volatile event (FOMC, news spike, etc.)")
        print("     → Or use --window-ms 1000 to catch slower moves")
    elif per_hour_at_p75 < 50:
        print("  ●  Moderate activity. Strategies may find some signal.")
        print("     → Run grid_search or grid_search_b on this window.")
    else:
        print("  ✓  High activity. Good conditions for both strategies.")
    print("=" * W)
    print()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Microstructure diagnostic")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start", default=None, help="ISO datetime e.g. 2026-03-02T13:30:00")
    parser.add_argument("--end", default=None, help="ISO datetime e.g. 2026-03-02T14:00:00")
    parser.add_argument("--window-ms", type=int, default=250, help="Rolling window in ms (default 250)")
    parser.add_argument("--max-ticks", type=int, default=150_000, help="Max ticks to load")
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc) if args.start else None
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else None

    await run(args.symbol, start, end, args.window_ms, args.max_ticks)


if __name__ == "__main__":
    uvloop.run(main())
