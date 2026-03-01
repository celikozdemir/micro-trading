from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.session import Base


class BookTick(Base):
    """Best bid/ask snapshot from Binance bookTicker stream."""

    __tablename__ = "book_ticks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    timestamp_exchange: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timestamp_local: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bid_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    bid_qty: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    ask_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    ask_qty: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    spread_bps: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    lag_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_book_ticks_symbol_ts", "symbol", "timestamp_exchange"),
    )


class AggTrade(Base):
    """Aggregated trade from Binance aggTrade stream."""

    __tablename__ = "agg_trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    trade_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timestamp_exchange: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timestamp_local: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    is_buyer_maker: Mapped[bool] = mapped_column(Boolean, nullable=False)
    lag_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_agg_trades_symbol_ts", "symbol", "timestamp_exchange"),
    )


class LatencyMetric(Base):
    """Periodic WS latency snapshots for monitoring."""

    __tablename__ = "latency_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    timestamp_local: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    stream: Mapped[str] = mapped_column(String(20), nullable=False)
    p50_lag_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    p95_lag_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    p99_lag_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sample_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
