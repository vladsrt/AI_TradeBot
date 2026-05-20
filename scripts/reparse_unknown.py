"""
Re-parse all UNKNOWN / None-action signals in the database
using the NEW regex parser (no LLM). Creates trades for any
that parse as actionable.

Run: .venv/bin/python scripts/reparse_unknown.py
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import sys

from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select as sa_select, or_

from app.db.session import async_session, init_db
from app.db.models import RawSignal, Trade, TradeLog, TradeStatus
from app.parser.ai_parser import _regex_preparse
from app.telegram.listener import (
    handle_open_signal,
    handle_cancel_order,
    handle_limit_filled,
    handle_update_signal,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("reparse")


async def main():
    await init_db()

    async with async_session() as session:
        # Find signals that are UNKNOWN or have parse_success=False or no action
        result = await session.execute(
            sa_select(RawSignal).where(
                or_(
                    RawSignal.action == "UNKNOWN",
                    RawSignal.action == None,
                    RawSignal.parse_success == False,
                )
            ).order_by(RawSignal.id)
        )
        unknown_signals = result.scalars().all()

        logger.info("Found %d UNKNOWN signals to reparse", len(unknown_signals))

        reprocessed = 0

        for raw in unknown_signals:
            text = raw.raw_text
            is_reply = raw.tg_reply_to_id is not None

            # Try regex
            parsed = _regex_preparse(text, is_reply)

            if parsed is None:
                logger.debug("Still unknown: %s", text[:60])
                continue

            action = parsed.get("action", "UNKNOWN")
            if action == "UNKNOWN":
                continue

            logger.info(
                "REPARSED [%d]: %s → %s",
                raw.id, text[:60], action,
            )

            # Update the raw signal
            raw.parsed_json = parsed
            raw.action = action
            raw.parse_success = True
            raw.parse_latency_ms = 0  # regex is instant

            await session.flush()

            # Execute action
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
                    logger.info(" Unhandled: %s", action)
                    continue

                reprocessed += 1

            except Exception as exc:
                logger.error(" Failed: %s", exc)

        await session.commit()
        logger.info("Done. Reparsed %d/%d signals.", reprocessed, len(unknown_signals))


if __name__ == "__main__":
    asyncio.run(main())
