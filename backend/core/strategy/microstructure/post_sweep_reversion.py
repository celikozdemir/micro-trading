"""
Strategy B: Post-Sweep Micro Mean Reversion

After detecting an aggressive sweep (burst of trades pushing price hard in one
direction), fades the move — entering contrarian on the assumption that liquidity
will refill and price will snap back.

The edge is the OPPOSITE of Strategy A:
  Strategy A: enter WITH the burst (momentum continuation)
  Strategy B: enter AGAINST the burst (mean reversion / liquidity refill)

From the config:
  window_ms:            rolling window for sweep detection
  trade_count_trigger:  min trades in window to confirm a sweep
  move_bps_trigger:     min mid-price move (bps) in window to confirm a sweep
  entry_delay_ms:       wait this long after detecting sweep before entering
                        (0 = immediate; 50–150ms = wait for exhaustion)
  exit.take_profit_bps: close at this reversion gain
  exit.stop_loss_bps:   close at this loss (sweep continues against us)
  exit.max_hold_ms:     force-close if reversion hasn't happened
  cooldown_ms:          pause after each trade
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from backend.core.backtester.fill_model import FillModel
from backend.core.data.normalizer import AggTrade, BookTick
from backend.core.strategy.microstructure.burst_momentum import BacktestTrade, OpenPosition


# ------------------------------------------------------------------ #
# Internal state                                                       #
# ------------------------------------------------------------------ #


@dataclass
class SymbolState:
    # Rolling window entries: (timestamp_ms, qty, is_buy_aggressor)
    trade_window: deque = field(default_factory=deque)
    # Mid-price history: (timestamp_ms, mid_price)
    mid_history: deque = field(default_factory=deque)
    # Most recent book tick
    last_book: Optional[BookTick] = None
    # Cooldown expiry
    cooldown_until_ms: int = 0
    # Open position
    open_position: Optional[OpenPosition] = None
    # Pending contrarian entry after entry_delay_ms
    pending_side: Optional[str] = None   # "BUY" | "SELL"
    pending_after_ms: int = 0            # execute entry once now_ms >= this


# ------------------------------------------------------------------ #
# Strategy                                                             #
# ------------------------------------------------------------------ #


class PostSweepReversionStrategy:
    """
    Stateful post-sweep mean reversion strategy.
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
        self.entry_delay_ms: int = s.get("entry_delay_ms", 0)
        ex = s["exit"]
        self.take_profit_bps: Decimal = Decimal(str(ex["take_profit_bps"]))
        self.stop_loss_bps: Decimal = Decimal(str(ex["stop_loss_bps"]))
        self.max_hold_ms: int = ex["max_hold_ms"]
        self.cooldown_ms: int = s["cooldown_ms"]
        self.max_spread_bps: Decimal = Decimal(str(config["risk"]["max_spread_bps"]))
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

        state.mid_history.append((bt.timestamp_exchange_ms, bt.mid_price))
        cutoff = bt.timestamp_exchange_ms - self.window_ms
        while state.mid_history and state.mid_history[0][0] < cutoff:
            state.mid_history.popleft()

        # Execute pending delayed entry if the delay has elapsed
        if state.pending_side is not None and bt.timestamp_exchange_ms >= state.pending_after_ms:
            self._execute_pending_entry(bt.symbol, bt.timestamp_exchange_ms, bt)

        # Check exit for open position
        if state.open_position:
            self._check_exit(bt.symbol, bt.timestamp_exchange_ms, bt)

    def _on_agg_trade(self, at: AggTrade) -> None:
        state = self._get_state(at.symbol)
        now_ms = at.timestamp_exchange_ms

        is_buy_aggressor = not at.is_buyer_maker
        state.trade_window.append((now_ms, at.qty, is_buy_aggressor))

        cutoff = now_ms - self.window_ms
        while state.trade_window and state.trade_window[0][0] < cutoff:
            state.trade_window.popleft()

        # Keep mid_history alive from trade price when book_ticks are sparse
        state.mid_history.append((now_ms, at.price))
        while state.mid_history and state.mid_history[0][0] < cutoff:
            state.mid_history.popleft()

        # Timeout check on agg_trade when no book_tick arrives
        if state.open_position is not None and state.last_book is not None:
            hold_ms = now_ms - state.open_position.entry_time_ms
            if hold_ms >= self.max_hold_ms:
                self._check_exit(at.symbol, now_ms, state.last_book)
                return

        # No entry logic while a position or pending entry is active
        if state.open_position is not None or state.pending_side is not None:
            return

        if now_ms > state.cooldown_until_ms:
            self._check_sweep(at.symbol, now_ms)

    # ---------------------------------------------------------------- #
    # Sweep detection → contrarian entry                                #
    # ---------------------------------------------------------------- #

    def _check_sweep(self, symbol: str, now_ms: int) -> None:
        state = self._get_state(symbol)
        book = state.last_book

        if book is None:
            return
        if len(state.trade_window) < self.trade_count_trigger:
            return
        if len(state.mid_history) < 2:
            return

        window_start_mid = state.mid_history[0][1]
        if window_start_mid == 0:
            return

        mid_move_bps = (book.mid_price - window_start_mid) / window_start_mid * 10000
        if abs(mid_move_bps) < self.move_bps_trigger:
            return

        if book.spread_bps > self.max_spread_bps:
            return

        # Confirm sweep direction with aggressor flow
        buy_qty = sum(q for _, q, is_buy in state.trade_window if is_buy)
        sell_qty = sum(q for _, q, is_buy in state.trade_window if not is_buy)

        # Fade the sweep: SHORT after UP burst, LONG after DOWN burst
        if mid_move_bps > 0 and buy_qty > sell_qty:
            side = "SELL"
        elif mid_move_bps < 0 and sell_qty > buy_qty:
            side = "BUY"
        else:
            return

        if self.entry_delay_ms > 0:
            # Delayed entry: wait for sweep exhaustion before committing
            state.pending_side = side
            state.pending_after_ms = now_ms + self.entry_delay_ms
        else:
            # Immediate entry
            self._open_position(symbol, side, now_ms, book)

    def _execute_pending_entry(self, symbol: str, now_ms: int, book: BookTick) -> None:
        state = self._get_state(symbol)
        side = state.pending_side
        state.pending_side = None

        # Cancel if spread has blown out since we detected the sweep
        if book.spread_bps > self.max_spread_bps:
            return

        self._open_position(symbol, side, now_ms, book)

    def _open_position(self, symbol: str, side: str, now_ms: int, book: BookTick) -> None:
        state = self._get_state(symbol)
        qty = self.entry_qty.get(symbol, Decimal("0.001"))

        if side == "BUY":
            fill = self.fill_model.fill_entry_long(book.ask_price, book.mid_price)
        else:
            fill = self.fill_model.fill_entry_short(book.bid_price, book.mid_price)

        state.open_position = OpenPosition(
            side=side,
            entry_time_ms=now_ms,
            entry_price=fill.price,
            qty=qty,
            entry_mid=book.mid_price,
        )

    # ---------------------------------------------------------------- #
    # Exit logic (identical mechanics to BurstMomentumStrategy)         #
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
