"""
Export channel history — import past messages for analysis.

Run this BEFORE the listener to fill the database with old signals.
The listener will then only catch new messages going forward.

Usage:
    .venv/bin/python scripts/backfill.py
"""
from __future__ import annotations

import asyncio
import logging
import datetime
from telethon import TelegramClient
from sqlalchemy import select as sa_select

from app.config import config
from app.parser.ai_parser import parser as ai_parser
from app.db.session import async_session, init_db
from app.db.models import RawSignal

logger = logging.getLogger("backfill")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ---------------------------------------------------------------------------
# Main function — fetch and parse
# ---------------------------------------------------------------------------


async def backfill(limit: int = 200):
    """
    Fetch the last `limit` messages from the Telegram channel,
    parse each one with AI, and store them in the database.
    """

    # --- Initialize database ---

    await init_db()


    # --- Connect to Telegram ---

    tg = TelegramClient(
        "session/shark_vip",
        config.TG_API_ID,
        config.TG_API_HASH,
    )
    await tg.start(phone=config.TG_PHONE)


    # --- Find the channel by name ---

    channel = None
    async for dialog in tg.iter_dialogs():
        if dialog.name and config.TG_CHANNEL_NAME.lower() in dialog.name.lower():
            channel = dialog.entity
            break

    if not channel:
        raise RuntimeError(
            f"Channel '{config.TG_CHANNEL_NAME}' not found in your dialogs."
        )

    logger.info(
        "Fetching last %s messages from: %s", limit, channel.title
    )


    # --- Get messages ---

    messages = await tg.get_messages(channel, limit=limit)

    parsed_count = 0
    skipped_count = 0


    # --- Parse and store each message (oldest first) ---

    for msg in reversed(messages):

        # Skip messages without text (images, stickers, etc.)
        if not msg.text:
            skipped_count += 1
            continue

        text = msg.text.strip()

        # Check if this is a reply to another message
        reply_to = (
            msg.reply_to.reply_to_msg_id if msg.reply_to else None
        )

        is_edit = msg.edit_date is not None


        # --- Ask AI to parse the message ---

        parsed, latency = await ai_parser.parse(
            text, is_reply=(reply_to is not None)
        )

        action = parsed.get("action") if parsed else "UNKNOWN"


        # --- Save to database ---

        async with async_session() as session:

            # Skip if we already have this message
            result = await session.execute(
                sa_select(RawSignal).where(
                    RawSignal.tg_message_id == msg.id
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                skipped_count += 1
                continue

            # Create a new signal record
            raw = RawSignal(
                tg_message_id    = msg.id,
                tg_chat_id       = msg.chat_id,
                tg_reply_to_id   = reply_to,
                raw_text         = text,
                parsed_json      = parsed,
                action           = action,
                parse_success    = parsed is not None,
                parse_latency_ms = latency,
                is_edit          = is_edit,
                received_at      = msg.date or datetime.datetime.utcnow(),
            )
            session.add(raw)
            await session.commit()

            parsed_count += 1

            logger.info(
                "[%s/%s] msg=%s action=%s latency=%.0fms",
                parsed_count, len(messages), msg.id, action, latency,
            )


    # --- Done ---

    logger.info(
        "Done! Parsed: %s, Skipped: %s (no text or already in database)",
        parsed_count, skipped_count,
    )

    await tg.disconnect()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    asyncio.run(backfill())
