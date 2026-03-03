"""
Strategy A: Burst Momentum Catch

Three-gate entry filter — all must fire simultaneously within the burst window:
  1. Relative intensity spike  — 1s notional > intensity_spike_mult × 60s-avg-per-sec
  2. Volatility expansion      — sigma_fast EWMA > vol_expansion_ratio × sigma_slow EWMA
  3. Notional AFI gate         — (buy_notional - sell_notional) / total > afi_threshold

This eliminates the ~80% of false-positive entries that triggered on ambient noise
when only raw trade count + mid-price displacement were checked.

Config keys (strategy section):
  window_ms:                rolling window for AFI / trade-count floor (ms)
  trade_count_trigger:      minimum trades in window as a noise floor
  move_bps_trigger:         minimum mid-price displacement in window (bps)
  afi_threshold:            notional AFI in [-1,+1] required, default 0.4
  intensity_spike_mult:     1s notional must exceed this × 60s-avg-per-sec, default 5.0
  sigma_fast_halflife_ms:   half-life for fast-vol EWMA, default 1500
  sigma_slow_halflife_ms:   half-life for slow-vol EWMA, default 45000
  vol_expansion_ratio:      sigma_fast/sigma_slow threshold, default 2.5
  exit.take_profit_bps:     close at this gross gain
  exit.stop_loss_bps:       close at this gross loss
  exit.max_hold_ms:         force-close after this hold time
  cooldown_ms:              minimum gap between trades (per symbol)
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from backend.core.backtester.fill_model import FillModel
from backend.core.data.normalizer import AggTrade, BookTick


# ------------------------------------------------------------------ #
# Internal state types                                                 #
# ------------------------------------------------------------------ #


@dataclass
class OpenPosition:
    side: str            # "BUY" | "SELL"
    entry_time_ms: int
    entry_price: Decimal
    qty: Decimal
    entry_mid: Decimal   # mid at entry, used for bps calculations
    high_watermark_bps: float = 0.0  # best gross pnl_bps seen since entry (for trailing stop)


@dataclass
class SymbolState:
    # 250 ms window: (timestamp_ms, notional_usd: float, is_buy_aggressor: bool)
    # Used for AFI and trade-count floor.
    trade_window: deque = field(default_factory=deque)

    # Mid-price history for velocity: (timestamp_ms, mid_price: Decimal)
    mid_history: deque = field(default_factory=deque)

    # 1-second notional window for the intensity spike numerator:
    # (timestamp_ms, notional_usd: float)
    intensity_1s_window: deque = field(default_factory=deque)

    # 60-second rolling baseline for the intensity spike denominator:
    # (timestamp_ms, notional_usd: float)
    baseline_window: deque = field(default_factory=deque)

    # EWMA volatility state — floats for hot-path speed
    sigma_fast: float = 0.0          # short half-life EWMA of |mid_return|
    sigma_slow: float = 0.0          # long half-life EWMA of |mid_return|
    last_ewma_mid: float = 0.0       # previous mid used in EWMA update
    last_ewma_ms: int = 0            # timestamp of last EWMA update

    # Dual trend EWMAs for momentum-aligned entry (crossover approach)
    trend_ewma: float = 0.0          # slow EWMA of mid-price (5-min halflife)
    short_trend_ewma: float = 0.0    # fast EWMA of mid-price (1-min halflife)
    trend_ewma_start_ms: int = 0     # timestamp of first observation (warm-up check)

    # Most recent book tick (needed for exit checks triggered by agg_trades)
    last_book: Optional[BookTick] = None

    # Cooldown expiry timestamp
    cooldown_until_ms: int = 0

    # Current open position (None = flat)
    open_position: Optional[OpenPosition] = None


# ------------------------------------------------------------------ #
# Trade record (output)                                                #
# ------------------------------------------------------------------ #


@dataclass
class BacktestTrade:
    symbol: str
    side: str
    entry_time_ms: int
    entry_price: Decimal
    exit_time_ms: int
    exit_price: Decimal
    qty: Decimal
    exit_reason: str       # take_profit | stop_loss | timeout
    hold_ms: int
    gross_pnl_usd: Decimal
    gross_pnl_bps: Decimal
    fees_usd: Decimal
    net_pnl_usd: Decimal


# ------------------------------------------------------------------ #
# Strategy                                                             #
# ------------------------------------------------------------------ #


class BurstMomentumStrategy:
    """
    Stateful burst momentum strategy.
    Call on_event() with each BookTick or AggTrade in time order.
    Completed trades accumulate in self.trades.
    """

    def __init__(self, config: dict, fill_model: FillModel):
        s = config["strategy"]

        # Core burst detection
        self.window_ms: int = s["window_ms"]
        self.trade_count_trigger: int = s["trade_count_trigger"]
        self.move_bps_trigger: Decimal = Decimal(str(s["move_bps_trigger"]))

        # Position sizing
        self.entry_qty: dict[str, Decimal] = {
            sym: Decimal(str(qty)) for sym, qty in s["entry_qty"].items()
        }

        # Exit parameters
        ex = s["exit"]
        self.take_profit_bps: Decimal = Decimal(str(ex["take_profit_bps"]))
        self.stop_loss_bps: Decimal = Decimal(str(ex["stop_loss_bps"]))
        self.max_hold_ms: int = ex["max_hold_ms"]
        self.cooldown_ms: int = s["cooldown_ms"]
        # Trailing stop: once gross >= trail_trigger_bps, stop trails trail_bps below
        # the high watermark. The stop moves up with price but never back down.
        # trail_trigger_bps=4.0 = fee cost threshold — only trail once we've covered fees.
        # trail_bps=2.0 — trail 2 bps below peak (net = peak - 2 - 4 fees).
        self.trail_trigger_bps: float = float(ex.get("trail_trigger_bps", 4.0))
        self.trail_bps: float = float(ex.get("trail_bps", 2.0))

        # Risk
        self.max_spread_bps: Decimal = Decimal(str(config["risk"]["max_spread_bps"]))

        # ── NEW: three-gate signal parameters ──────────────────────────
        # Gate 1: Relative intensity spike
        self.intensity_spike_mult: float = float(s.get("intensity_spike_mult", 5.0))

        # Gate 2: Volatility expansion
        self.sigma_fast_halflife_ms: float = float(s.get("sigma_fast_halflife_ms", 1500))
        self.sigma_slow_halflife_ms: float = float(s.get("sigma_slow_halflife_ms", 45_000))
        self.vol_expansion_ratio: float = float(s.get("vol_expansion_ratio", 2.5))

        # Gate 4: Dual-trend direction filter (crossover)
        # Slow EWMA (5-min): medium-term trend reference.
        # Fast EWMA (1-min): short-term momentum direction.
        # Entry allowed only when both agree: fast > slow = uptrend (LONG only).
        self.trend_halflife_ms: float = float(s.get("trend_halflife_ms", 300_000))
        self.short_trend_halflife_ms: float = float(s.get("short_trend_halflife_ms", 60_000))
        # Require this many ms before enforcing — slow EWMA needs ~5 min to stabilize.
        self.trend_warmup_ms: int = int(s.get("trend_warmup_ms", 300_000))

        # Gate 3: Notional AFI
        self.afi_threshold: float = float(s.get("afi_threshold", 0.4))
        # ───────────────────────────────────────────────────────────────

        self.fill_model = fill_model
        self._states: dict[str, SymbolState] = {}
        self.trades: list[BacktestTrade] = []

    # ---------------------------------------------------------------- #
    # Public interface                                                   #
    # ---------------------------------------------------------------- #

    def on_event(self, event: BookTick | AggTrade) -> None:
        if isinstance(event, BookTick):
            self._on_book_tick(event)
        elif isinstance(event, AggTrade):
            self._on_agg_trade(event)

    # ---------------------------------------------------------------- #
    # Event handlers                                                     #
    # ---------------------------------------------------------------- #

    def _on_book_tick(self, bt: BookTick) -> None:
        state = self._get_state(bt.symbol)
        state.last_book = bt

        # Update EWMA vol with actual mid price (most accurate)
        self._update_ewma(state, float(bt.mid_price), bt.timestamp_exchange_ms)

        # Track mid-price history for velocity calculation
        state.mid_history.append((bt.timestamp_exchange_ms, bt.mid_price))
        cutoff = bt.timestamp_exchange_ms - self.window_ms
        while state.mid_history and state.mid_history[0][0] < cutoff:
            state.mid_history.popleft()

        # Check exit conditions for open position
        if state.open_position:
            self._check_exit(bt.symbol, bt.timestamp_exchange_ms, bt)

    def _on_agg_trade(self, at: AggTrade) -> None:
        state = self._get_state(at.symbol)
        now_ms = at.timestamp_exchange_ms
        notional = float(at.qty * at.price)
        is_buy_aggressor = not at.is_buyer_maker

        # 250 ms window — stores notional (not raw qty) for AFI
        state.trade_window.append((now_ms, notional, is_buy_aggressor))
        cutoff_burst = now_ms - self.window_ms
        while state.trade_window and state.trade_window[0][0] < cutoff_burst:
            state.trade_window.popleft()

        # 1-second intensity window (spike numerator)
        state.intensity_1s_window.append((now_ms, notional))
        cutoff_1s = now_ms - 1_000
        while state.intensity_1s_window and state.intensity_1s_window[0][0] < cutoff_1s:
            state.intensity_1s_window.popleft()

        # 60-second baseline window (spike denominator)
        state.baseline_window.append((now_ms, notional))
        cutoff_60s = now_ms - 60_000
        while state.baseline_window and state.baseline_window[0][0] < cutoff_60s:
            state.baseline_window.popleft()

        # Update EWMA vol using trade price as mid proxy between book ticks
        self._update_ewma(state, float(at.price), now_ms)

        # Keep mid_history alive between book ticks (for velocity)
        state.mid_history.append((now_ms, at.price))
        while state.mid_history and state.mid_history[0][0] < cutoff_burst:
            state.mid_history.popleft()

        # Timeout check — fire via agg_trade when book ticks are sparse
        if state.open_position is not None and state.last_book is not None:
            hold_ms = now_ms - state.open_position.entry_time_ms
            if hold_ms >= self.max_hold_ms:
                self._check_exit(at.symbol, now_ms, state.last_book)
                return

        if state.open_position is None and now_ms > state.cooldown_until_ms:
            self._check_entry(at.symbol, now_ms)

    # ---------------------------------------------------------------- #
    # EWMA volatility updater (hot path — floats only)                  #
    # ---------------------------------------------------------------- #

    def _update_ewma(self, state: SymbolState, mid: float, now_ms: int) -> None:
        """Update sigma_fast and sigma_slow with the latest mid-price observation."""
        if state.last_ewma_ms == 0:
            state.last_ewma_mid = mid
            state.last_ewma_ms = now_ms
            return

        dt_ms = now_ms - state.last_ewma_ms
        if dt_ms <= 0:
            return

        # Absolute log-return (|r| = volatility proxy)
        if state.last_ewma_mid > 0:
            ret = abs(mid - state.last_ewma_mid) / state.last_ewma_mid
        else:
            ret = 0.0

        # Time-aware EWMA decay: alpha = 1 - exp(-dt / halflife)
        alpha_fast = 1.0 - math.exp(-dt_ms / self.sigma_fast_halflife_ms)
        alpha_slow = 1.0 - math.exp(-dt_ms / self.sigma_slow_halflife_ms)
        alpha_trend = 1.0 - math.exp(-dt_ms / self.trend_halflife_ms)
        alpha_short_trend = 1.0 - math.exp(-dt_ms / self.short_trend_halflife_ms)

        state.sigma_fast = alpha_fast * ret + (1.0 - alpha_fast) * state.sigma_fast
        state.sigma_slow = alpha_slow * ret + (1.0 - alpha_slow) * state.sigma_slow

        # Trend EWMAs track price level (not returns) for crossover gate
        if state.trend_ewma == 0.0:
            state.trend_ewma = mid
            state.short_trend_ewma = mid
            state.trend_ewma_start_ms = now_ms
        else:
            state.trend_ewma = alpha_trend * mid + (1.0 - alpha_trend) * state.trend_ewma
            state.short_trend_ewma = alpha_short_trend * mid + (1.0 - alpha_short_trend) * state.short_trend_ewma

        state.last_ewma_mid = mid
        state.last_ewma_ms = now_ms

    # ---------------------------------------------------------------- #
    # Entry logic — all gates must pass                                  #
    # ---------------------------------------------------------------- #

    def _check_entry(self, symbol: str, now_ms: int) -> None:
        state = self._get_state(symbol)
        book = state.last_book

        if book is None:
            return

        # Floor: minimum trade count in the burst window (prevents entry with 1-2 trades)
        if len(state.trade_window) < self.trade_count_trigger:
            return

        # Mid-price velocity — price must have moved meaningfully in the window
        if len(state.mid_history) < 2:
            return
        window_start_mid = state.mid_history[0][1]
        if window_start_mid == 0:
            return
        mid_move_bps = (book.mid_price - window_start_mid) / window_start_mid * 10000
        if abs(mid_move_bps) < self.move_bps_trigger:
            return

        # ── Gate 1: Relative intensity spike ───────────────────────────
        # Need at least 10 s of baseline before firing to avoid startup spikes.
        if len(state.baseline_window) < 2:
            return
        baseline_span_ms = state.baseline_window[-1][0] - state.baseline_window[0][0]
        if baseline_span_ms < 10_000:
            return

        notional_1s = sum(n for _, n in state.intensity_1s_window)
        avg_per_sec = sum(n for _, n in state.baseline_window) / (baseline_span_ms / 1000.0)
        if avg_per_sec == 0 or notional_1s < self.intensity_spike_mult * avg_per_sec:
            return
        # ───────────────────────────────────────────────────────────────

        # ── Gate 2: Volatility expansion ───────────────────────────────
        # Require enough history for sigma_slow to be meaningful.
        if state.sigma_slow < 1e-10:
            return
        if state.sigma_fast < self.vol_expansion_ratio * state.sigma_slow:
            return
        # ───────────────────────────────────────────────────────────────

        # Spread guard
        if book.spread_bps > self.max_spread_bps:
            return

        # ── Gate 4: Dual-trend crossover filter ────────────────────────
        # Crossover: 1-min EWMA vs 5-min EWMA determines momentum direction.
        #   fast > slow → uptrend  → LONG only
        #   fast < slow → downtrend → SHORT only
        # More responsive than price vs slow EWMA; filters intraday reversals.
        # Only enforce after the slow EWMA has had enough warmup to stabilize.
        if (state.trend_ewma > 0.0
                and now_ms - state.trend_ewma_start_ms >= self.trend_warmup_ms):
            is_uptrend = state.short_trend_ewma > state.trend_ewma
            if mid_move_bps > 0 and not is_uptrend:
                return  # bullish burst but 1-min EWMA is below 5-min — downtrend — skip
            if mid_move_bps < 0 and is_uptrend:
                return  # bearish burst but 1-min EWMA is above 5-min — uptrend — skip
        # ───────────────────────────────────────────────────────────────

        # ── Gate 3: Notional AFI ────────────────────────────────────────
        buy_notional = sum(n for _, n, is_buy in state.trade_window if is_buy)
        sell_notional = sum(n for _, n, is_buy in state.trade_window if not is_buy)
        total_notional = buy_notional + sell_notional
        if total_notional == 0:
            return
        afi = (buy_notional - sell_notional) / total_notional  # in [-1, +1]

        if mid_move_bps > 0 and afi >= self.afi_threshold:
            side = "BUY"
            fill = self.fill_model.fill_entry_long(book.ask_price, book.mid_price)
        elif mid_move_bps < 0 and afi <= -self.afi_threshold:
            side = "SELL"
            fill = self.fill_model.fill_entry_short(book.bid_price, book.mid_price)
        else:
            return
        # ───────────────────────────────────────────────────────────────

        qty = self.entry_qty.get(symbol, Decimal("0.001"))
        state.open_position = OpenPosition(
            side=side,
            entry_time_ms=now_ms,
            entry_price=fill.price,
            qty=qty,
            entry_mid=book.mid_price,
        )

    # ---------------------------------------------------------------- #
    # Exit logic                                                         #
    # ---------------------------------------------------------------- #

    def _check_exit(self, symbol: str, now_ms: int, book: BookTick) -> None:
        state = self._get_state(symbol)
        pos = state.open_position
        if pos is None:
            return

        hold_ms = now_ms - pos.entry_time_ms
        exit_reason: str | None = None
        exit_fill = None

        if pos.side == "BUY":
            pnl_bps = (book.bid_price - pos.entry_price) / pos.entry_mid * 10000
            pnl_f = float(pnl_bps)
            # Update trailing high watermark
            if pnl_f > pos.high_watermark_bps:
                pos.high_watermark_bps = pnl_f
            # Trailing stop: once peak >= trigger, stop trails trail_bps below the peak
            if pos.high_watermark_bps >= self.trail_trigger_bps:
                is_stopped = pnl_f <= pos.high_watermark_bps - self.trail_bps
            else:
                is_stopped = pnl_bps <= -self.stop_loss_bps
            if pnl_bps >= self.take_profit_bps:
                exit_reason = "take_profit"
            elif is_stopped:
                exit_reason = "stop_loss"
            elif hold_ms >= self.max_hold_ms:
                exit_reason = "timeout"
            if exit_reason:
                exit_fill = self.fill_model.fill_exit_long(book.bid_price, book.mid_price)
                gross_pnl_bps = (exit_fill.price - pos.entry_price) / pos.entry_mid * 10000
                gross_pnl_usd = (exit_fill.price - pos.entry_price) * pos.qty
        else:  # SELL
            pnl_bps = (pos.entry_price - book.ask_price) / pos.entry_mid * 10000
            pnl_f = float(pnl_bps)
            if pnl_f > pos.high_watermark_bps:
                pos.high_watermark_bps = pnl_f
            if pos.high_watermark_bps >= self.trail_trigger_bps:
                is_stopped = pnl_f <= pos.high_watermark_bps - self.trail_bps
            else:
                is_stopped = pnl_bps <= -self.stop_loss_bps
            if pnl_bps >= self.take_profit_bps:
                exit_reason = "take_profit"
            elif is_stopped:
                exit_reason = "stop_loss"
            elif hold_ms >= self.max_hold_ms:
                exit_reason = "timeout"
            if exit_reason:
                exit_fill = self.fill_model.fill_exit_short(book.ask_price, book.mid_price)
                gross_pnl_bps = (pos.entry_price - exit_fill.price) / pos.entry_mid * 10000
                gross_pnl_usd = (pos.entry_price - exit_fill.price) * pos.qty

        if exit_reason is None or exit_fill is None:
            return

        # Fees on combined entry + exit notional
        entry_notional = pos.entry_price * pos.qty
        exit_notional = exit_fill.price * pos.qty
        fees_usd = (entry_notional + exit_notional) * exit_fill.fee_bps / Decimal("10000")
        net_pnl_usd = gross_pnl_usd - fees_usd

        self.trades.append(
            BacktestTrade(
                symbol=symbol,
                side=pos.side,
                entry_time_ms=pos.entry_time_ms,
                entry_price=pos.entry_price,
                exit_time_ms=now_ms,
                exit_price=exit_fill.price,
                qty=pos.qty,
                exit_reason=exit_reason,
                hold_ms=hold_ms,
                gross_pnl_usd=gross_pnl_usd,
                gross_pnl_bps=gross_pnl_bps,
                fees_usd=fees_usd,
                net_pnl_usd=net_pnl_usd,
            )
        )

        state.open_position = None
        state.cooldown_until_ms = now_ms + self.cooldown_ms

    def _get_state(self, symbol: str) -> SymbolState:
        if symbol not in self._states:
            self._states[symbol] = SymbolState()
        return self._states[symbol]
