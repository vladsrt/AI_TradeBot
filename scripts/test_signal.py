"""
Quick test trade — opens a real position on Binance Demo.

This is a standalone script. It does NOT read from Telegram.
Use it to verify that the exchange connection and order pipeline work.

Run:
    .venv/bin/python scripts/test_signal.py
"""
from __future__ import annotations

import asyncio
import datetime

from app.exchange.broker import exchange
from app.db.session import async_session, init_db
from app.db.models import RawSignal, Trade, TradeLog, TradeStatus


# ---------------------------------------------------------------------------
# The test signal — a fake DOGE LONG, same format as the real channel
# ---------------------------------------------------------------------------


SIGNAL_TEXT = """#DOGE LONG
Плечо: 20х
Вход: 0.10401 (MARKET)
ТР: 0.15
Stop: 0.10028"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    """Open one test position and save it to the database."""

    await init_db()
    await exchange.connect()

    # --- Trade parameters ---

    pair  = "DOGEUSDT"
    entry = 0.10401
    sl    = 0.10028
    tp    = 0.15
    qty   = 50  # minimum viable quantity for demo

    print(f">>> Opening test trade: {pair} x20, qty={qty} <<<")


    # --- Execute on exchange ---

    await exchange.set_leverage(pair, 20)
    order = await exchange.market_buy(pair, qty)

    print(f"  Order: {order.get('orderId', '?')}")


    # --- SL and TP (tracked in DB only — demo limitation) ---

    await exchange.place_stop_loss(pair, sl, qty)
    await exchange.place_take_profit(pair, tp, qty)


    # --- Save to database ---

    async with async_session() as session:

        # Create a signal record
        raw = RawSignal(
            tg_message_id    = 99999,          # fake ID, not from real Telegram
            tg_chat_id       = 0,
            raw_text         = SIGNAL_TEXT,
            parsed_json      = {
                "pair": pair,
                "entry": entry,
                "stop_loss": sl,
                "take_profit": tp,
            },
            action            = "OPEN",
            parse_success     = True,
            parse_latency_ms  = 3500,
            received_at       = datetime.datetime.utcnow(),
        )
        session.add(raw)
        await session.flush()


        # Create a trade linked to the signal
        trade = Trade(
            signal_id          = raw.id,
            pair               = pair,
            direction          = "LONG",
            leverage           = 20,
            planned_entry      = entry,
            planned_stop       = sl,
            planned_tp         = tp,
            entry_price        = entry,
            entry_quantity     = qty,
            entry_time         = datetime.datetime.utcnow(),
            current_stop       = sl,
            current_tp         = tp,
            status             = TradeStatus.OPEN,
        )
        session.add(trade)
        await session.flush()


        # Log the action
        log = TradeLog(
            trade_id    = trade.id,
            action      = "OPEN",
            detail_json = {"test": True},
        )
        session.add(log)
        await session.commit()

        print(f"  Trade #{trade.id} saved to database — STATUS: OPEN")


    # --- Show current position ---

    pos = await exchange.get_position(pair)
    amt = pos.get("positionAmt", "0") if pos else "0"
    print(f"  Exchange position: {amt} {pair}")

    await exchange.close()
    print(f">>> Done. Check dashboard: http://localhost:8080 <<<")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    asyncio.run(main())
