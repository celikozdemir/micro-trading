"""
Micro Runner — Phase 1: Market Data Recorder

Connects to Binance WebSocket streams, normalizes tick data,
buffers in-memory, and flushes to TimescaleDB periodically.

Run:
    python -m workers.micro_runner
    # or with a custom config:
    TRADING_CONFIG=configs/default.yaml python -m workers.micro_runner
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections import defaultdict, deque
from datetime import datetime, timezone

import structlog
import uvloop
from sqlalchemy import text

from backend.config import load_trading_config, settings
from backend.core.data.feeds.binance_ws import BinanceWebSocketFeed
from backend.core.data.normalizer import AggTrade, BookTick, ms_to_dt
from backend.db.session import AsyncSessionLocal, Base, engine
from backend.models.market_data import AggTrade as AggTradeModel
from backend.models.market_data import BookTick as BookTickModel
from backend.models.market_data import LatencyMetric

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
)

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

log = structlog.get_logger(__name__)


class MicroRunner:
    """
    Phase 1: Market Data Recorder.

    Hot path  → on_event() — no I/O, only appends to in-memory buffers.
    Cold path → _flush_loop() — drains buffers to DB every flush_interval_s.
    """

    def __init__(self, config: dict):
        self.config = config
        telemetry = config.get("telemetry", {})
        self._flush_interval_s: float = telemetry.get("flush_interval_s", 1.0)
        self._latency_log_interval_s: float = telemetry.get("latency_log_interval_s", 60.0)

        self._book_tick_buffer: list[BookTick] = []
        self._agg_trade_buffer: list[AggTrade] = []
        self._lag_samples: dict[str, deque[int]] = defaultdict(lambda: deque(maxlen=1000))
        self._running = False
        self._feed: BinanceWebSocketFeed | None = None
        # Cumulative counters exposed to the dashboard
        self.total_book_ticks: int = 0
        self.total_agg_trades: int = 0

    # ------------------------------------------------------------------ #
    # Hot path                                                             #
    # ------------------------------------------------------------------ #

    def on_event(self, event: BookTick | AggTrade) -> None:
        """Called synchronously from the WS handler. Zero I/O."""
        if isinstance(event, BookTick):
            self._book_tick_buffer.append(event)
            self._lag_samples[f"{event.symbol}:bookTicker"].append(event.lag_ms)
            self.total_book_ticks += 1
        elif isinstance(event, AggTrade):
            self._agg_trade_buffer.append(event)
            self._lag_samples[f"{event.symbol}:aggTrade"].append(event.lag_ms)
            self.total_agg_trades += 1

    def get_stats(self) -> dict:
        return {
            "total_book_ticks": self.total_book_ticks,
            "total_agg_trades": self.total_agg_trades,
            "buffer_book_ticks": len(self._book_tick_buffer),
            "buffer_agg_trades": len(self._agg_trade_buffer),
        }

    # ------------------------------------------------------------------ #
    # Cold path — DB writes                                                #
    # ------------------------------------------------------------------ #

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._flush_interval_s)
            await self._flush()

    async def _flush(self) -> None:
        # Atomically drain buffers
        book_batch, self._book_tick_buffer = self._book_tick_buffer, []
        trade_batch, self._agg_trade_buffer = self._agg_trade_buffer, []

        if not book_batch and not trade_batch:
            return

        async with AsyncSessionLocal() as session:
            try:
                for bt in book_batch:
                    session.add(
                        BookTickModel(
                            symbol=bt.symbol,
                            timestamp_exchange=ms_to_dt(bt.timestamp_exchange_ms),
                            timestamp_local=ms_to_dt(bt.timestamp_local_ms),
                            bid_price=bt.bid_price,
                            bid_qty=bt.bid_qty,
                            ask_price=bt.ask_price,
                            ask_qty=bt.ask_qty,
                            spread_bps=bt.spread_bps,
                            lag_ms=bt.lag_ms,
                        )
                    )
                for at in trade_batch:
                    session.add(
                        AggTradeModel(
                            symbol=at.symbol,
                            trade_id=at.trade_id,
                            timestamp_exchange=ms_to_dt(at.timestamp_exchange_ms),
                            timestamp_local=ms_to_dt(at.timestamp_local_ms),
                            price=at.price,
                            qty=at.qty,
                            is_buyer_maker=at.is_buyer_maker,
                            lag_ms=at.lag_ms,
                        )
                    )
                await session.commit()
                log.info(
                    "Flushed tick data",
                    book_ticks=len(book_batch),
                    agg_trades=len(trade_batch),
                )
            except Exception as e:
                await session.rollback()
                log.error("DB flush failed", error=str(e))

    # ------------------------------------------------------------------ #
    # Latency reporting                                                    #
    # ------------------------------------------------------------------ #

    async def _latency_log_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._latency_log_interval_s)
            await self._log_latency()

    async def _log_latency(self) -> None:
        now = datetime.now(tz=timezone.utc)
        async with AsyncSessionLocal() as session:
            try:
                for key, samples in self._lag_samples.items():
                    if len(samples) < 5:
                        continue
                    symbol, stream = key.split(":")
                    sorted_s = sorted(samples)
                    n = len(sorted_s)
                    p50 = sorted_s[n // 2]
                    p95 = sorted_s[int(n * 0.95)]
                    p99 = sorted_s[int(n * 0.99)]
                    log.info(
                        "WS latency",
                        symbol=symbol,
                        stream=stream,
                        p50_ms=p50,
                        p95_ms=p95,
                        p99_ms=p99,
                        samples=n,
                    )
                    session.add(
                        LatencyMetric(
                            timestamp_local=now,
                            symbol=symbol,
                            stream=stream,
                            p50_lag_ms=p50,
                            p95_lag_ms=p95,
                            p99_lag_ms=p99,
                            sample_count=n,
                        )
                    )
                await session.commit()
            except Exception as e:
                await session.rollback()
                log.error("Latency log flush failed", error=str(e))

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        self._running = True
        cfg = self.config
        risk = cfg["risk"]

        self._feed = feed = BinanceWebSocketFeed(
            venue=cfg["venue"],
            symbols=cfg["symbols"],
            streams=cfg["data_streams"],
            on_event=self.on_event,
            max_reconnects=risk["reconnect_storm"]["max_reconnects"],
            reconnect_window_min=risk["reconnect_storm"]["window_min"],
        )

        log.info(
            "Market Data Recorder starting",
            mode=cfg["mode"],
            venue=cfg["venue"],
            symbols=cfg["symbols"],
            streams=cfg["data_streams"],
        )

        await asyncio.gather(
            feed.run(),
            self._flush_loop(),
            self._latency_log_loop(),
        )

    async def stop(self) -> None:
        log.info("Shutting down, flushing remaining data...")
        self._running = False
        if self._feed is not None:
            await self._feed.stop()
        await self._flush()
        log.info("Shutdown complete.")


# ------------------------------------------------------------------ #
# DB bootstrap                                                         #
# ------------------------------------------------------------------ #


async def init_db() -> None:
    """Create tables and TimescaleDB hypertables if available."""
    # Transaction 1: create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("DB tables ready")

    # Transaction 2 (per table): promote to TimescaleDB hypertables if available.
    # Each runs in its own transaction so a failure doesn't roll back table creation.
    for table, col in [
        ("book_ticks", "timestamp_exchange"),
        ("agg_trades", "timestamp_exchange"),
        ("latency_metrics", "timestamp_local"),
    ]:
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        f"SELECT create_hypertable('{table}', '{col}', if_not_exists => TRUE)"
                    )
                )
            log.info("TimescaleDB hypertable ready", table=table)
        except Exception:
            # TimescaleDB extension not enabled — plain Postgres tables are fine
            pass


# ------------------------------------------------------------------ #
# Entry point                                                          #
# ------------------------------------------------------------------ #


async def main() -> None:
    config_path = os.environ.get("TRADING_CONFIG", "configs/default.yaml")
    config = load_trading_config(config_path)

    runner = MicroRunner(config)

    loop = asyncio.get_event_loop()

    def _shutdown(sig: signal.Signals) -> None:
        log.info("Signal received, shutting down...", signal=sig.name)
        asyncio.create_task(runner.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown, sig)

    await init_db()
    await runner.run()


if __name__ == "__main__":
    uvloop.run(main())
