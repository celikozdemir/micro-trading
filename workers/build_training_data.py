"""
Build ML training dataset from historical tick data in TimescaleDB.

For every candidate entry point (sampled at configurable intervals), computes:
  - A 13-feature vector from the current microstructure state
  - Forward-looking labels: whether a LONG or SHORT entry would be profitable

Saves the result as a parquet file ready for XGBoost training.

Usage:
    python -m workers.build_training_data --symbol BTCUSDT --days 5
    python -m workers.build_training_data --symbol BTCUSDT --days 5 --sample-ms 500
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import text

from backend.config import settings
from backend.core.ml.features import FEATURE_NAMES, FeatureExtractor
from backend.db.session import engine

FORWARD_WINDOW_MS = 300_000   # 5-minute forward look for P&L
FEE_BPS = 4.0                 # round-trip taker fees in bps
MAKER_FEE_BPS = 2.0           # round-trip maker fees
BATCH_SIZE = 50_000


async def load_ticks(symbol: str, start_dt: datetime, end_dt: datetime):
    """Load interleaved book ticks and agg trades, ordered by exchange timestamp."""

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    book_sql = text("""
        SELECT 'book' as type, timestamp_exchange as ts,
               bid_price, bid_qty, ask_price, ask_qty,
               NULL as price, NULL as qty, NULL as is_buyer_maker
        FROM book_ticks
        WHERE symbol = :sym AND timestamp_exchange >= :s AND timestamp_exchange < :e
    """)

    trade_sql = text("""
        SELECT 'trade' as type, timestamp_exchange as ts,
               NULL as bid_price, NULL as bid_qty, NULL as ask_price, NULL as ask_qty,
               price, qty, is_buyer_maker
        FROM agg_trades
        WHERE symbol = :sym AND timestamp_exchange >= :s AND timestamp_exchange < :e
    """)

    combined_sql = text(f"""
        ({book_sql.text} UNION ALL {trade_sql.text})
        ORDER BY ts ASC
    """)

    params = {"sym": symbol, "s": start_dt, "e": end_dt}

    print(f"Loading ticks for {symbol} from {start_dt} to {end_dt}...")
    t0 = time.time()

    async with engine.connect() as conn:
        result = await conn.execute(combined_sql, params)
        rows = result.fetchall()

    elapsed = time.time() - t0
    print(f"  Loaded {len(rows):,} ticks in {elapsed:.1f}s")
    return rows


async def load_forward_prices(symbol: str, start_dt: datetime, end_dt: datetime):
    """
    Load mid-price samples for forward-looking labeling.
    Uses book ticks sampled at ~1s granularity for efficiency.
    """
    sql = text("""
        SELECT
            EXTRACT(EPOCH FROM timestamp_exchange) * 1000 as ts_ms,
            (bid_price + ask_price) / 2.0 as mid
        FROM book_ticks
        WHERE symbol = :sym AND timestamp_exchange >= :s AND timestamp_exchange < :e
        ORDER BY timestamp_exchange ASC
    """)
    params = {"sym": symbol, "s": start_dt, "e": end_dt}

    print(f"Loading forward prices for labeling...")
    t0 = time.time()

    async with engine.connect() as conn:
        result = await conn.execute(sql, params)
        rows = result.fetchall()

    elapsed = time.time() - t0
    print(f"  Loaded {len(rows):,} price points in {elapsed:.1f}s")
    return rows


def compute_forward_labels(
    ts_ms: float,
    mid_price: float,
    price_index: np.ndarray,
    price_values: np.ndarray,
    fee_bps: float = FEE_BPS,
) -> tuple[int, int, float, float]:
    """
    Given a candidate entry at (ts_ms, mid_price), look forward FORWARD_WINDOW_MS
    and determine if a LONG or SHORT would be profitable net of fees.

    Returns: (label_long, label_short, best_long_bps, best_short_bps)
    """
    end_ms = ts_ms + FORWARD_WINDOW_MS

    # Find the slice of forward prices
    start_idx = np.searchsorted(price_index, ts_ms, side="left")
    end_idx = np.searchsorted(price_index, end_ms, side="right")

    if end_idx <= start_idx:
        return 0, 0, 0.0, 0.0

    forward_prices = price_values[start_idx:end_idx]

    # Best achievable P&L in bps (using max/min of forward prices)
    best_long_bps = float((np.max(forward_prices) - mid_price) / mid_price * 10_000)
    best_short_bps = float((mid_price - np.min(forward_prices)) / mid_price * 10_000)

    label_long = 1 if best_long_bps > fee_bps else 0
    label_short = 1 if best_short_bps > fee_bps else 0

    return label_long, label_short, best_long_bps, best_short_bps


async def build_dataset(symbol: str, days: int, sample_ms: int = 1000, fee_bps: float = FEE_BPS):
    """Main pipeline: load ticks, extract features, compute labels, save parquet."""
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    # Extend end for forward labeling
    label_end_dt = end_dt
    data_end_dt = end_dt + timedelta(milliseconds=FORWARD_WINDOW_MS)

    # Load all data
    ticks = await load_ticks(symbol, start_dt, data_end_dt)
    fwd_prices = await load_forward_prices(symbol, start_dt, data_end_dt)

    if len(ticks) < 1000 or len(fwd_prices) < 100:
        print(f"Insufficient data: {len(ticks)} ticks, {len(fwd_prices)} prices")
        return None

    # Build forward price arrays for fast labeling
    price_ts = np.array([float(r[0]) for r in fwd_prices])
    price_mid = np.array([float(r[1]) for r in fwd_prices])

    # Replay ticks through feature extractor
    extractor = FeatureExtractor()
    last_sample_ms = 0
    end_label_ms = int(label_end_dt.timestamp() * 1000)

    samples = []
    warmup_ms = 60_000  # skip first 60s for EWMA warmup

    print(f"Replaying {len(ticks):,} ticks, sampling every {sample_ms}ms...")
    t0 = time.time()

    for row in ticks:
        tick_type = row[0]
        ts = row[1]
        ts_ms = ts.timestamp() * 1000 if hasattr(ts, 'timestamp') else float(ts)

        if tick_type == "book":
            extractor.on_book_tick(
                symbol, int(ts_ms),
                float(row[2]), float(row[3]),
                float(row[4]), float(row[5]),
            )
        else:
            extractor.on_agg_trade(
                symbol, int(ts_ms),
                float(row[6]), float(row[7]),
                not bool(row[8]),  # is_buyer_maker → is_buy_aggressor
            )

        # Sample at intervals after warmup, but don't label beyond end_label
        if ts_ms - last_sample_ms >= sample_ms and ts_ms > (ticks[0][1].timestamp() * 1000 + warmup_ms):
            if ts_ms < end_label_ms:
                feat = extractor.extract(symbol, int(ts_ms))
                if feat is not None:
                    mid = extractor._states[symbol].mid_price
                    if mid > 0:
                        ll, ls, blb, bsb = compute_forward_labels(
                            ts_ms, mid, price_ts, price_mid, fee_bps
                        )
                        samples.append({
                            "ts_ms": ts_ms,
                            "mid_price": mid,
                            **{name: float(feat[i]) for i, name in enumerate(FEATURE_NAMES)},
                            "label_long": ll,
                            "label_short": ls,
                            "best_long_bps": blb,
                            "best_short_bps": bsb,
                        })
            last_sample_ms = ts_ms

    elapsed = time.time() - t0
    print(f"  Generated {len(samples):,} samples in {elapsed:.1f}s")

    if not samples:
        print("No samples generated!")
        return None

    df = pd.DataFrame(samples)

    # Stats
    n_long = df["label_long"].sum()
    n_short = df["label_short"].sum()
    print(f"\nDataset stats:")
    print(f"  Total samples:   {len(df):,}")
    print(f"  Long profitable: {n_long:,} ({n_long/len(df)*100:.1f}%)")
    print(f"  Short profitable:{n_short:,} ({n_short/len(df)*100:.1f}%)")
    print(f"  Mean best long:  {df['best_long_bps'].mean():.2f} bps")
    print(f"  Mean best short: {df['best_short_bps'].mean():.2f} bps")

    # Save
    os.makedirs("data", exist_ok=True)
    out_path = f"data/training_{symbol}_{days}d.parquet"
    df.to_parquet(out_path, index=False)
    print(f"\nSaved to {out_path}")
    return df


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--sample-ms", type=int, default=1000, help="Sampling interval in ms")
    parser.add_argument("--fee-bps", type=float, default=FEE_BPS, help="Round-trip fee in bps")
    args = parser.parse_args()

    await build_dataset(args.symbol, args.days, args.sample_ms, args.fee_bps)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
