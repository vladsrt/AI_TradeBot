"""
Real-time trading statistics dashboard.

FastAPI + static HTML with JS polling for real-time stats.
"""
from __future__ import annotations

import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select as sa_select

from app.db.session import async_session
from app.db.models import Trade, TradeStatus, RawSignal


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Signal Bot Dashboard")

DASHBOARD_HTML = (
    Path(__file__).parent / "templates" / "dashboard.html"
).read_text()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

async def get_stats():
    """Pull all trade stats from SQLite in one shot."""

    async with async_session() as session:

        # --- All trades with status counts ---
        total_result = await session.scalar(
            sa_select(func.count()).select_from(Trade)
        )

        opened_result = await session.scalar(
            sa_select(func.count()).where(Trade.status == TradeStatus.OPEN)
        )

        error_result = await session.scalar(
            sa_select(func.count()).where(Trade.status == TradeStatus.ERROR)
        )

        # --- PnL from closed trades ---
        result = await session.execute(
            sa_select(Trade).where(
                Trade.status == TradeStatus.CLOSED,
                Trade.pnl_amount.isnot(None),
            ).order_by(Trade.exit_time.desc())
        )
        closed_trades = result.scalars().all()

        # --- Win rate ---
        wins     = [t for t in closed_trades if (t.pnl_amount or 0) > 0]
        losses   = [t for t in closed_trades if (t.pnl_amount or 0) < 0]
        breakeven = [t for t in closed_trades if (t.pnl_amount or 0) == 0]

        win_rate = (
            (len(wins) / len(closed_trades) * 100)
            if closed_trades else 0
        )

        total_pnl     = sum(t.pnl_amount or 0 for t in closed_trades)
        total_pnl_pct = sum(t.pnl_percent or 0 for t in closed_trades)

        best  = max(closed_trades, key=lambda t: t.pnl_amount or 0, default=None)
        worst = min(closed_trades, key=lambda t: t.pnl_amount or 0, default=None)

        # --- Per-pair breakdown ---
        pair_data = {}
        for t in closed_trades:
            pair = t.pair
            if pair not in pair_data:
                pair_data[pair] = {"trades": 0, "wins": 0, "pnl": 0.0}
            pair_data[pair]["trades"] += 1
            pair_data[pair]["pnl"]   += t.pnl_amount or 0
            if (t.pnl_amount or 0) > 0:
                pair_data[pair]["wins"] += 1

        pairs_list = []
        for pair, data in sorted(
            pair_data.items(), key=lambda x: x[1]["pnl"], reverse=True
        ):
            pairs_list.append({
                "pair":     pair,
                "trades":   data["trades"],
                "wins":     data["wins"],
                "win_rate": round(
                    data["wins"] / data["trades"] * 100, 1
                ) if data["trades"] else 0,
                "pnl":      round(data["pnl"], 2),
            })

        # --- Avg latency ---
        avg_latency = await session.scalar(
            sa_select(func.avg(RawSignal.parse_latency_ms)).where(
                RawSignal.parse_success == True
            )
        )

        # --- Recent signals (last 20) ---
        result = await session.execute(
            sa_select(RawSignal).order_by(
                RawSignal.received_at.desc()
            ).limit(20)
        )
        recent_signals = result.scalars().all()

        recent_list = []
        for s in recent_signals:
            text_truncated = s.raw_text
            if len(text_truncated) > 120:
                text_truncated = text_truncated[:120] + "..."

            recent_list.append({
                "id":      s.tg_message_id,
                "action":  s.action,
                "text":    text_truncated,
                "time": (
                    s.received_at.strftime("%Y-%m-%d %H:%M")
                    if s.received_at else "?"
                ),
                "latency": round(s.parse_latency_ms, 0)
                           if s.parse_latency_ms else 0,
            })

        # --- Best / worst ---
        best_trade = None
        if best:
            best_trade = {
                "pair":    best.pair,
                "pnl":     round(best.pnl_amount, 2),
                "pnl_pct": round(best.pnl_percent, 2),
            }

        worst_trade = None
        if worst:
            worst_trade = {
                "pair":    worst.pair,
                "pnl":     round(worst.pnl_amount, 2),
                "pnl_pct": round(worst.pnl_percent, 2),
            }

        # --- Build final dict ---
        return {
            "total_trades":   total_result or 0,
            "open_trades":    opened_result or 0,
            "closed_trades":  len(closed_trades),
            "error_trades":   error_result or 0,
            "wins":           len(wins),
            "losses":         len(losses),
            "breakeven":      len(breakeven),
            "win_rate":       round(win_rate, 1),
            "total_pnl":      round(total_pnl, 2),
            "total_pnl_pct":  round(total_pnl_pct, 2),
            "best_trade":     best_trade,
            "worst_trade":    worst_trade,
            "pairs":          pairs_list,
            "avg_latency_ms": round(avg_latency, 0) if avg_latency else 0,
            "recent_signals": recent_list,
            "generated_at":   datetime.datetime.utcnow().strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            ),
        }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Main dashboard page — static HTML, JS fetches /api/stats."""

    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/api/stats")
async def api_stats():
    """JSON API — for JS polling."""
    return await get_stats()
