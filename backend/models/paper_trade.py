from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.session import Base


class PaperTrade(Base):
    """
    Paper trade record — strategy decision on live data, no real order placed.

    Mirrors BacktestTrade fields so backtest and live results are comparable.
    entry_time_ms / exit_time_ms are exchange timestamps (ms since epoch).
    created_at is the wall-clock time the trade was persisted.
    """

    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)   # BUY | SELL
    entry_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    exit_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    exit_reason: Mapped[str] = mapped_column(String(20), nullable=False)  # take_profit | stop_loss | timeout
    hold_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    gross_pnl_bps: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    gross_pnl_usd: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    fees_usd: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    net_pnl_usd: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_paper_trades_symbol_entry", "symbol", "entry_time_ms"),
    )
