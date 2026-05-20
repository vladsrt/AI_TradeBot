"""
Database models for the signal bot.

Three main tables:
- RawSignal  — every Telegram message we receive
- Trade      — a position opened from a signal
- TradeLog   — every action taken on a trade (open, update, close)
"""
from __future__ import annotations

import datetime
import enum
from typing import Optional

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    DateTime,
    Enum as SAEnum,
    Text,
    ForeignKey,
    JSON,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Shared base for all ORM models."""
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SignalAction(str, enum.Enum):
    """Possible actions parsed from a Telegram signal."""

    OPEN          = "OPEN"           # new position
    UPDATE_STOP   = "UPDATE_STOP"    # move stop-loss
    UPDATE_TP     = "UPDATE_TP"      # move take-profit
    PARTIAL_CLOSE = "PARTIAL_CLOSE"  # close part, move SL to BE
    MOVE_TO_BE    = "MOVE_TO_BE"     # move stop-loss to entry price
    CLOSE         = "CLOSE"          # exit the position
    CANCEL_ORDER  = "CANCEL_ORDER"   # cancel a limit order
    LIMIT_FILLED  = "LIMIT_FILLED"   # limit order was filled
    UNKNOWN       = "UNKNOWN"        # could not classify


class TradeStatus(str, enum.Enum):
    """Lifecycle of a trade."""

    PENDING           = "PENDING"            # signal parsed, not yet executed
    OPEN              = "OPEN"               # position is active on exchange
    UPDATED           = "UPDATED"            # SL or TP was modified
    PARTIALLY_CLOSED  = "PARTIALLY_CLOSED"   # some of the position was closed
    CLOSED            = "CLOSED"             # fully exited
    ERROR             = "ERROR"              # execution failed


# ---------------------------------------------------------------------------
# RawSignal — stores every message from the channel
# ---------------------------------------------------------------------------


class RawSignal(Base):
    """
    One row per Telegram message that looks like a trading signal.
    Stores the raw text AND the AI-parsed structured JSON.
    """

    __tablename__ = "raw_signals"

    # --- IDs ------------------------------------------------------------

    id: Mapped[int] = mapped_column(primary_key=True)

    tg_message_id: Mapped[int] = mapped_column(
        Integer, unique=True
    )

    tg_chat_id: Mapped[int] = mapped_column(Integer)

    tg_reply_to_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )

    # --- Content --------------------------------------------------------

    raw_text: Mapped[str] = mapped_column(Text)

    is_edit: Mapped[bool] = mapped_column(Boolean, default=False)

    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )

    # --- AI parser result -----------------------------------------------

    parsed_json: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True
    )

    action: Mapped[Optional[str]] = mapped_column(
        String(30), nullable=True
    )

    parse_success: Mapped[bool] = mapped_column(
        Boolean, default=False
    )

    parse_error: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )

    parse_latency_ms: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    # --- Relationship ---------------------------------------------------

    trade: Mapped[Optional["Trade"]] = relationship(
        back_populates="signal", uselist=False
    )


# ---------------------------------------------------------------------------
# Trade — one position opened from a signal
# ---------------------------------------------------------------------------


class Trade(Base):
    """
    A real position opened on the exchange.
    Linked 1:1 to a RawSignal (the OPEN message).
    """

    __tablename__ = "trades"

    # --- IDs ------------------------------------------------------------

    id: Mapped[int] = mapped_column(primary_key=True)

    signal_id: Mapped[int] = mapped_column(
        ForeignKey("raw_signals.id"), unique=True
    )

    signal: Mapped["RawSignal"] = relationship(back_populates="trade")

    # --- Market info ----------------------------------------------------

    pair: Mapped[str] = mapped_column(String(20))

    direction: Mapped[str] = mapped_column(String(10), default="LONG")

    leverage: Mapped[int] = mapped_column(Integer, default=20)

    # --- Entry ----------------------------------------------------------

    entry_price: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    entry_quantity: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    entry_time: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )

    entry_order_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )

    # --- Planned risk parameters (from signal) --------------------------

    planned_entry: Mapped[float] = mapped_column(Float)

    planned_stop: Mapped[float] = mapped_column(Float)

    planned_tp: Mapped[float] = mapped_column(Float)

    risk_per_trade_pct: Mapped[float] = mapped_column(
        Float, default=1.0
    )

    # --- Current state (can change via updates) -------------------------

    current_stop: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    current_tp: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    status: Mapped[TradeStatus] = mapped_column(
        SAEnum(TradeStatus), default=TradeStatus.PENDING
    )

    # --- Exit -----------------------------------------------------------

    exit_price: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    exit_time: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True
    )

    exit_reason: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )

    # --- PnL ------------------------------------------------------------

    pnl_amount: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    pnl_percent: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    max_favorable_excursion: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    max_adverse_excursion: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    # --- Timestamps -----------------------------------------------------

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )

    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    # --- Relationship ---------------------------------------------------

    log: Mapped[list["TradeLog"]] = relationship(back_populates="trade")


# ---------------------------------------------------------------------------
# TradeLog — every action on a trade
# ---------------------------------------------------------------------------


class TradeLog(Base):
    """
    Immutable log entry for every action taken on a trade.
    OPEN → UPDATE_STOP → MOVE_TO_BE → CLOSE, etc.
    """

    __tablename__ = "trade_logs"

    # --- IDs ------------------------------------------------------------

    id: Mapped[int] = mapped_column(primary_key=True)

    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"))

    trade: Mapped["Trade"] = relationship(back_populates="log")

    # --- Content --------------------------------------------------------

    action: Mapped[str] = mapped_column(String(30))

    detail_json: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True
    )

    timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )
