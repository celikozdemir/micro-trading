"""
Strategy A+: Advanced Burst Momentum

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
from backend.core.data.normalizer import AggTrade, BookTick, MarkPrice
from backend.core.ml.features import FeatureExtractor
from backend.core.ml.scorer import MLScorer


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
    
    # ── Macro Regime Filter ──────────────────────────────────────────
    macro_trend_ewma: float = 0.0    # slow EWMA of mid-price (e.g. 15-min)
    macro_trend_ewma_prev: float = 0.0 # for slope detection
    macro_trend_start_ms: int = 0    # warm-up check for macro trend
    # ─────────────────────────────────────────────────────────────────
    
    # ── Volatility Regime Detection ──────────────────────────────────
    vol_history: deque = field(default_factory=deque)  # (ts_ms, abs_return)
    realized_vol_5m: float = 0.0   # rolling 5-min realized volatility (bps)
    # ─────────────────────────────────────────────────────────────────

    # ── Funding Rate ─────────────────────────────────────────────────
    funding_rate: float = 0.0         # latest funding rate from markPrice
    next_funding_time_ms: int = 0     # next funding settlement time
    # ─────────────────────────────────────────────────────────────────

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


class AdvancedMomentumStrategy:
    """
    Stateful advanced burst momentum strategy.
    Call on_event() with each BookTick or AggTrade in time order.
    Completed trades accumulate in self.trades.
    """

    def __init__(self, config: dict, fill_model: FillModel, primary_symbol: str = "BTCUSDT",
                 ml_scorer: MLScorer | None = None):
        s = config["strategy"]

        # ── Primary Symbol (for cross-asset correlation) ───────────────
        self.primary_symbol = primary_symbol

        # ── ML Signal Scoring ──────────────────────────────────────────
        ml_cfg = s.get("ml", {})
        self.ml_enabled: bool = ml_cfg.get("enabled", False)
        self.ml_threshold: float = ml_cfg.get("threshold", 0.55)
        self._ml_scorer = ml_scorer or (MLScorer(ml_cfg.get("model_dir", "models")) if self.ml_enabled else None)
        self._feature_extractor = FeatureExtractor(
            sigma_fast_halflife_ms=float(s.get("sigma_fast_halflife_ms", 500)),
            sigma_slow_halflife_ms=float(s.get("sigma_slow_halflife_ms", 45000)),
        ) if self.ml_enabled else None
        # ───────────────────────────────────────────────────────────────

        # Determine which symbols we care about
        symbols = config.get("symbols", ["BTCUSDT", "ETHUSDT"])
        
        # ── Parameter resolution (supports per-symbol overrides) ────────
        self.sym_params: dict[str, dict] = {}
        overrides = s.get("symbol_overrides", {})
        ex = s.get("exit", {})
        
        for sym in symbols:
            sym_s = {**s, **overrides.get(sym, {})}  # Override base with symbol-specific
            sym_ex = {**ex, **overrides.get(sym, {}).get("exit", {})} # Override exit 
            
            self.sym_params[sym] = {
                "window_ms": int(sym_s.get("window_ms", 250)),
                "trade_count_trigger": int(sym_s.get("trade_count_trigger", 5)),
                "move_bps_trigger": float(sym_s.get("move_bps_trigger", 0.5)),
                
                "take_profit_bps": float(sym_ex.get("take_profit_bps", 10.0)),
                "stop_loss_bps": float(sym_ex.get("stop_loss_bps", 5.0)),
                "max_hold_ms": int(sym_ex.get("max_hold_ms", 30000)),
                "trail_trigger_bps": float(sym_ex.get("trail_trigger_bps", 4.0)),
                "trail_bps": float(sym_ex.get("trail_bps", 2.0)),
                
                "intensity_spike_mult": float(sym_s.get("intensity_spike_mult", 5.0)),
                "vol_expansion_ratio": float(sym_s.get("vol_expansion_ratio", 2.5)),
                "trend_halflife_ms": float(sym_s.get("trend_halflife_ms", 300_000)),
                "short_trend_halflife_ms": float(sym_s.get("short_trend_halflife_ms", 60_000)),
                "trend_warmup_ms": int(sym_s.get("trend_warmup_ms", 300_000)),
                
                "afi_threshold": float(sym_s.get("afi_threshold", 0.4)),
                "obi_threshold": float(sym_s.get("obi_threshold", 0.2)),
                "short_only": bool(sym_s.get("short_only", True)),
                "adaptive_vol_multiplier": float(sym_s.get("adaptive_vol_multiplier", 0.0)),
                
                "macro_trend_halflife_ms": float(sym_s.get("macro_trend_halflife_ms", 900_000)),
                "macro_trend_warmup_ms": int(sym_s.get("macro_trend_warmup_ms", 600_000)),
                "funding_rate_filter": bool(sym_s.get("funding_rate_filter", True)),
            }
        
        self.cooldown_ms: int = s.get("cooldown_ms", 2000)
        self.max_spread_bps: Decimal = Decimal(str(config["risk"]["max_spread_bps"]))

        # entry_qty — parsed from strategy.entry_qty in config (keyed by symbol)
        self.entry_qty: dict[str, Decimal] = {
            sym: Decimal(str(qty)) for sym, qty in s.get("entry_qty", {}).items()
        }
        
        self.sigma_fast_halflife_ms: float = float(s.get("sigma_fast_halflife_ms", 1500))
        self.sigma_slow_halflife_ms: float = float(s.get("sigma_slow_halflife_ms", 45000))
        # ───────────────────────────────────────────────────────────────

        self.fill_model = fill_model
        self._states: dict[str, SymbolState] = {}
        self.trades: list[BacktestTrade] = []

    # ---------------------------------------------------------------- #
    # Public interface                                                   #
    # ---------------------------------------------------------------- #

    def on_event(self, event: BookTick | AggTrade | MarkPrice) -> None:
        if isinstance(event, BookTick):
            self._on_book_tick(event)
        elif isinstance(event, AggTrade):
            self._on_agg_trade(event)
        elif isinstance(event, MarkPrice):
            self._on_mark_price(event)

    # ---------------------------------------------------------------- #
    # Event handlers                                                     #
    # ---------------------------------------------------------------- #

    def _on_mark_price(self, mp: MarkPrice) -> None:
        state = self._get_state(mp.symbol)
        state.funding_rate = float(mp.funding_rate)
        state.next_funding_time_ms = mp.next_funding_time_ms

    def _on_book_tick(self, bt: BookTick) -> None:
        state = self._get_state(bt.symbol)
        state.last_book = bt

        if self._feature_extractor is not None:
            self._feature_extractor.on_book_tick(
                bt.symbol, bt.timestamp_exchange_ms,
                float(bt.bid_price), float(bt.bid_qty),
                float(bt.ask_price), float(bt.ask_qty),
            )

        # Update EWMA vol with actual mid price (most accurate)
        self._update_ewma(state, float(bt.mid_price), bt.timestamp_exchange_ms, bt.symbol)

        # Track mid-price history for velocity calculation
        state.mid_history.append((bt.timestamp_exchange_ms, bt.mid_price))
        p = self.sym_params.get(bt.symbol)
        cutoff = bt.timestamp_exchange_ms - (p["window_ms"] if p else 250)
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

        if self._feature_extractor is not None:
            self._feature_extractor.on_agg_trade(
                at.symbol, now_ms,
                float(at.price), float(at.qty), is_buy_aggressor,
            )

        p = self.sym_params.get(at.symbol)
        if not p:
            return
            
        # 250 ms window — stores notional (not raw qty) for AFI
        state.trade_window.append((now_ms, notional, is_buy_aggressor))
        cutoff_burst = now_ms - p["window_ms"]
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
        self._update_ewma(state, float(at.price), now_ms, at.symbol)

        # Keep mid_history alive between book ticks (for velocity)
        state.mid_history.append((now_ms, at.price))
        while state.mid_history and state.mid_history[0][0] < cutoff_burst:
            state.mid_history.popleft()

        # Timeout check — fire via agg_trade when book ticks are sparse
        if state.open_position is not None and state.last_book is not None:
            hold_ms = now_ms - state.open_position.entry_time_ms
            if hold_ms >= p["max_hold_ms"]:
                self._check_exit(at.symbol, now_ms, state.last_book)
                return

        if state.open_position is None and now_ms > state.cooldown_until_ms:
            self._check_entry(at.symbol, now_ms)

    # ---------------------------------------------------------------- #
    # EWMA volatility updater (hot path — floats only)                  #
    # ---------------------------------------------------------------- #

    def _update_ewma(self, state: SymbolState, mid: float, now_ms: int, symbol: str = "") -> None:
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

        p = self.sym_params.get(symbol) or self.sym_params.get(next(iter(self.sym_params), ""), {})
        trend_halflife = p.get("trend_halflife_ms", 300_000)
        short_trend_halflife = p.get("short_trend_halflife_ms", 60_000)
        macro_halflife = p.get("macro_trend_halflife_ms", 900_000)
        
        alpha_trend = 1.0 - math.exp(-dt_ms / trend_halflife)
        alpha_short_trend = 1.0 - math.exp(-dt_ms / short_trend_halflife)
        alpha_macro = 1.0 - math.exp(-dt_ms / macro_halflife)

        state.sigma_fast = alpha_fast * ret + (1.0 - alpha_fast) * state.sigma_fast
        state.sigma_slow = alpha_slow * ret + (1.0 - alpha_slow) * state.sigma_slow

        # Trend EWMAs track price level (not returns) for crossover gate
        if state.trend_ewma == 0.0:
            state.trend_ewma = mid
            state.short_trend_ewma = mid
            state.macro_trend_ewma = mid
            state.macro_trend_ewma_prev = mid
            state.trend_ewma_start_ms = now_ms
            state.macro_trend_start_ms = now_ms
        else:
            state.trend_ewma = alpha_trend * mid + (1.0 - alpha_trend) * state.trend_ewma
            state.short_trend_ewma = alpha_short_trend * mid + (1.0 - alpha_short_trend) * state.short_trend_ewma
            
            # Store prev for slope before updating
            state.macro_trend_ewma_prev = state.macro_trend_ewma
            state.macro_trend_ewma = alpha_macro * mid + (1.0 - alpha_macro) * state.macro_trend_ewma

        state.last_ewma_mid = mid
        state.last_ewma_ms = now_ms
        
        # ── 5-min Realized Volatility Tracker ───────────────────────────
        abs_ret_bps = abs(ret * 10000)
        state.vol_history.append((now_ms, abs_ret_bps))
        cutoff_5m = now_ms - 300_000
        while state.vol_history and state.vol_history[0][0] < cutoff_5m:
            state.vol_history.popleft()
        if len(state.vol_history) > 10:
            state.realized_vol_5m = sum(v for _, v in state.vol_history) / len(state.vol_history)
        # ───────────────────────────────────────────────────────────────

    # ---------------------------------------------------------------- #
    # Entry logic — all gates must pass                                  #
    # ---------------------------------------------------------------- #

    def _check_entry(self, symbol: str, now_ms: int) -> None:
        state = self._get_state(symbol)
        book = state.last_book

        p = self.sym_params.get(symbol)
        if not p:
            return

        # Floor: minimum trade count in the burst window (prevents entry with 1-2 trades)
        if len(state.trade_window) < p["trade_count_trigger"]:
            return

        # Mid-price velocity — price must have moved meaningfully in the window
        if len(state.mid_history) < 2:
            return
        window_start_mid = state.mid_history[0][1]
        if window_start_mid == 0:
            return
        mid_move_bps = (book.mid_price - window_start_mid) / window_start_mid * 10000
        if abs(mid_move_bps) < p["move_bps_trigger"]:
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
        if avg_per_sec == 0 or notional_1s < p["intensity_spike_mult"] * avg_per_sec:
            return
        # ───────────────────────────────────────────────────────────────

        # ── Gate 2: Volatility expansion ───────────────────────────────
        # Require enough history for sigma_slow to be meaningful.
        if state.sigma_slow < 1e-10:
            return
        if state.sigma_fast < p["vol_expansion_ratio"] * state.sigma_slow:
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
                and now_ms - state.trend_ewma_start_ms >= p["trend_warmup_ms"]):
            is_uptrend = state.short_trend_ewma > state.trend_ewma
            if mid_move_bps > 0 and not is_uptrend:
                return  # bullish burst but 1-min EWMA is below 5-min — downtrend — skip
            if mid_move_bps < 0 and is_uptrend:
                return  # bearish burst but 1-min EWMA is above 5-min — uptrend — skip
        # ───────────────────────────────────────────────────────────────

        # ── Gate 6: Macro Trend Alignment ───────────────────────────────
        # Only enter in the direction of the macro trend.
        # For SHORTS: price < macro EMA and macro EMA declining.
        # For LONGS:  price > macro EMA and macro EMA rising.
        if state.macro_trend_ewma > 0.0:
            if now_ms - state.macro_trend_start_ms >= p["macro_trend_warmup_ms"]:
                mid_price = float(book.mid_price)
                macro_rising = state.macro_trend_ewma > state.macro_trend_ewma_prev
                macro_falling = state.macro_trend_ewma < state.macro_trend_ewma_prev
                
                if mid_move_bps > 0:  # Bullish burst → wants to go LONG
                    if mid_price < state.macro_trend_ewma or not macro_rising:
                        return  # Don't go long below a declining macro trend
                elif mid_move_bps < 0:  # Bearish burst → wants to go SHORT
                    if mid_price > state.macro_trend_ewma or not macro_falling:
                        return  # Don't short above a rising macro trend
        # ───────────────────────────────────────────────────────────────

        # ── Gate 3: Notional AFI ────────────────────────────────────────
        buy_notional = sum(n for _, n, is_buy in state.trade_window if is_buy)
        sell_notional = sum(n for _, n, is_buy in state.trade_window if not is_buy)
        total_notional = buy_notional + sell_notional
        if total_notional == 0:
            return
        afi = (buy_notional - sell_notional) / total_notional  # in [-1, +1]

        # ── Gate 5: Order Book Imbalance (OBI) ─────────────────────────
        total_qty = book.bid_qty + book.ask_qty
        obi = float((book.bid_qty - book.ask_qty) / total_qty) if total_qty > 0 else 0.0
        # ───────────────────────────────────────────────────────────────

        # ── Multi-Symbol Correlation Check ─────────────────────────────
        if symbol != self.primary_symbol:
            primary_state = self._get_state(self.primary_symbol)
            p_primary = self.sym_params.get(self.primary_symbol, p)
            if primary_state.trend_ewma > 0.0 and now_ms - primary_state.trend_ewma_start_ms >= p_primary["trend_warmup_ms"]:
                primary_is_uptrend = primary_state.short_trend_ewma > primary_state.trend_ewma
                if mid_move_bps > 0 and not primary_is_uptrend:
                    return # Primary is bearish, ignore long altcoin burst
                if mid_move_bps < 0 and primary_is_uptrend:
                    return # Primary is bullish, ignore short altcoin burst
        # ───────────────────────────────────────────────────────────────

        # ── Funding Rate Filter ──────────────────────────────────────────
        # When enabled, prefer the side that collects funding:
        #   funding > 0 → longs pay shorts → favor SHORT
        #   funding < 0 → shorts pay longs → favor LONG
        # Don't block trades entirely — only filter when funding is meaningfully skewed.
        funding = state.funding_rate
        if p["funding_rate_filter"] and abs(funding) > 0.0001:
            if mid_move_bps > 0 and funding > 0.0003:
                return  # Strong positive funding — don't go long (paying high rate)
            if mid_move_bps < 0 and funding < -0.0003:
                return  # Strong negative funding — don't go short (paying high rate)
        # ───────────────────────────────────────────────────────────────

        if mid_move_bps > 0 and afi >= p["afi_threshold"] and obi >= p["obi_threshold"]:
            if p["short_only"]:
                return  # Skip longs if short_only mode
            side = "BUY"
            direction = "long"
            fill = self.fill_model.fill_entry_long(book.ask_price, book.mid_price)
        elif mid_move_bps < 0 and afi <= -p["afi_threshold"] and obi <= -p["obi_threshold"]:
            side = "SELL"
            direction = "short"
            fill = self.fill_model.fill_entry_short(book.bid_price, book.mid_price)
        else:
            return
        # ───────────────────────────────────────────────────────────────

        # ── ML Confidence Gate ────────────────────────────────────────
        # If ML scoring is enabled and models are loaded, require
        # P(profitable) > threshold. Graceful fallback if no model.
        if self.ml_enabled and self._ml_scorer is not None and self._feature_extractor is not None:
            features = self._feature_extractor.extract(symbol, now_ms)
            if features is not None:
                should_enter, confidence = self._ml_scorer.should_enter(features, direction)
                if not should_enter:
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

    def _get_regime_params(self, symbol: str, state: 'SymbolState') -> tuple[float, float, int]:
        """Return (take_profit_bps, stop_loss_bps, max_hold_ms) based on volatility regime."""
        p = self.sym_params.get(symbol, {})
        vol = state.realized_vol_5m
        
        low_vol_threshold = 0.3
        high_vol_threshold = 1.5
        
        if vol < low_vol_threshold:
            return (8.0, 5.0, 45_000)     # 45s hold, tight scalp
        elif vol < high_vol_threshold:
            return (15.0, 8.0, 90_000)    # 90s hold, moderate
        else:
            return (30.0, 15.0, 180_000)  # 3 min hold, wide

    def _check_exit(self, symbol: str, now_ms: int, book: BookTick) -> None:
        state = self._get_state(symbol)
        pos = state.open_position
        if pos is None:
            return

        p = self.sym_params.get(symbol)
        if not p: return
        
        # ── Regime-Adaptive Parameters ───────────────────────────────────
        regime_tp, regime_sl, regime_hold = self._get_regime_params(symbol, state)
        # ───────────────────────────────────────────────────────────────
        
        hold_ms = now_ms - pos.entry_time_ms
        exit_reason: str | None = None
        exit_fill = None

        # Adaptive Thresholds
        if p["adaptive_vol_multiplier"] > 0 and state.sigma_slow > 0:
            # Scale TP/SL based on current volatility regime
            vol_adjustment = Decimal(str(max(0.5, min(3.0, state.sigma_slow * p["adaptive_vol_multiplier"]))))
            dynamic_tp = Decimal(str(regime_tp)) * vol_adjustment
            dynamic_sl = Decimal(str(regime_sl)) * vol_adjustment
        else:
            dynamic_tp = Decimal(str(regime_tp))
            dynamic_sl = Decimal(str(regime_sl))

        if pos.side == "BUY":
            pnl_bps = (book.bid_price - pos.entry_price) / pos.entry_mid * 10000
            pnl_f = float(pnl_bps)
            # Update trailing high watermark
            if pnl_f > pos.high_watermark_bps:
                pos.high_watermark_bps = pnl_f
            trail_trigger = regime_tp * 0.4
            trail_distance = regime_tp * 0.25
            if pos.high_watermark_bps >= trail_trigger:
                is_stopped = pnl_f <= pos.high_watermark_bps - trail_distance
            else:
                is_stopped = pnl_bps <= -dynamic_sl
            if pnl_bps >= dynamic_tp:
                exit_reason = "take_profit"
            elif is_stopped:
                exit_reason = "stop_loss"
            elif hold_ms >= regime_hold:
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
            trail_trigger = regime_tp * 0.4
            trail_distance = regime_tp * 0.25
            if pos.high_watermark_bps >= trail_trigger:
                is_stopped = pnl_f <= pos.high_watermark_bps - trail_distance
            else:
                is_stopped = pnl_bps <= -dynamic_sl
            if pnl_bps >= dynamic_tp:
                exit_reason = "take_profit"
            elif is_stopped:
                exit_reason = "stop_loss"
            elif hold_ms >= regime_hold:
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
