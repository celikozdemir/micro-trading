"""
Data retention and cleanup tool.

Shows current DB storage usage (rows + estimated size), identifies pre-deduplication
data that should be removed, and manages rolling retention via TimescaleDB policies.

Usage:
    # Show sizes only
    python -m workers.data_retention --stats

    # Delete all data recorded before a specific date (pre-dedup cleanup)
    python -m workers.data_retention --clean-before 2026-03-02

    # Set up TimescaleDB auto-retention (drops chunks older than N days daily)
    python -m workers.data_retention --setup-retention 30

    # Manually delete data older than N days (if TimescaleDB policies not available)
    python -m workers.data_retention --purge-days 30
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone

import uvloop
from sqlalchemy import text

from backend.db.session import AsyncSessionLocal

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


# ── Storage query helpers ──────────────────────────────────────────────────────

_STATS_SQL = """
SELECT
    table_name,
    row_estimate,
    pg_size_pretty(total_bytes) AS total_size,
    pg_size_pretty(table_bytes) AS table_size,
    pg_size_pretty(index_bytes) AS index_size,
    total_bytes
FROM (
    SELECT
        relname AS table_name,
        reltuples::BIGINT AS row_estimate,
        pg_total_relation_size(oid) AS total_bytes,
        pg_relation_size(oid) AS table_bytes,
        pg_total_relation_size(oid) - pg_relation_size(oid) AS index_bytes
    FROM pg_class
    WHERE relname IN ('book_ticks', 'agg_trades', 'latency_metrics', 'paper_trades')
) t
ORDER BY total_bytes DESC;
"""

_RANGE_SQL = """
SELECT
    '{table}' AS table_name,
    COUNT(*) AS row_count,
    MIN(timestamp_exchange) AS earliest,
    MAX(timestamp_exchange) AS latest
FROM {table}
WHERE symbol = :symbol
"""

_RANGE_PAPER_SQL = """
SELECT
    'paper_trades' AS table_name,
    COUNT(*) AS row_count,
    to_timestamp(MIN(entry_time_ms) / 1000.0) AS earliest,
    to_timestamp(MAX(entry_time_ms) / 1000.0) AS latest
FROM paper_trades
"""


async def show_stats() -> None:
    async with AsyncSessionLocal() as session:
        # Table sizes
        result = await session.execute(text(_STATS_SQL))
        rows = result.fetchall()

        W = 70
        print()
        print("=" * W)
        print("  DB Storage Stats")
        print("=" * W)
        print(f"  {'Table':<20}  {'Rows (est)':>12}  {'Table':>10}  {'Index':>10}  {'Total':>10}")
        print("  " + "-" * (W - 2))

        total_bytes = 0
        for row in rows:
            print(
                f"  {row.table_name:<20}  {row.row_estimate:>12,}  "
                f"{row.table_size:>10}  {row.index_size:>10}  {row.total_size:>10}"
            )
            total_bytes += row.total_bytes

        from sqlalchemy.engine import Row
        print("  " + "-" * (W - 2))
        print(f"  {'TOTAL':<20}  {'':>12}  {'':>10}  {'':>10}  {_fmt_bytes(total_bytes):>10}")
        print("=" * W)
        print()

        # Date ranges per symbol
        print("  Date ranges per symbol:")
        print()
        for table in ("book_ticks", "agg_trades"):
            for symbol in ("BTCUSDT", "ETHUSDT"):
                try:
                    r = await session.execute(
                        text(_RANGE_SQL.format(table=table)),
                        {"symbol": symbol},
                    )
                    row = r.fetchone()
                    if row and row.row_count > 0:
                        print(
                            f"  {table:<12} {symbol}  "
                            f"{row.row_count:>10,} rows  "
                            f"{row.earliest.strftime('%Y-%m-%d %H:%M') if row.earliest else 'N/A'} → "
                            f"{row.latest.strftime('%Y-%m-%d %H:%M') if row.latest else 'N/A'} UTC"
                        )
                except Exception:
                    pass  # table may not exist yet

        try:
            r = await session.execute(text(_RANGE_PAPER_SQL))
            row = r.fetchone()
            if row and row.row_count > 0:
                print(
                    f"  {'paper_trades':<12} ALL    "
                    f"{row.row_count:>10,} rows  "
                    f"{row.earliest.strftime('%Y-%m-%d %H:%M') if row.earliest else 'N/A'} → "
                    f"{row.latest.strftime('%Y-%m-%d %H:%M') if row.latest else 'N/A'} UTC"
                )
        except Exception:
            pass

        print()
        _print_retention_recommendation()


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} PB"


def _print_retention_recommendation() -> None:
    print("  Storage estimate (post-dedup data):")
    print("    book_ticks:  ~100–200 rows/min  →  ~200k rows/day per symbol")
    print("    agg_trades:  ~300–500 rows/min  →  ~600k rows/day per symbol")
    print("    Total (2 symbols, uncompressed): ~150 MB/day")
    print("    With TimescaleDB compression (10x): ~15 MB/day")
    print()
    print("  Recommended retention: 30 days (~450 MB compressed, ~4.5 GB uncompressed)")
    print("  Pre-dedup data (before 2026-03-02): should be deleted — ~50x larger per day")
    print()
    print("  To clean pre-dedup data:  python -m workers.data_retention --clean-before 2026-03-02")
    print("  To set auto-retention:    python -m workers.data_retention --setup-retention 30")


# ── Cleanup actions ────────────────────────────────────────────────────────────

async def clean_before(cutoff_date: str) -> None:
    """
    Delete all market data recorded before cutoff_date (ISO date, e.g. 2026-03-02).

    Strategy (fastest → slowest):
      1. TimescaleDB drop_chunks — drops whole chunk files instantly, no row scanning.
      2. Plain DELETE with statement_timeout disabled — for non-TimescaleDB tables.
    Never runs COUNT(*) first; large counts time out on pre-dedup datasets.
    """
    cutoff_dt = datetime.fromisoformat(cutoff_date).replace(tzinfo=timezone.utc)
    print()
    print(f"  Deleting all book_ticks and agg_trades before {cutoff_dt.strftime('%Y-%m-%d %H:%M')} UTC...")
    print("  Using TimescaleDB drop_chunks if available, else plain DELETE (no timeout).")
    print()

    from backend.db.session import engine

    for table in ("book_ticks", "agg_trades"):
        # ── Fast path: TimescaleDB drop_chunks in its own transaction ────
        # engine.begin() auto-commits on success, auto-rolls-back on any
        # exception — so a failed drop_chunks never poisons the next block.
        try:
            async with engine.begin() as conn:
                await conn.execute(text("SET statement_timeout = 0"))
                result = await conn.execute(
                    text(f"SELECT drop_chunks('{table}', older_than => :cutoff::timestamptz)"),
                    {"cutoff": cutoff_dt.isoformat()},
                )
                print(f"  {table}: dropped {result.rowcount} TimescaleDB chunk(s) ✓  (fast path)")
            continue  # next table
        except Exception:
            pass  # transaction was rolled back automatically; try DELETE below

        # ── Slow path: plain DELETE in a fresh, clean transaction ────────
        print(f"  {table}: running DELETE (no timeout)...")
        async with engine.begin() as conn:
            await conn.execute(text("SET statement_timeout = 0"))
            result = await conn.execute(
                text(f"DELETE FROM {table} WHERE timestamp_exchange < :cutoff"),
                {"cutoff": cutoff_dt},
            )
            print(f"  {table}: deleted {result.rowcount:,} rows ✓")

    print()
    print("  Done. Run --stats to verify the new sizes.")
    print()


async def setup_retention(days: int) -> None:
    """
    Set up rolling data retention.

    Tries TimescaleDB retention policies first (instant, no DELETE overhead).
    Falls back to installing a systemd timer that runs --purge-days daily at 3am UTC.
    """
    print()
    print(f"  Setting up {days}-day rolling retention...")
    print()

    # ── Try TimescaleDB first ────────────────────────────────────────────
    timescale_ok = True
    async with AsyncSessionLocal() as session:
        for table in ("book_ticks", "agg_trades"):
            try:
                await session.execute(
                    text(f"SELECT remove_retention_policy('{table}', if_exists => true)")
                )
                await session.execute(
                    text(f"SELECT add_retention_policy('{table}', INTERVAL '{days} days')")
                )
                await session.commit()
                print(f"  {table}: TimescaleDB retention policy → {days} days ✓")
            except Exception:
                await session.rollback()
                timescale_ok = False
                break

    if timescale_ok:
        print()
        print(f"  TimescaleDB will drop chunks older than {days} days automatically (daily).")
        print()
        return

    # ── Fallback: systemd timer ──────────────────────────────────────────
    print("  TimescaleDB not available (plain Postgres tables).")
    print(f"  Creating systemd timer to run --purge-days {days} daily at 03:00 UTC...")
    print()

    _write_systemd_timer(days)


def _write_systemd_timer(days: int) -> None:
    """Write systemd service + timer files for daily retention cleanup."""
    import os
    deploy_dir = os.path.join(os.path.dirname(__file__), "..", "deploy")
    deploy_dir = os.path.normpath(deploy_dir)

    service_content = f"""[Unit]
Description=Algo Trading Data Retention (delete data older than {days} days)
After=network.target docker.service
Requires=docker.service

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/home/ubuntu/algo-trading
ExecStart=/home/ubuntu/algo-trading/.venv/bin/python -m workers.data_retention --purge-days {days}
StandardOutput=journal
StandardError=journal
"""

    timer_content = f"""[Unit]
Description=Daily data retention for algo trading DB (keep last {days} days)

[Timer]
OnCalendar=*-*-* 03:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
"""

    svc_path = os.path.join(deploy_dir, "algo-retention.service")
    tmr_path = os.path.join(deploy_dir, "algo-retention.timer")

    with open(svc_path, "w") as f:
        f.write(service_content)
    with open(tmr_path, "w") as f:
        f.write(timer_content)

    print(f"  Written: deploy/algo-retention.service")
    print(f"  Written: deploy/algo-retention.timer")
    print()
    print("  Install with:")
    print("    sudo cp deploy/algo-retention.service deploy/algo-retention.timer /etc/systemd/system/")
    print("    sudo systemctl daemon-reload")
    print("    sudo systemctl enable --now algo-retention.timer")
    print()
    print("  Verify it's scheduled:")
    print("    sudo systemctl list-timers algo-retention.timer")
    print()


async def purge_days(days: int) -> None:
    """Manually delete data older than N days (fallback when TimescaleDB policies unavailable)."""
    print()
    print(f"  Deleting data older than {days} days (no timeout)...")

    from backend.db.session import engine

    for table in ("book_ticks", "agg_trades"):
        try:
            async with engine.begin() as conn:
                await conn.execute(text("SET statement_timeout = 0"))
                result = await conn.execute(
                    text(f"SELECT drop_chunks('{table}', older_than => NOW() - INTERVAL '{days} days')"),
                )
                print(f"  {table}: dropped {result.rowcount} TimescaleDB chunk(s) ✓")
            continue
        except Exception:
            pass

        async with engine.begin() as conn:
            await conn.execute(text("SET statement_timeout = 0"))
            result = await conn.execute(
                text(
                    f"DELETE FROM {table} "
                    f"WHERE timestamp_exchange < NOW() - INTERVAL '{days} days'"
                )
            )
            print(f"  {table}: deleted {result.rowcount:,} rows ✓")

    print()
    print("  Done.")
    print()


# ── Entry point ────────────────────────────────────────────────────────────────

async def main() -> None:
    from backend.config import load_trading_config
    load_trading_config()  # initialise settings (DB URL etc.)

    parser = argparse.ArgumentParser(description="Data retention and cleanup tool")
    parser.add_argument("--stats", action="store_true", help="Show storage stats and exit")
    parser.add_argument(
        "--clean-before",
        metavar="DATE",
        help="Delete all market data before this ISO date (e.g. 2026-03-02)"
    )
    parser.add_argument(
        "--setup-retention",
        metavar="DAYS",
        type=int,
        help="Set TimescaleDB auto-retention policy to N days (e.g. 30)"
    )
    parser.add_argument(
        "--purge-days",
        metavar="DAYS",
        type=int,
        help="Manually delete data older than N days (fallback if no TimescaleDB)"
    )
    args = parser.parse_args()

    if not any([args.stats, args.clean_before, args.setup_retention, args.purge_days]):
        parser.print_help()
        return

    if args.stats or args.clean_before or args.setup_retention or args.purge_days:
        await show_stats()

    if args.clean_before:
        confirm = input(f"  Confirm: delete all data before {args.clean_before}? [yes/N] ").strip()
        if confirm.lower() != "yes":
            print("  Aborted.")
            return
        await clean_before(args.clean_before)

    if args.setup_retention:
        await setup_retention(args.setup_retention)

    if args.purge_days:
        confirm = input(f"  Confirm: delete data older than {args.purge_days} days? [yes/N] ").strip()
        if confirm.lower() != "yes":
            print("  Aborted.")
            return
        await purge_days(args.purge_days)


if __name__ == "__main__":
    uvloop.run(main())
