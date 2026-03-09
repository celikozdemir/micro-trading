"""
Feature extractor for ML signal scoring.

Maintains rolling state from tick data and produces a feature vector
suitable for XGBoost inference. Used both by:
  - Offline training pipeline (replay historical ticks)
  - Live inference (called from the strategy hot path)

Feature vector (13 features):
  0  afi                   — aggressive flow imbalance (-1 to +1), 250ms window
  1  obi                   — order book imbalance (-1 to +1), best bid/ask qty
  2  intensity_ratio       — 1s notional / 60s avg-per-sec
  3  vol_expansion         — sigma_fast / sigma_slow EWMA ratio
  4  mid_move_bps          — mid-price displacement over 250ms burst window
  5  spread_bps            — current bid-ask spread in basis points
  6  book_depth_ratio      — bid_qty / (bid_qty + ask_qty) at best level
  7  trade_imbalance_1s    — (buy_notional - sell_notional) / total in 1s
  8  trade_imbalance_5s    — (buy_notional - sell_notional) / total in 5s
  9  vwap_deviation_bps    — mid vs 1-min VWAP deviation in bps
  10 time_of_day_sin       — sin(2π × hour/24)
  11 time_of_day_cos       — cos(2π × hour/24)
  12 realized_vol_regime   — rolling 5-min realized vol (avg abs return bps)
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

import numpy as np

FEATURE_NAMES = [
    "afi", "obi", "intensity_ratio", "vol_expansion", "mid_move_bps",
    "spread_bps", "book_depth_ratio", "trade_imbalance_1s", "trade_imbalance_5s",
    "vwap_deviation_bps", "time_of_day_sin", "time_of_day_cos", "realized_vol_regime",
]

NUM_FEATURES = len(FEATURE_NAMES)


@dataclass
class _SymbolFeatureState:
    # 250ms burst window: (ts_ms, notional, is_buy_aggressor)
    trade_window_250: deque = field(default_factory=deque)
    # 1s trade window
    trade_window_1s: deque = field(default_factory=deque)
    # 5s trade window
    trade_window_5s: deque = field(default_factory=deque)
    # 60s baseline window: (ts_ms, notional)
    baseline_60s: deque = field(default_factory=deque)
    # 1-min VWAP: (ts_ms, price, qty)
    vwap_window: deque = field(default_factory=deque)
    # Mid-price history for burst displacement
    mid_history: deque = field(default_factory=deque)
    # Vol history for 5-min realized vol
    vol_history: deque = field(default_factory=deque)

    # EWMA state
    sigma_fast: float = 0.0
    sigma_slow: float = 0.0
    last_ewma_mid: float = 0.0
    last_ewma_ms: int = 0

    # Latest book state
    bid_price: float = 0.0
    ask_price: float = 0.0
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    mid_price: float = 0.0
    spread_bps: float = 0.0


class FeatureExtractor:
    """
    Stateful feature extractor. Feed ticks in time order via on_book_tick()
    and on_agg_trade(), then call extract() to get the current feature vector.
    """

    def __init__(
        self,
        sigma_fast_halflife_ms: float = 500.0,
        sigma_slow_halflife_ms: float = 45_000.0,
    ):
        self._fast_hl = sigma_fast_halflife_ms
        self._slow_hl = sigma_slow_halflife_ms
        self._states: dict[str, _SymbolFeatureState] = {}

    def _get(self, symbol: str) -> _SymbolFeatureState:
        if symbol not in self._states:
            self._states[symbol] = _SymbolFeatureState()
        return self._states[symbol]

    def on_book_tick(
        self, symbol: str, ts_ms: int,
        bid_price: float, bid_qty: float,
        ask_price: float, ask_qty: float,
    ) -> None:
        s = self._get(symbol)
        s.bid_price = bid_price
        s.ask_price = ask_price
        s.bid_qty = bid_qty
        s.ask_qty = ask_qty
        mid = (bid_price + ask_price) / 2.0
        s.mid_price = mid
        s.spread_bps = (ask_price - bid_price) / mid * 10_000 if mid > 0 else 0.0

        s.mid_history.append((ts_ms, mid))
        cutoff = ts_ms - 250
        while s.mid_history and s.mid_history[0][0] < cutoff:
            s.mid_history.popleft()

        self._update_ewma(s, mid, ts_ms)

    def on_agg_trade(
        self, symbol: str, ts_ms: int,
        price: float, qty: float, is_buy_aggressor: bool,
    ) -> None:
        s = self._get(symbol)
        notional = price * qty

        # 250ms window
        s.trade_window_250.append((ts_ms, notional, is_buy_aggressor))
        c250 = ts_ms - 250
        while s.trade_window_250 and s.trade_window_250[0][0] < c250:
            s.trade_window_250.popleft()

        # 1s window
        s.trade_window_1s.append((ts_ms, notional, is_buy_aggressor))
        c1 = ts_ms - 1_000
        while s.trade_window_1s and s.trade_window_1s[0][0] < c1:
            s.trade_window_1s.popleft()

        # 5s window
        s.trade_window_5s.append((ts_ms, notional, is_buy_aggressor))
        c5 = ts_ms - 5_000
        while s.trade_window_5s and s.trade_window_5s[0][0] < c5:
            s.trade_window_5s.popleft()

        # 60s baseline
        s.baseline_60s.append((ts_ms, notional))
        c60 = ts_ms - 60_000
        while s.baseline_60s and s.baseline_60s[0][0] < c60:
            s.baseline_60s.popleft()

        # 1-min VWAP
        s.vwap_window.append((ts_ms, price, qty))
        while s.vwap_window and s.vwap_window[0][0] < c60:
            s.vwap_window.popleft()

        # Mid history (use trade price as proxy between book updates)
        s.mid_history.append((ts_ms, price))
        c250 = ts_ms - 250
        while s.mid_history and s.mid_history[0][0] < c250:
            s.mid_history.popleft()

        self._update_ewma(s, price, ts_ms)

    def _update_ewma(self, s: _SymbolFeatureState, mid: float, ts_ms: int) -> None:
        if s.last_ewma_ms == 0:
            s.last_ewma_mid = mid
            s.last_ewma_ms = ts_ms
            return
        dt = ts_ms - s.last_ewma_ms
        if dt <= 0:
            return

        ret = abs(mid - s.last_ewma_mid) / s.last_ewma_mid if s.last_ewma_mid > 0 else 0.0
        alpha_f = 1.0 - math.exp(-dt / self._fast_hl)
        alpha_s = 1.0 - math.exp(-dt / self._slow_hl)
        s.sigma_fast = alpha_f * ret + (1.0 - alpha_f) * s.sigma_fast
        s.sigma_slow = alpha_s * ret + (1.0 - alpha_s) * s.sigma_slow

        # 5-min realized vol
        abs_ret_bps = abs(ret * 10_000)
        s.vol_history.append((ts_ms, abs_ret_bps))
        c5m = ts_ms - 300_000
        while s.vol_history and s.vol_history[0][0] < c5m:
            s.vol_history.popleft()

        s.last_ewma_mid = mid
        s.last_ewma_ms = ts_ms

    def extract(self, symbol: str, ts_ms: int) -> Optional[np.ndarray]:
        """
        Extract feature vector for the given symbol at the current moment.
        Returns None if insufficient data (cold start).
        """
        s = self._states.get(symbol)
        if s is None or s.mid_price == 0 or s.last_ewma_ms == 0:
            return None

        # AFI (250ms)
        buy_n = sum(n for _, n, b in s.trade_window_250 if b)
        sell_n = sum(n for _, n, b in s.trade_window_250 if not b)
        total = buy_n + sell_n
        afi = (buy_n - sell_n) / total if total > 0 else 0.0

        # OBI (best level)
        total_qty = s.bid_qty + s.ask_qty
        obi = (s.bid_qty - s.ask_qty) / total_qty if total_qty > 0 else 0.0

        # Intensity ratio
        if len(s.baseline_60s) >= 2:
            span_ms = s.baseline_60s[-1][0] - s.baseline_60s[0][0]
            if span_ms > 5_000:
                avg_per_sec = sum(n for _, n in s.baseline_60s) / (span_ms / 1_000)
                notional_1s = sum(n for _, n in s.trade_window_1s)
                intensity_ratio = notional_1s / avg_per_sec if avg_per_sec > 0 else 0.0
            else:
                intensity_ratio = 0.0
        else:
            intensity_ratio = 0.0

        # Vol expansion
        vol_expansion = s.sigma_fast / s.sigma_slow if s.sigma_slow > 1e-12 else 0.0

        # Mid move bps (250ms window)
        if len(s.mid_history) >= 2:
            start_mid = s.mid_history[0][1]
            mid_move_bps = (s.mid_price - start_mid) / start_mid * 10_000 if start_mid > 0 else 0.0
        else:
            mid_move_bps = 0.0

        # Spread bps
        spread_bps = s.spread_bps

        # Book depth ratio (best level)
        book_depth_ratio = s.bid_qty / total_qty if total_qty > 0 else 0.5

        # Trade imbalance 1s
        buy_1s = sum(n for _, n, b in s.trade_window_1s if b)
        sell_1s = sum(n for _, n, b in s.trade_window_1s if not b)
        tot_1s = buy_1s + sell_1s
        trade_imbalance_1s = (buy_1s - sell_1s) / tot_1s if tot_1s > 0 else 0.0

        # Trade imbalance 5s
        buy_5s = sum(n for _, n, b in s.trade_window_5s if b)
        sell_5s = sum(n for _, n, b in s.trade_window_5s if not b)
        tot_5s = buy_5s + sell_5s
        trade_imbalance_5s = (buy_5s - sell_5s) / tot_5s if tot_5s > 0 else 0.0

        # VWAP deviation
        if len(s.vwap_window) > 0:
            total_pq = sum(p * q for _, p, q in s.vwap_window)
            total_q = sum(q for _, _, q in s.vwap_window)
            vwap = total_pq / total_q if total_q > 0 else s.mid_price
            vwap_deviation_bps = (s.mid_price - vwap) / vwap * 10_000 if vwap > 0 else 0.0
        else:
            vwap_deviation_bps = 0.0

        # Time of day encoding
        hour_frac = (ts_ms / 1_000 / 3600) % 24
        time_sin = math.sin(2.0 * math.pi * hour_frac / 24.0)
        time_cos = math.cos(2.0 * math.pi * hour_frac / 24.0)

        # Realized vol regime (5-min avg abs return in bps)
        if len(s.vol_history) > 10:
            realized_vol = sum(v for _, v in s.vol_history) / len(s.vol_history)
        else:
            realized_vol = 0.0

        return np.array([
            afi, obi, intensity_ratio, vol_expansion, mid_move_bps,
            spread_bps, book_depth_ratio, trade_imbalance_1s, trade_imbalance_5s,
            vwap_deviation_bps, time_sin, time_cos, realized_vol,
        ], dtype=np.float32)
