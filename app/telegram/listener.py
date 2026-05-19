"""
Telethon userbot — the heart of the signal bot.

What it does:
1. Listens to new and edited messages in the VIP Telegram channel.
2. Sends each message to the AI parser → gets structured JSON.
3. Opens positions on Binance Demo (OPEN signals).
4. Updates stops / take-profits / closes (UPDATE / CLOSE signals).
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
from sqlalchemy import select as sa_select

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
    deposit    = float(parsed.get("deposit") or config.DEFAULT_DEPOSIT)
    risk_pct   = float(parsed.get("risk_pct") or config.DEFAULT_RISK_PERCENT)

    qty = calc_position_size(deposit, risk_pct, entry, sl)

    logger.info(
        "[OPEN] %s entry=%s(%s) tp=%s sl=%s lev=%sx qty=%.4f",
        pair, entry, entry_type, tp, sl, lev, qty,
    )


    # --- 1. Set leverage -------------------------------------------------

    try:
        await exchange.set_leverage(pair, lev)
    except Exception as exc:
        logger.warning("Leverage already set or not needed: %s", exc)


    # --- 2. Place entry order --------------------------------------------

    if entry_type == "LIMIT":
        order = await exchange.limit_buy(pair, entry, qty)
    else:
        order = await exchange.market_buy(pair, qty)

    fill_price = order.get("average") or order.get("price") or entry


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
        entry_time         = datetime.datetime.utcnow(),
        entry_order_id     = str(order.get("id", "")),
        current_stop       = sl,
        current_tp         = tp,
        status             = TradeStatus.OPEN,
    )
    session.add(trade)
    await session.flush()


    # --- 5. Log the action -----------------------------------------------

    log_entry = TradeLog(
        trade_id    = trade.id,
        action      = "OPEN",
        detail_json = {
            "entry_price": fill_price,
            "quantity": qty,
            "order_id": order.get("id"),
            "order_status": order.get("status"),
        },
    )
    session.add(log_entry)
    await session.commit()

    logger.info("[OPEN] ✅ Trade #%s: %s @ %s", trade.id, pair, fill_price)


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

    This message is usually a REPLY to a previous OPEN signal.
    We look up the original signal → find the linked trade → apply the change.
    """

    action = parsed["action"]


    # --- Find the original trade -----------------------------------------

    if not raw.tg_reply_to_id:
        logger.warning("[UPDATE] No reply_to — cannot link to a trade")
        return

    # Step 1: find the RawSignal that this message replies to
    result = await session.execute(
        sa_select(RawSignal).where(
            RawSignal.tg_message_id == raw.tg_reply_to_id
        )
    )
    original_signal = result.scalar_one_or_none()

    if not original_signal:
        logger.warning(
            "[UPDATE] Original signal %s not found in database",
            raw.tg_reply_to_id,
        )
        return

    # Step 2: find the Trade linked to that signal
    result = await session.execute(
        sa_select(Trade).where(Trade.signal_id == original_signal.id)
    )
    trade = result.scalar_one_or_none()

    if not trade:
        logger.warning(
            "[UPDATE] No trade for signal_id=%s", original_signal.id
        )
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
                trade.exit_price = order.get("average") or order.get("price")

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

    1. Parse text with AI → get structured JSON.
    2. Save to database (raw_signals table).
    3. If action is OPEN → execute a trade.
    4. If action is UPDATE/CLOSE → modify the linked trade.
    """

    msg = event.message

    if not msg.text:
        return  # skip images, stickers, etc.


    text = msg.text.strip()

    # Check if this is a reply to an earlier signal
    reply_to = msg.reply_to.reply_to_msg_id if msg.reply_to else None


    # --- 1. AI parse -----------------------------------------------------

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
    We re-parse it and store the new version.

    Note: currently we do NOT modify the linked trade on edit
    (that would be too risky — an edit could be a typo fix).
    Future versions might handle this.
    """

    msg = event.message

    if not msg.text:
        return


    text = msg.text.strip()
    reply_to = msg.reply_to.reply_to_msg_id if msg.reply_to else None


    # --- AI parse --------------------------------------------------------

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


    # --- Store as new raw_signal row (with is_edit=True) -----------------

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
        await session.commit()


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
