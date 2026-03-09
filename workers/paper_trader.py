"""
Paper Trader — Phase 3: Live Paper Execution

Runs BurstMomentumStrategy on real-time Binance WebSocket data.
No real orders are placed. Every completed strategy decision is saved to the
paper_trades table so you can measure live edge over time.

Runs alongside micro_runner.py (which handles market data recording).
This process maintains its own WS connection and only writes to paper_trades.

Usage:
    python -m workers.paper_trader          # taker fill model (default, conservative)
    python -m workers.paper_trader --maker  # maker fill model (optimistic)
    python -m workers.paper_trader --maker --primary BTCUSDT  # maker fill with BTC-correlated filter

Systemd: see deploy/algo-paper.service
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timezone

import structlog
import uvloop
from sqlalchemy import text

from backend.config import load_trading_config
from backend.core.backtester.fill_model import FillModel
from backend.core.data.feeds.binance_ws import BinanceWebSocketFeed
from backend.core.data.normalizer import AggTrade, BookTick, MarkPrice
from backend.core.strategy.microstructure.advanced_momentum import (
    BacktestTrade,
    AdvancedMomentumStrategy,
)
from backend.db.session import AsyncSessionLocal, Base, engine
from backend.models.paper_trade import PaperTrade

logging.basicConfig(level=logging.INFO, format="%(message)s")

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


class PaperTrader:
    """
    Live paper trading engine.

    Hot path  → on_event() — feeds each tick to the strategy, buffers new trades.
    Cold path → _flush_loop() — persists buffered trades to DB every second.
    """

    def __init__(self, config: dict, fill_model: FillModel, primary_symbol: str = "BTCUSDT"):
        self.config = config
        telemetry = config.get("telemetry", {})
        self._flush_interval_s: float = telemetry.get("flush_interval_s", 1.0)
        self._pnl_log_interval_s: float = telemetry.get("latency_log_interval_s", 60.0)

        self._strategy = AdvancedMomentumStrategy(config, fill_model, primary_symbol=primary_symbol)
        self._last_trade_count: int = 0
        self._trade_buffer: list[BacktestTrade] = []
        self._running = False
        self._feed: BinanceWebSocketFeed | None = None

        # Running P&L accumulators
        self._total_trades: int = 0
        self._total_wins: int = 0
        self._total_net_pnl: float = 0.0

        # Live state shared with FastAPI via JSON file
        s = self._strategy
        p = s.sym_params.get(primary_symbol, {}) if hasattr(s, "sym_params") else {}
        self._live_state: dict = {
            "ts_ms": None,
            "symbols": {},
            "positions": {},
            "config": {
                "take_profit_bps":  float(p.get("take_profit_bps", 0)),
                "stop_loss_bps":    float(p.get("stop_loss_bps", 0)),
                "trail_trigger_bps": float(p.get("trail_trigger_bps", 0)),
                "trail_bps":        float(p.get("trail_bps", 0)),
            },
        }
        self._state_file: str = os.environ.get("LIVE_STATE_FILE", "/tmp/algo_live_state.json")

    # ------------------------------------------------------------------ #
    # Hot path                                                             #
    # ------------------------------------------------------------------ #

    def on_event(self, event: BookTick | AggTrade | MarkPrice) -> None:
        """Feed tick to strategy. Zero I/O."""
        self._strategy.on_event(event)

        # Track funding rate in live state
        if isinstance(event, MarkPrice):
            self._live_state.setdefault("funding", {})[event.symbol] = {
                "rate": float(event.funding_rate),
                "next_time_ms": event.next_funding_time_ms,
                "mark_price": float(event.mark_price),
            }
            return

        # Update in-memory live state (no I/O — written by _state_write_loop)
        if isinstance(event, BookTick):
            self._live_state["ts_ms"] = event.timestamp_exchange_ms
            self._live_state["symbols"][event.symbol] = {
                "bid": float(event.bid_price),
                "ask": float(event.ask_price),
                "mid": float(event.mid_price),
                "spread_bps": float(event.spread_bps),
                "ts_ms": event.timestamp_exchange_ms,
            }
            # Reflect current open position P&L
            sym_state = self._strategy._states.get(event.symbol)
            if sym_state is not None:
                op = sym_state.open_position
                if op is not None:
                    if op.side == "BUY":
                        pnl_bps = float((event.bid_price - op.entry_price) / op.entry_mid * 10000)
                    else:
                        pnl_bps = float((op.entry_price - event.ask_price) / op.entry_mid * 10000)
                    self._live_state["positions"][event.symbol] = {
                        "side": op.side,
                        "entry_price": float(op.entry_price),
                        "entry_time_ms": op.entry_time_ms,
                        "qty": float(op.qty),
                        "current_pnl_bps": round(pnl_bps, 3),
                        "high_watermark_bps": round(op.high_watermark_bps, 3),
                    }
                else:
                    self._live_state["positions"][event.symbol] = None

        # Detect newly completed trades
        new_count = len(self._strategy.trades)
        if new_count > self._last_trade_count:
            new_trades = self._strategy.trades[self._last_trade_count:]
            self._trade_buffer.extend(new_trades)
            self._last_trade_count = new_count

            # Log immediately so each trade is visible in journalctl
            for t in new_trades:
                log.info(
                    "Paper trade",
                    symbol=t.symbol,
                    side=t.side,
                    exit_reason=t.exit_reason,
                    hold_ms=t.hold_ms,
                    gross_bps=f"{float(t.gross_pnl_bps):.2f}",
                    net_usd=f"{float(t.net_pnl_usd):.4f}",
                )

    # ------------------------------------------------------------------ #
    # Cold path — DB writes                                                #
    # ------------------------------------------------------------------ #

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._flush_interval_s)
            await self._flush()

    async def _flush(self) -> None:
        if not self._trade_buffer:
            return

        batch, self._trade_buffer = self._trade_buffer, []

        async with AsyncSessionLocal() as session:
            try:
                for t in batch:
                    session.add(
                        PaperTrade(
                            symbol=t.symbol,
                            side=t.side,
                            entry_time_ms=t.entry_time_ms,
                            exit_time_ms=t.exit_time_ms,
                            entry_price=t.entry_price,
                            exit_price=t.exit_price,
                            qty=t.qty,
                            exit_reason=t.exit_reason,
                            hold_ms=t.hold_ms,
                            gross_pnl_bps=t.gross_pnl_bps,
                            gross_pnl_usd=t.gross_pnl_usd,
                            fees_usd=t.fees_usd,
                            net_pnl_usd=t.net_pnl_usd,
                        )
                    )
                    self._total_trades += 1
                    if float(t.net_pnl_usd) > 0:
                        self._total_wins += 1
                    self._total_net_pnl += float(t.net_pnl_usd)

                await session.commit()
            except Exception as e:
                await session.rollback()
                # Return trades to buffer so they aren't lost
                self._trade_buffer = batch + self._trade_buffer
                log.error("Paper trade flush failed", error=str(e))

    # ------------------------------------------------------------------ #
    # Live state file (read by FastAPI /api/live endpoint)                #
    # ------------------------------------------------------------------ #

    async def _state_write_loop(self) -> None:
        """Write live market state to a shared JSON file every 200ms."""
        while self._running:
            await asyncio.sleep(0.2)
            try:
                with open(self._state_file, "w") as f:
                    json.dump(self._live_state, f)
            except Exception:
                pass  # Non-critical; dashboard will show stale or no data

    # ------------------------------------------------------------------ #
    # P&L reporting                                                        #
    # ------------------------------------------------------------------ #

    async def _pnl_log_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._pnl_log_interval_s)
            self._log_pnl()

    def _log_pnl(self) -> None:
        if self._total_trades == 0:
            log.info("Paper P&L — no trades yet")
            return
        win_rate = self._total_wins / self._total_trades * 100
        log.info(
            "Paper P&L summary",
            trades=self._total_trades,
            win_rate=f"{win_rate:.1f}%",
            net_pnl_usd=f"${self._total_net_pnl:.4f}",
        )

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

        s = cfg["strategy"]
        ex = s.get("exit", {})
        log.info(
            "Paper Trader starting",
            venue=cfg["venue"],
            symbols=cfg["symbols"],
            move_bps_trigger=s.get("move_bps_trigger", 0),
            intensity_filter_trades=s.get("intensity_filter_trades", 0),
            take_profit_bps=ex.get("take_profit_bps", 0),
            stop_loss_bps=ex.get("stop_loss_bps", 0),
        )

        await asyncio.gather(
            feed.run(),
            self._flush_loop(),
            self._pnl_log_loop(),
            self._state_write_loop(),
        )

    async def stop(self) -> None:
        log.info("Shutting down paper trader...")
        self._running = False
        if self._feed is not None:
            await self._feed.stop()
        await self._flush()
        self._log_pnl()
        log.info("Paper trader stopped.")


# ------------------------------------------------------------------ #
# DB bootstrap                                                         #
# ------------------------------------------------------------------ #


async def init_db() -> None:
    """Create paper_trades table (and any other missing tables)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("DB tables ready")

    # Promote to TimescaleDB hypertable if available
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "SELECT create_hypertable('paper_trades', 'entry_time_ms', "
                    "chunk_time_interval => 86400000, if_not_exists => TRUE)"
                )
            )
        log.info("TimescaleDB hypertable ready", table="paper_trades")
    except Exception:
        pass  # Plain Postgres is fine


# ------------------------------------------------------------------ #
# Entry point                                                          #
# ------------------------------------------------------------------ #


async def main(maker: bool = False, primary_symbol: str = "BTCUSDT") -> None:
    config_path = os.environ.get("TRADING_CONFIG", "configs/default.yaml")
    config = load_trading_config(config_path)

    if maker:
        from decimal import Decimal
        fill_model = FillModel(slippage_bps=Decimal("0.0"), fee_bps=Decimal("2.0"))
        log.info("Fill model: MAKER (fee=2 bps/side, slippage=0)")
    else:
        fill_model = FillModel()
        log.info("Fill model: TAKER (fee=4 bps/side, slippage=1.5 bps)")

    trader = PaperTrader(config, fill_model, primary_symbol=primary_symbol)

    loop = asyncio.get_event_loop()

    def _shutdown(sig: signal.Signals) -> None:
        log.info("Signal received, shutting down...", signal=sig.name)
        asyncio.create_task(trader.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown, sig)

    await init_db()
    await trader.run()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Paper trader — live strategy on Binance WS")
    parser.add_argument("--maker", action="store_true", help="Use maker fill model instead of taker")
    parser.add_argument("--primary", default="BTCUSDT", help="Primary symbol for correlation filter")
    args = parser.parse_args()
    uvloop.run(main(maker=args.maker, primary_symbol=args.primary))
