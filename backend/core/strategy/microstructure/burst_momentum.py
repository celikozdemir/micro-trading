"""
Strategy A: Burst Momentum Catch

Detects sudden spikes in aggressive trade flow combined with mid-price
velocity, enters in the direction of the burst, exits quickly via
take-profit, stop-loss, or timeout.

From the config:
  window_ms:            rolling window for trade counting
  trade_count_trigger:  min trades in window to consider entry
  move_bps_trigger:     min mid-price move (bps) in window to confirm burst
  exit.take_profit_bps: close at this gain
  exit.stop_loss_bps:   close at this loss
  exit.max_hold_ms:     force-close after this duration
  cooldown_ms:          pause after each trade
"""

from __future__ import annotations

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


@dataclass
class SymbolState:
    # Rolling window entries: (timestamp_ms, qty, is_buy_aggressor)
    trade_window: deque = field(default_factory=deque)
    # Mid-price history for velocity: (timestamp_ms, mid_price)
    mid_history: deque = field(default_factory=deque)
    # Regime intensity window: timestamps of agg_trades in the last N seconds
    intensity_window: deque = field(default_factory=deque)
    # Most recent book tick
    last_book: Optional[BookTick] = None
    # Cooldown expiry
    cooldown_until_ms: int = 0
    # Current open position
    open_position: Optional[OpenPosition] = None


# ------------------------------------------------------------------ #
# Trade record (output of the simulation)                              #
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
        self.window_ms: int = s["window_ms"]
        self.trade_count_trigger: int = s["trade_count_trigger"]
        self.move_bps_trigger: Decimal = Decimal(str(s["move_bps_trigger"]))
        self.entry_qty: dict[str, Decimal] = {
            sym: Decimal(str(qty)) for sym, qty in s["entry_qty"].items()
        }
        ex = s["exit"]
        self.take_profit_bps: Decimal = Decimal(str(ex["take_profit_bps"]))
        self.stop_loss_bps: Decimal = Decimal(str(ex["stop_loss_bps"]))
        self.max_hold_ms: int = ex["max_hold_ms"]
        self.cooldown_ms: int = s["cooldown_ms"]
        self.max_spread_bps: Decimal = Decimal(str(config["risk"]["max_spread_bps"]))
        # Regime / intensity gate: only trade when market is hot.
        # intensity_filter_window_ms: how far back to count trades (default 10s)
        # intensity_filter_trades:    min trades in that window to allow entry (0 = disabled)
        self.intensity_filter_window_ms: int = s.get("intensity_filter_window_ms", 10_000)
        self.intensity_filter_trades: int = s.get("intensity_filter_trades", 0)
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

        # is_buyer_maker=True means sell aggressor; False means buy aggressor
        is_buy_aggressor = not at.is_buyer_maker
        state.trade_window.append((now_ms, at.qty, is_buy_aggressor))

        cutoff = now_ms - self.window_ms
        while state.trade_window and state.trade_window[0][0] < cutoff:
            state.trade_window.popleft()

        # Maintain the longer-horizon intensity window for regime filtering
        state.intensity_window.append(now_ms)
        intensity_cutoff = now_ms - self.intensity_filter_window_ms
        while state.intensity_window and state.intensity_window[0] < intensity_cutoff:
            state.intensity_window.popleft()

        # Keep mid_history alive using trade price as proxy when book_ticks are sparse.
        # In production book_ticks and agg_trades interleave at high frequency; in
        # backtesting with a book_tick cap this keeps velocity detection working.
        state.mid_history.append((now_ms, at.price))
        while state.mid_history and state.mid_history[0][0] < cutoff:
            state.mid_history.popleft()

        # Timeout is time-based — fire it on agg_trade events when no book_tick arrives.
        # Uses last known book_tick for exit price (stale is acceptable for timeout exits).
        if state.open_position is not None and state.last_book is not None:
            hold_ms = now_ms - state.open_position.entry_time_ms
            if hold_ms >= self.max_hold_ms:
                self._check_exit(at.symbol, now_ms, state.last_book)
                return

        if state.open_position is None and now_ms > state.cooldown_until_ms:
            self._check_entry(at.symbol, now_ms)

    # ---------------------------------------------------------------- #
    # Entry logic                                                        #
    # ---------------------------------------------------------------- #

    def _check_entry(self, symbol: str, now_ms: int) -> None:
        state = self._get_state(symbol)
        book = state.last_book

        if book is None:
            return

        # Minimum trade activity in window
        if len(state.trade_window) < self.trade_count_trigger:
            return

        # Need mid-price history to compute velocity
        if len(state.mid_history) < 2:
            return

        window_start_mid = state.mid_history[0][1]
        if window_start_mid == 0:
            return

        mid_move_bps = (book.mid_price - window_start_mid) / window_start_mid * 10000
        if abs(mid_move_bps) < self.move_bps_trigger:
            return

        # Regime intensity gate: only trade when market is hot
        if self.intensity_filter_trades > 0 and len(state.intensity_window) < self.intensity_filter_trades:
            return

        # Skip if spread is too wide
        if book.spread_bps > self.max_spread_bps:
            return

        # Confirm direction with aggressor flow
        buy_qty = sum(q for _, q, is_buy in state.trade_window if is_buy)
        sell_qty = sum(q for _, q, is_buy in state.trade_window if not is_buy)

        if mid_move_bps > 0 and buy_qty > sell_qty:
            side = "BUY"
            fill = self.fill_model.fill_entry_long(book.ask_price, book.mid_price)
        elif mid_move_bps < 0 and sell_qty > buy_qty:
            side = "SELL"
            fill = self.fill_model.fill_entry_short(book.bid_price, book.mid_price)
        else:
            return

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
            if pnl_bps >= self.take_profit_bps:
                exit_reason = "take_profit"
            elif pnl_bps <= -self.stop_loss_bps:
                exit_reason = "stop_loss"
            elif hold_ms >= self.max_hold_ms:
                exit_reason = "timeout"
            if exit_reason:
                exit_fill = self.fill_model.fill_exit_long(book.bid_price, book.mid_price)
                gross_pnl_bps = (exit_fill.price - pos.entry_price) / pos.entry_mid * 10000
                gross_pnl_usd = (exit_fill.price - pos.entry_price) * pos.qty
        else:  # SELL
            pnl_bps = (pos.entry_price - book.ask_price) / pos.entry_mid * 10000
            if pnl_bps >= self.take_profit_bps:
                exit_reason = "take_profit"
            elif pnl_bps <= -self.stop_loss_bps:
                exit_reason = "stop_loss"
            elif hold_ms >= self.max_hold_ms:
                exit_reason = "timeout"
            if exit_reason:
                exit_fill = self.fill_model.fill_exit_short(book.ask_price, book.mid_price)
                gross_pnl_bps = (pos.entry_price - exit_fill.price) / pos.entry_mid * 10000
                gross_pnl_usd = (pos.entry_price - exit_fill.price) * pos.qty

        if exit_reason is None or exit_fill is None:
            return

        # Fees: taker fee on entry notional + exit notional
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
