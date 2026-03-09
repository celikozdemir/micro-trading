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
from sqlalchemy.ext.asyncio import create_async_engine

from backend.config import settings
from backend.core.ml.features import FEATURE_NAMES, FeatureExtractor

FORWARD_WINDOW_MS = 300_000   # 5-minute forward look for P&L
FEE_BPS = 4.0                 # round-trip taker fees in bps
CHUNK_HOURS = 3               # load data in 3-hour chunks to avoid timeouts

# Separate engine with long timeout for batch data loading
_batch_engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=2,
    max_overflow=0,
    connect_args={"server_settings": {"statement_timeout": "600000"}},  # 10 min
)


async def _load_chunk(sql: str, params: dict) -> list:
    async with _batch_engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        return result.fetchall()


async def load_ticks_chunked(symbol: str, start_dt: datetime, end_dt: datetime):
    """Load book ticks and agg trades in time chunks, merge in memory."""
    all_events = []
    chunk_start = start_dt

    while chunk_start < end_dt:
        chunk_end = min(chunk_start + timedelta(hours=CHUNK_HOURS), end_dt)
        params = {"sym": symbol, "s": chunk_start, "e": chunk_end}

        t0 = time.time()

        books = await _load_chunk(
            "SELECT timestamp_exchange, bid_price, bid_qty, ask_price, ask_qty "
            "FROM book_ticks WHERE symbol = :sym AND timestamp_exchange >= :s AND timestamp_exchange < :e",
            params,
        )
        trades = await _load_chunk(
            "SELECT timestamp_exchange, price, qty, is_buyer_maker "
            "FROM agg_trades WHERE symbol = :sym AND timestamp_exchange >= :s AND timestamp_exchange < :e",
            params,
        )

        # Tag and merge
        for r in books:
            all_events.append(("book", r[0], r[1], r[2], r[3], r[4], None, None, None))
        for r in trades:
            all_events.append(("trade", r[0], None, None, None, None, r[1], r[2], r[3]))

        elapsed = time.time() - t0
        print(f"  Chunk {chunk_start.strftime('%m/%d %H:%M')} → {chunk_end.strftime('%H:%M')}: "
              f"{len(books):,} books + {len(trades):,} trades ({elapsed:.1f}s)")

        chunk_start = chunk_end

    # Sort by timestamp
    all_events.sort(key=lambda x: x[1])
    print(f"  Total: {len(all_events):,} events")
    return all_events


async def load_forward_prices(symbol: str, start_dt: datetime, end_dt: datetime):
    """Load mid-price samples for forward-looking labeling, in chunks."""
    all_prices = []
    chunk_start = start_dt

    while chunk_start < end_dt:
        chunk_end = min(chunk_start + timedelta(hours=CHUNK_HOURS), end_dt)
        rows = await _load_chunk(
            "SELECT EXTRACT(EPOCH FROM timestamp_exchange) * 1000, "
            "(bid_price + ask_price) / 2.0 "
            "FROM book_ticks WHERE symbol = :sym AND timestamp_exchange >= :s AND timestamp_exchange < :e",
            {"sym": symbol, "s": chunk_start, "e": chunk_end},
        )
        all_prices.extend(rows)
        chunk_start = chunk_end

    all_prices.sort(key=lambda x: x[0])
    print(f"  Forward prices: {len(all_prices):,} points")
    return all_prices


EXIT_OFFSETS_MS = [60_000, 120_000, 300_000]  # check at 1m, 2m, 5m


def compute_forward_labels(
    ts_ms: float,
    mid_price: float,
    price_index: np.ndarray,
    price_values: np.ndarray,
    fee_bps: float = FEE_BPS,
) -> tuple[int, int, float, float]:
    """
    Realistic labeling: check if entry would be profitable at FIXED forward
    offsets (1m, 2m, 5m). Uses the price at the CLOSE of the hold period,
    not the best achievable — this matches actual trading outcomes.

    Label = 1 if ANY of the forward offsets shows a net profit after fees.
    Also tracks the 2-minute forward return as the primary signal.

    Returns: (label_long, label_short, fwd_2m_long_bps, fwd_2m_short_bps)
    """
    long_profitable = False
    short_profitable = False
    fwd_2m_long_bps = 0.0
    fwd_2m_short_bps = 0.0

    for offset_ms in EXIT_OFFSETS_MS:
        target_ms = ts_ms + offset_ms
        idx = np.searchsorted(price_index, target_ms, side="left")

        if idx >= len(price_values):
            continue

        fwd_price = price_values[idx]
        long_bps = float((fwd_price - mid_price) / mid_price * 10_000)
        short_bps = float((mid_price - fwd_price) / mid_price * 10_000)

        if long_bps > fee_bps:
            long_profitable = True
        if short_bps > fee_bps:
            short_profitable = True

        if offset_ms == 120_000:
            fwd_2m_long_bps = long_bps
            fwd_2m_short_bps = short_bps

    return (
        1 if long_profitable else 0,
        1 if short_profitable else 0,
        fwd_2m_long_bps,
        fwd_2m_short_bps,
    )


async def build_dataset(symbol: str, days: int, sample_ms: int = 1000, fee_bps: float = FEE_BPS):
    """Main pipeline: load ticks, extract features, compute labels, save parquet."""
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    # Extend end for forward labeling
    label_end_dt = end_dt
    data_end_dt = end_dt + timedelta(milliseconds=FORWARD_WINDOW_MS)

    # Load all data in chunks to avoid timeouts
    ticks = await load_ticks_chunked(symbol, start_dt, data_end_dt)
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

    first_ts = ticks[0][1]
    first_ts_ms = first_ts.timestamp() * 1000 if hasattr(first_ts, 'timestamp') else float(first_ts)
    warmup_cutoff = first_ts_ms + warmup_ms

    print(f"Replaying {len(ticks):,} ticks, sampling every {sample_ms}ms...")
    t0 = time.time()
    report_interval = len(ticks) // 10 or 1

    for idx, row in enumerate(ticks):
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

        if idx % report_interval == 0 and idx > 0:
            pct = idx / len(ticks) * 100
            print(f"    {pct:.0f}% ({len(samples):,} samples so far)")

        # Sample at intervals after warmup, but don't label beyond end_label
        if ts_ms - last_sample_ms >= sample_ms and ts_ms > warmup_cutoff:
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
                            "fwd_2m_long_bps": blb,
                            "fwd_2m_short_bps": bsb,
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
    print(f"  Mean 2m fwd long:  {df['fwd_2m_long_bps'].mean():.2f} bps")
    print(f"  Mean 2m fwd short: {df['fwd_2m_short_bps'].mean():.2f} bps")

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
    await _batch_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
