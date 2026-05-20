"""
Telethon userbot — the heart of the signal bot.

What it does:
1. Listens to new and edited messages in the VIP Telegram channel.
2. Parses each message with regex (instant) — no LLM.
3. Opens positions on Binance Demo (OPEN signals), including limit orders.
4. Handles LIMIT_FILLED, CANCEL_ORDER, UPDATE_STOP/TP, PARTIAL_CLOSE, CLOSE.
5. Stores everything in SQLite for the dashboard.

Run:
    .venv/bin/python -m app.telegram.listener
"""
from __future__ import annotations

import asyncio
import logging
import datetime
from typing import Optional

from telethon import TelegramClient, events
from telethon.tl.types import Channel
from sqlalchemy import select as sa_select, desc

from app.config import config
from app.parser.ai_parser import parser as ai_parser
from app.exchange.broker import exchange
from app.db.session import async_session, init_db
from app.db.models import (
    RawSignal,
    Trade,
    TradeLog,
    TradeStatus,
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


logger = logging.getLogger("signal-bot")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ---------------------------------------------------------------------------
# Telegram client
# ---------------------------------------------------------------------------


client = TelegramClient(
    "session/shark_vip",
    config.TG_API_ID,
    config.TG_API_HASH,
)


# ---------------------------------------------------------------------------
# Channel resolver — finds the channel by name in the user's dialogs
# ---------------------------------------------------------------------------


async def resolve_channel(name: str) -> Channel:
    """
    Search the user's Telegram dialogs for a channel whose name
    contains `name` (case-insensitive match).

    Raises RuntimeError if the channel is not found.
    """

    await client.start(phone=config.TG_PHONE)

    async for dialog in client.iter_dialogs():
        if dialog.name and name.lower() in dialog.name.lower():
            logger.info(
                "Found channel: %s (ID=%s)", dialog.name, dialog.id
            )
            return dialog.entity

    raise RuntimeError(
        f"Channel '{name}' not found in your dialogs. Are you subscribed?"
    )


# ---------------------------------------------------------------------------
# Position size calculator
# ---------------------------------------------------------------------------


def calc_position_size(
    deposit: float,
    risk_pct: float,
    entry: float,
    stop: float,
) -> float:
    """
    Calculate how many coins to buy.

    Formula (for LONG positions):
        risk_amount = deposit × (risk_pct / 100)
        quantity    = risk_amount / |entry - stop|

    This ensures that if stop-loss is hit, we lose exactly `risk_pct`% of the deposit.
    """

    risk_amount = deposit * (risk_pct / 100)
    diff = abs(entry - stop)

    if diff <= 0:
        return 0.0

    return risk_amount / diff


# ---------------------------------------------------------------------------
# Trade lookup helpers
# ---------------------------------------------------------------------------


async def find_trade_by_pair(pair: str, session) -> Optional[Trade]:
    """
    Find the most recent OPEN/PENDING/UPDATED trade for a pair.
    Used for updates that don't come as replies (just mention #PAIR).
    """
    result = await session.execute(
        sa_select(Trade)
        .where(
            Trade.pair == pair,
            Trade.status.in_([TradeStatus.PENDING, TradeStatus.OPEN, TradeStatus.UPDATED]),
        )
        .order_by(desc(Trade.id))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def find_trade_by_reply(reply_to_id: int, session) -> Optional[Trade]:
    """
    Find a trade by the Telegram message ID it was created from.
    Used for reply-based updates (most common).
    """
    # Step 1: find the RawSignal that this message replies to
    result = await session.execute(
        sa_select(RawSignal).where(
            RawSignal.tg_message_id == reply_to_id
        )
    )
    original_signal = result.scalar_one_or_none()

    if not original_signal:
        return None

    # Step 2: find the Trade linked to that signal
    result = await session.execute(
        sa_select(Trade).where(Trade.signal_id == original_signal.id)
    )
    return result.scalar_one_or_none()


async def find_trade_for_update(
    parsed: dict,
    raw: RawSignal,
    session,
) -> Optional[Trade]:
    """
    Try to find the relevant trade:
    1. If this is a reply — find by reply_to message ID.
    2. If parsed has a 'pair' field — find the most recent open trade for that pair.
    """
    # Try reply first
    if raw.tg_reply_to_id:
        trade = await find_trade_by_reply(raw.tg_reply_to_id, session)
        if trade:
            return trade

    # Fallback: find by pair
    pair = parsed.get("pair")
    if pair:
        return await find_trade_by_pair(pair, session)

    return None


# ---------------------------------------------------------------------------
# Signal handler — OPEN (new position)
# ---------------------------------------------------------------------------


async def handle_open_signal(
    parsed: dict,
    raw: RawSignal,
    session,
) -> None:
    """
    Execute a new LONG position on Binance Demo.

    Steps:
    1. Set leverage.
    2. Place entry order (MARKET or LIMIT).
    3. Place stop-loss and take-profit orders.
    4. Save the trade to the database.
    """

    # --- Extract values from parsed signal -------------------------------

    pair       = parsed["pair"]
    entry      = float(parsed["entry"])
    entry_type = parsed.get("entry_type", "MARKET").upper()
    tp         = float(parsed["take_profit"])
    sl         = float(parsed["stop_loss"])
    lev        = int(parsed.get("leverage", config.DEFAULT_LEVERAGE))
    # ALWAYS use OUR deposit, not the one from the signal (that's the author's deposit)
    deposit    = config.DEFAULT_DEPOSIT  # $10

    # Margin = deposit (e.g. $10). Position notional = margin × leverage = $10 × 20x = $200
    position_notional = deposit * lev  # e.g. $10 × 20 = $200
    qty_raw = position_notional / entry  # e.g. $200 / $0.10424 ≈ 1918 DOGE

    # Round quantity to Binance stepSize
    sym_info = await exchange.get_symbol_info(pair)
    if sym_info:
        step = sym_info.get("stepSize", 0.0001)
        min_qty = sym_info.get("minQty", 0)
        qty = exchange.round_quantity(qty_raw, step)
        if qty < min_qty:
            qty = min_qty
        logger.info(
            "[OPEN] %s qty: \${%.2f} / \${%.6f} = %.4f → rounded to %s (step=%s)",
            pair, deposit, entry, qty_raw, qty, step,
        )
    else:
        qty = qty_raw
        logger.warning("[OPEN] %s — cannot get symbol info, using raw qty", pair)

    logger.info(
        "[OPEN] %s entry=%s(%s) tp=%s sl=%s lev=%sx margin=$%.0f notional=$%.0f qty=%s",
        pair, entry, entry_type, tp, sl, lev, deposit, position_notional, qty,
    )


    # --- 1. Set leverage -------------------------------------------------

    try:
        await exchange.set_leverage(pair, lev)
    except Exception as exc:
        logger.warning("Leverage already set or not needed: %s", exc)


    # --- 2. Place entry order --------------------------------------------

    if entry_type == "LIMIT":
        order = await exchange.limit_buy(pair, entry, qty)

        # Limit orders might not fill immediately — trade is PENDING
        fill_price = None
        status = TradeStatus.PENDING
        order_id = str(order.get("orderId", ""))
        logger.info("[OPEN] Limit order placed: %s @ %s (ID=%s)", pair, entry, order_id)
    else:
        order = await exchange.market_buy(pair, qty)

        fill_price = float(order.get("avgPrice") or order.get("price") or entry)
        status = TradeStatus.OPEN
        order_id = str(order.get("orderId", ""))
        logger.info("[OPEN] Market order filled: %s @ %s", pair, fill_price)


    # --- 3. Place SL and TP orders ---------------------------------------

    await exchange.place_stop_loss(pair, sl, qty)
    await exchange.place_take_profit(pair, tp, qty)


    # --- 4. Save trade to database ---------------------------------------

    trade = Trade(
        signal_id          = raw.id,
        pair               = pair,
        direction          = "LONG",
        leverage           = lev,
        planned_entry      = entry,
        planned_stop       = sl,
        planned_tp         = tp,
        risk_per_trade_pct = risk_pct,
        entry_price        = fill_price,
        entry_quantity     = qty,
        entry_time         = datetime.datetime.utcnow() if status == TradeStatus.OPEN else None,
        entry_order_id     = order_id,
        current_stop       = sl,
        current_tp         = tp,
        status             = status,
    )
    session.add(trade)
    await session.flush()


    # --- 5. Log the action -----------------------------------------------

    log_entry = TradeLog(
        trade_id    = trade.id,
        action      = "OPEN",
        detail_json = {
            "entry_type": entry_type,
            "entry_price": fill_price,
            "quantity": qty,
            "order_id": order_id,
            "order_status": order.get("status"),
        },
    )
    session.add(log_entry)
    await session.commit()

    logger.info("[OPEN] ✅ Trade #%s: %s @ %s (%s)", trade.id, pair, fill_price or entry, status.value)


# ---------------------------------------------------------------------------
# Signal handler — CANCEL_ORDER
# ---------------------------------------------------------------------------


async def handle_cancel_order(
    parsed: dict,
    raw: RawSignal,
    session,
) -> None:
    """
    Cancel a limit order that hasn't been filled yet.

    Looks up the most recent PENDING trade (limit order waiting for fill)
    and cancels it on Binance.
    """
    trade = await find_trade_for_update(parsed, raw, session)

    if not trade:
        logger.warning("[CANCEL] No pending trade found to cancel")
        return

    if trade.status != TradeStatus.PENDING:
        logger.warning("[CANCEL] Trade #%s is %s — not pending, cannot cancel", trade.id, trade.status.value)
        return

    if not trade.entry_order_id:
        logger.warning("[CANCEL] Trade #%s has no order_id — cannot cancel", trade.id)
        return

    logger.info("[CANCEL] Cancelling order %s for %s", trade.entry_order_id, trade.pair)

    try:
        await exchange.cancel_order(trade.pair, int(trade.entry_order_id))
    except Exception as exc:
        logger.warning("[CANCEL] Cancel API call failed (may already be cancelled): %s", exc)

    # Cancel any remaining open orders for this symbol
    await exchange.cancel_all_open_orders(trade.pair)

    trade.status = TradeStatus.CLOSED
    trade.exit_reason = "cancelled"

    log_entry = TradeLog(
        trade_id    = trade.id,
        action      = "CANCEL_ORDER",
        detail_json = parsed,
    )
    session.add(log_entry)
    await session.commit()

    logger.info("[CANCEL] ✅ Trade #%s cancelled", trade.id)


# ---------------------------------------------------------------------------
# Signal handler — LIMIT_FILLED
# ---------------------------------------------------------------------------


async def handle_limit_filled(
    parsed: dict,
    raw: RawSignal,
    session,
) -> None:
    """
    Mark a PENDING limit trade as OPEN when the limit order was triggered.

    Gets the fill price from the current position on Binance.
    """
    trade = await find_trade_for_update(parsed, raw, session)

    if not trade:
        logger.warning("[LIMIT_FILLED] No trade found")
        return

    if trade.status != TradeStatus.PENDING:
        logger.warning("[LIMIT_FILLED] Trade #%s is %s — not pending", trade.id, trade.status.value)
        return

    # Get the actual fill price from the position
    pos = await exchange.get_position(trade.pair)
    if pos and float(pos.get("positionAmt", 0)) > 0:
        entry_price = float(pos.get("entryPrice", trade.planned_entry))
    else:
        # Fallback: use planned entry price
        entry_price = trade.planned_entry

    trade.status = TradeStatus.OPEN
    trade.entry_price = entry_price
    trade.entry_time = datetime.datetime.utcnow()

    log_entry = TradeLog(
        trade_id    = trade.id,
        action      = "LIMIT_FILLED",
        detail_json = {
            "entry_price": entry_price,
            "message": parsed.get("message", ""),
        },
    )
    session.add(log_entry)
    await session.commit()

    logger.info("[LIMIT_FILLED] ✅ Trade #%s: %s @ %s", trade.id, trade.pair, entry_price)


# ---------------------------------------------------------------------------
# Signal handler — UPDATE (stop, tp, close, etc.)
# ---------------------------------------------------------------------------


async def handle_update_signal(
    parsed: dict,
    raw: RawSignal,
    session,
) -> None:
    """
    Handle an UPDATE-type signal.

    Finds the relevant trade (via reply or pair name) and applies the change.
    """

    action = parsed["action"]

    trade = await find_trade_for_update(parsed, raw, session)

    if not trade:
        logger.warning("[UPDATE] No trade found for action=%s", action)
        return

    if trade.status == TradeStatus.CLOSED:
        logger.warning("[UPDATE] Trade #%s is already closed", trade.id)
        return

    logger.info("[UPDATE] Trade #%s (%s): %s", trade.id, trade.pair, action)


    # --- Apply the update ------------------------------------------------


    try:

        if action == "UPDATE_STOP":
            new_sl = float(parsed["new_stop"])
            await exchange.update_sl(trade.pair, new_sl, trade.entry_quantity)
            trade.current_stop = new_sl
            trade.status = TradeStatus.UPDATED

        elif action == "UPDATE_TP":
            new_tp = float(parsed["new_tp"])
            await exchange.update_tp(trade.pair, new_tp, trade.entry_quantity)
            trade.current_tp = new_tp
            trade.status = TradeStatus.UPDATED

        elif action == "MOVE_TO_BE":
            await exchange.update_sl(
                trade.pair, trade.entry_price, trade.entry_quantity
            )
            trade.current_stop = trade.entry_price
            trade.status = TradeStatus.UPDATED

        elif action == "PARTIAL_CLOSE":
            move_be = parsed.get("move_stop_to_breakeven", False)
            if move_be:
                await exchange.update_sl(
                    trade.pair, trade.entry_price, trade.entry_quantity
                )
                trade.current_stop = trade.entry_price
                trade.status = TradeStatus.PARTIALLY_CLOSED

        elif action == "CLOSE":
            order = await exchange.close_position(trade.pair)
            await exchange.cancel_all_open_orders(trade.pair)

            trade.status      = TradeStatus.CLOSED
            trade.exit_time   = datetime.datetime.utcnow()
            trade.exit_reason = "signal_closed"

            if order:
                exit_price = float(order.get("avgPrice") or order.get("price") or 0)
                trade.exit_price = exit_price

                # Calculate PnL
                if trade.entry_price and trade.entry_quantity:
                    entry_value = trade.entry_price * trade.entry_quantity
                    exit_value  = exit_price * trade.entry_quantity
                    trade.pnl_amount   = round(exit_value - entry_value, 2)
                    if entry_value > 0:
                        trade.pnl_percent = round((exit_value - entry_value) / entry_value * 100, 2)

        else:
            logger.info("[UPDATE] Unhandled action: %s", action)
            return

    except Exception as exc:
        logger.error("[UPDATE] Execution failed: %s", exc, exc_info=True)
        return


    # --- Log the action --------------------------------------------------

    log_entry = TradeLog(
        trade_id    = trade.id,
        action      = action,
        detail_json = parsed,
    )
    session.add(log_entry)
    await session.commit()

    logger.info("[UPDATE] ✅ Trade #%s: %s applied", trade.id, action)


# ---------------------------------------------------------------------------
# Message handler — fires on every new message in the channel
# ---------------------------------------------------------------------------


async def on_message(event: events.NewMessage.Event):
    """
    Called when a new message arrives in the VIP channel.

    1. Parse text with regex (instant) → get structured JSON.
    2. Save to database (raw_signals table).
    3. If action is OPEN → execute a trade.
    4. If action is CANCEL → cancel limit order.
    5. If action is UPDATE/CLOSE → modify the linked trade.
    """

    msg = event.message

    if not msg.text:
        return  # skip images, stickers, etc.


    text = msg.text.strip()

    # Check if this is a reply to an earlier signal
    reply_to = msg.reply_to.reply_to_msg_id if msg.reply_to else None


    # --- 1. Parse --------------------------------------------------------

    parsed, latency = await ai_parser.parse(
        text, is_reply=(reply_to is not None)
    )

    if parsed is None:
        logger.warning("[SKIP] Parse failed: %s...", text[:80])
        return

    action = parsed.get("action", "UNKNOWN")

    logger.info(
        "[SIGNAL] msg=%s action=%s latency=%.0fms",
        msg.id, action, latency,
    )


    # --- 2. Store raw signal ---------------------------------------------

    async with async_session() as session:

        raw = RawSignal(
            tg_message_id     = msg.id,
            tg_chat_id        = msg.chat_id,
            tg_reply_to_id    = reply_to,
            raw_text          = text,
            parsed_json       = parsed,
            action            = action,
            parse_success     = True,
            parse_latency_ms  = latency,
            received_at       = msg.date or datetime.datetime.utcnow(),
        )
        session.add(raw)
        await session.flush()


        # --- 3. Execute action -------------------------------------------

        try:

            if action == "OPEN":
                await handle_open_signal(parsed, raw, session)

            elif action == "CANCEL_ORDER":
                await handle_cancel_order(parsed, raw, session)

            elif action == "LIMIT_FILLED":
                await handle_limit_filled(parsed, raw, session)

            elif action in (
                "UPDATE_STOP", "UPDATE_TP", "PARTIAL_CLOSE",
                "MOVE_TO_BE", "CLOSE",
            ):
                await handle_update_signal(parsed, raw, session)

            else:
                logger.info("[SKIP] Unhandled action: %s", action)

        except Exception as exc:
            logger.error(
                "[ERROR] %s for msg=%s: %s", action, msg.id, exc,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Edited message handler — signal posts can be edited
# ---------------------------------------------------------------------------


async def on_edit(event: events.MessageEdited.Event):
    """
    Called when an existing message is edited.
    Re-parses and applies the new action.

    Important: edits can carry new actions (e.g. "changed stop to X").
    We process them the same as new messages.
    """

    msg = event.message

    if not msg.text:
        return


    text = msg.text.strip()
    reply_to = msg.reply_to.reply_to_msg_id if msg.reply_to else None


    # --- Parse -----------------------------------------------------------

    parsed, latency = await ai_parser.parse(
        text, is_reply=(reply_to is not None)
    )

    if parsed is None:
        return

    action = parsed.get("action", "UNKNOWN")

    logger.info(
        "[EDIT] msg=%s action=%s latency=%.0fms",
        msg.id, action, latency,
    )


    # --- Store and execute -----------------------------------------------

    async with async_session() as session:

        raw = RawSignal(
            tg_message_id     = msg.id,
            tg_chat_id        = msg.chat_id,
            tg_reply_to_id    = reply_to,
            raw_text          = text,
            parsed_json       = parsed,
            action            = action,
            parse_success     = True,
            parse_latency_ms  = latency,
            is_edit           = True,
            received_at       = datetime.datetime.utcnow(),
        )
        session.add(raw)
        await session.flush()


        # --- Execute edit as a real action -------------------------------

        try:

            if action == "CANCEL_ORDER":
                await handle_cancel_order(parsed, raw, session)

            elif action == "LIMIT_FILLED":
                await handle_limit_filled(parsed, raw, session)

            elif action in (
                "UPDATE_STOP", "UPDATE_TP", "PARTIAL_CLOSE",
                "MOVE_TO_BE", "CLOSE",
            ):
                await handle_update_signal(parsed, raw, session)

            elif action == "OPEN":
                # Edits that are new OPEN signals → rare but possible
                await handle_open_signal(parsed, raw, session)

            # skip UNKNOWN, etc.

        except Exception as exc:
            logger.error(
                "[EDIT-ERROR] %s for msg=%s: %s", action, msg.id, exc,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main_async():
    """
    Start the signal bot:
    1. Init database (create tables).
    2. Test Binance connection.
    3. Find the Telegram channel.
    4. Register message handlers.
    5. Run until Ctrl+C.
    """

    # --- Database --------------------------------------------------------

    await init_db()
    logger.info("Database ready")


    # --- Binance connection test -----------------------------------------

    logger.info("Testing Binance Demo connection...")
    ok = await exchange.connect()

    if not ok:
        logger.warning(
            "Binance Demo connection failed — "
            "will still listen and save signals, but won't trade"
        )
    else:
        balance = await exchange.get_balance()
        usdt = balance.get("totalWalletBalance", "?")
        logger.info("Binance Demo connected. USDT balance: %s", usdt)


    # --- Find channel ----------------------------------------------------

    channel = await resolve_channel(config.TG_CHANNEL_NAME)
    logger.info("Listening on: %s", channel.title)


    # --- Register handlers -----------------------------------------------

    client.add_event_handler(
        on_message, events.NewMessage(chats=[channel])
    )
    client.add_event_handler(
        on_edit, events.MessageEdited(chats=[channel])
    )


    # --- Background SL/TP monitor ----------------------------------------
    # Binance Demo doesn't support STOP_MARKET/TAKE_PROFIT orders.
    # We poll prices every 2 seconds and close positions ourselves.

    async def monitor_sl_tp():
        """Background task: check if any open trade hit SL or TP."""
        while True:
            try:
                await asyncio.sleep(2)
                async with async_session() as session:
                    from sqlalchemy import select as sa_select
                    result = await session.execute(
                        sa_select(Trade).where(
                            Trade.status == TradeStatus.OPEN,
                            Trade.current_stop.isnot(None),
                            Trade.current_tp.isnot(None),
                        )
                    )
                    open_trades = result.scalars().all()

                    for trade in open_trades:
                        hit = await exchange.check_sl_tp(
                            trade.pair,
                            trade.current_stop,
                            trade.current_tp,
                        )
                        if hit:
                            logger.warning(
                                "[SL/TP MONITOR] %s hit %s — closing position",
                                trade.pair, hit,
                            )
                            order = await exchange.close_position(trade.pair)
                            if order:
                                exit_price = float(
                                    order.get("avgPrice") or order.get("price") or 0
                                )
                                trade.status = TradeStatus.CLOSED
                                trade.exit_time = datetime.datetime.utcnow()
                                trade.exit_price = exit_price
                                trade.exit_reason = hit

                                if trade.entry_price and trade.entry_quantity:
                                    entry_val = trade.entry_price * trade.entry_quantity
                                    exit_val = exit_price * trade.entry_quantity
                                    trade.pnl_amount = round(exit_val - entry_val, 2)
                                    if entry_val > 0:
                                        trade.pnl_percent = round(
                                            (exit_val - entry_val) / entry_val * 100, 2
                                        )

                                log_entry = TradeLog(
                                    trade_id=trade.id,
                                    action="SL_TP_HIT",
                                    detail_json={
                                        "reason": hit,
                                        "exit_price": exit_price,
                                    },
                                )
                                session.add(log_entry)
                                await session.commit()
                                logger.info(
                                    "[SL/TP MONITOR] ✅ Trade #%s closed: %s, pnl=$%.2f",
                                    trade.id, hit, trade.pnl_amount,
                                )
            except Exception as exc:
                logger.error("[SL/TP MONITOR] error: %s", exc, exc_info=True)

    asyncio.create_task(monitor_sl_tp())


    # --- Run forever -----------------------------------------------------

    logger.info("🚀 Signal Bot running! Ctrl+C to stop.")
    await client.run_until_disconnected()


# ---------------------------------------------------------------------------
# Script entrypoint
# ---------------------------------------------------------------------------


def main():
    """Synchronous wrapper — called by `python -m app.telegram.listener`."""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
