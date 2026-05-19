"""
Walk through the raw_signals history and create Trade entries
for every OPEN → CLOSE cycle.

This lets the dashboard show stats immediately,
without waiting 2 weeks for live trades to accumulate.

How it works:
1. Find all OPEN signals in chronological order.
2. For each OPEN, follow the reply chain to find CLOSE / UPDATE messages.
3. Create a Trade with simulated PnL:
   - If CLOSE says "фиксируем" / "закрываем" → assume exited at planned TP.
   - If CLOSE says "стоп лосс" → assume exited at planned SL.
4. Apply STOP_UPDATES and MOVE_TO_BE along the way.

Run once after backfill:
    .venv/bin/python scripts/simulate_trades.py
"""
from __future__ import annotations

import asyncio
import logging
import datetime

from sqlalchemy import select as sa_select, func

from app.db.session import async_session, init_db
from app.db.models import RawSignal, Trade, TradeLog, TradeStatus

logger = logging.getLogger("simulate")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_exit_type(text: str) -> str:
    """
    Read a CLOSE message and guess whether it was a win or a loss.

    Returns: "profit" | "loss" | "unknown"
    """

    text_lower = text.lower()

    loss_keywords = [
        "стоп лосс", "stop loss", "стоп-лосс",
        "закрыта по стоп", "сработал стоп", "минус",
    ]
    profit_keywords = [
        "фиксируем", "зафиксировал", "закрываем",
        "профит", "прибыль", "take profit",
        "тейк", "✅",
    ]

    for kw in loss_keywords:
        if kw in text_lower:
            return "loss"

    for kw in profit_keywords:
        if kw in text_lower:
            return "profit"

    return "profit"  # default — most closes are at profit


def _calc_pnl(
    entry: float,
    exit_price: float,
    quantity: float,
    planned_stop: float,
) -> tuple[float, float]:
    """
    Calculate PnL in $ and %.

    For LONG: pnl = (exit - entry) * quantity
    """

    amount = (exit_price - entry) * quantity
    risk = abs(entry - planned_stop)

    if risk > 0:
        percent = (amount / (entry * quantity)) * 100
    else:
        percent = 0.0

    return round(amount, 2), round(percent, 2)


# ---------------------------------------------------------------------------
# Main simulation
# ---------------------------------------------------------------------------


async def simulate():
    """Walk the history and create Trade entries."""

    await init_db()
    logger.info("Database ready")


    async with async_session() as session:

        # --- 1. Check if already simulated ---

        existing = await session.scalar(
            sa_select(func.count()).select_from(Trade)
        )

        if existing and existing > 0:
            logger.warning(
                "%s trades already exist — skipping simulation."
                " Delete signal_bot.db and re-run backfill if you want a fresh start.",
                existing,
            )
            return


        # --- 2. Get all OPEN signals, ordered by time ---

        result = await session.execute(
            sa_select(RawSignal)
            .where(RawSignal.action == "OPEN")
            .order_by(RawSignal.received_at.asc())
        )
        open_signals = result.scalars().all()

        logger.info("Found %s OPEN signals in history", len(open_signals))


        # --- 3. For each OPEN, build the trade lifecycle ---

        trades_created = 0

        for sig in open_signals:
            parsed = sig.parsed_json

            if not parsed:
                logger.warning(
                    "OPEN msg=%s has no parsed_json — skipping", sig.tg_message_id
                )
                continue

            pair   = parsed.get("pair", "UNKNOWN")
            entry  = float(parsed.get("entry", 0))
            sl     = float(parsed.get("stop_loss", 0))
            tp     = float(parsed.get("take_profit", 0))
            lev    = int(parsed.get("leverage", 20))
            qty    = float(parsed.get("quantity", 0))

            if entry <= 0 or sl <= 0 or tp <= 0:
                logger.warning(
                    "OPEN msg=%s has invalid prices — skipping", sig.tg_message_id
                )
                continue

            # Rough quantity estimation if not in parsed
            if qty <= 0:
                risk_amount = 1000 * 0.01  # 1% of $1000
                diff = abs(entry - sl)
                qty = risk_amount / diff if diff > 0 else 100

            logger.info(
                "[%s/%s] Simulating: %s entry=%.6f tp=%.6f sl=%.6f",
                trades_created + 1,
                len(open_signals),
                pair,
                entry,
                tp,
                sl,
            )

            # --- 3a. Find all replies to this signal (updates + close) ---

            result = await session.execute(
                sa_select(RawSignal)
                .where(RawSignal.tg_reply_to_id == sig.tg_message_id)
                .order_by(RawSignal.received_at.asc())
            )
            replies = result.scalars().all()

            # Current SL/TP start as planned
            current_sl = sl
            current_tp = tp

            # --- 3b. Process updates in order ---

            for reply in replies:
                action = reply.action

                if action == "UPDATE_STOP":
                    new_sl = reply.parsed_json.get("new_stop") if reply.parsed_json else None
                    if new_sl:
                        current_sl = float(new_sl)
                        logger.debug("  → SL updated to %.6f", current_sl)

                elif action == "UPDATE_TP":
                    new_tp = reply.parsed_json.get("new_tp") if reply.parsed_json else None
                    if new_tp:
                        current_tp = float(new_tp)
                        logger.debug("  → TP updated to %.6f", current_tp)

                elif action == "MOVE_TO_BE":
                    current_sl = entry
                    logger.debug("  → SL moved to breakeven (%.6f)", entry)

                elif action == "PARTIAL_CLOSE":
                    be = (
                        reply.parsed_json.get("move_stop_to_breakeven", False)
                        if reply.parsed_json else False
                    )
                    if be:
                        current_sl = entry
                        logger.debug("  → Partial close: SL to BE")

                elif action == "CLOSE":
                    exit_type = _detect_exit_type(reply.raw_text)

                    if exit_type == "profit":
                        exit_price = current_tp
                        exit_reason = "take_profit"
                    else:
                        exit_price = current_sl
                        exit_reason = "stop_loss"

                    pnl_amount, pnl_pct = _calc_pnl(
                        entry, exit_price, qty, sl
                    )

                    logger.info(
                        "  → CLOSED at %.6f (%s) — PnL: $%s (%s%%)",
                        exit_price, exit_reason, pnl_amount, pnl_pct,
                    )

                    # --- Create Trade row ---

                    trade = Trade(
                        signal_id          = sig.id,
                        pair               = pair,
                        direction          = "LONG",
                        leverage           = lev,
                        planned_entry      = entry,
                        planned_stop       = sl,
                        planned_tp         = tp,
                        risk_per_trade_pct = 1.0,
                        entry_price        = entry,
                        entry_quantity     = qty,
                        entry_time         = sig.received_at,
                        current_stop       = current_sl,
                        current_tp         = current_tp,
                        status             = TradeStatus.CLOSED,
                        exit_price         = exit_price,
                        exit_time          = reply.received_at,
                        exit_reason        = exit_reason,
                        pnl_amount         = pnl_amount,
                        pnl_percent         = pnl_pct,
                        created_at         = sig.received_at or datetime.datetime.utcnow(),
                        updated_at         = reply.received_at or datetime.datetime.utcnow(),
                    )
                    session.add(trade)
                    await session.flush()

                    # --- Log entries ---

                    open_log = TradeLog(
                        trade_id    = trade.id,
                        action      = "OPEN",
                        detail_json = {
                            "entry_price": entry,
                            "quantity": qty,
                            "simulated": True,
                        },
                        timestamp   = sig.received_at or datetime.datetime.utcnow(),
                    )
                    session.add(open_log)

                    close_log = TradeLog(
                        trade_id    = trade.id,
                        action      = "CLOSE",
                        detail_json = {
                            "exit_price": exit_price,
                            "exit_reason": exit_reason,
                            "simulated": True,
                        },
                        timestamp   = reply.received_at or datetime.datetime.utcnow(),
                    )
                    session.add(close_log)

                    await session.commit()

                    trades_created += 1
                    break  # trade is closed, stop processing replies

        # --- 4. Done ---

        logger.info(
            "Simulation complete! Created %s trades from %s open signals.",
            trades_created,
            len(open_signals),
        )

        # Count signals without CLOSE
        total_with_close = await session.scalar(
            sa_select(func.count())
            .select_from(Trade)
            .where(Trade.status == TradeStatus.CLOSED)
        )
        still_open_count = await session.scalar(
            sa_select(func.count()).where(RawSignal.action == "OPEN")
        ) - trades_created

        if still_open_count > 0:
            logger.info(
                "%s OPEN signals still active (no CLOSE found in history).",
                still_open_count,
            )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    asyncio.run(simulate())
