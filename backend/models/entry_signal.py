"""
Entry signal log — records features and outcomes at every entry candidate.

Used by the auto-retrainer to build ML training data from live trading.
Each row captures the 13-feature microstructure snapshot at the moment
the strategy decided to enter a trade, plus the eventual outcome.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.session import Base


class EntrySignal(Base):
    __tablename__ = "entry_signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    entry_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # 13 features
    afi: Mapped[float] = mapped_column(Float, nullable=False)
    obi: Mapped[float] = mapped_column(Float, nullable=False)
    intensity_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    vol_expansion: Mapped[float] = mapped_column(Float, nullable=False)
    mid_move_bps: Mapped[float] = mapped_column(Float, nullable=False)
    spread_bps: Mapped[float] = mapped_column(Float, nullable=False)
    book_depth_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    trade_imbalance_1s: Mapped[float] = mapped_column(Float, nullable=False)
    trade_imbalance_5s: Mapped[float] = mapped_column(Float, nullable=False)
    vwap_deviation_bps: Mapped[float] = mapped_column(Float, nullable=False)
    time_of_day_sin: Mapped[float] = mapped_column(Float, nullable=False)
    time_of_day_cos: Mapped[float] = mapped_column(Float, nullable=False)
    realized_vol_regime: Mapped[float] = mapped_column(Float, nullable=False)

    # Outcome (filled when the trade completes)
    exit_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    gross_pnl_bps: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    net_pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    hold_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profitable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_entry_signals_symbol_time", "symbol", "entry_time_ms"),
    )
