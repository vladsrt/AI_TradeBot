#!/usr/bin/env bash
# ──────────────────────────────────────────────────
# Signal Bot — one-command launcher
# ──────────────────────────────────────────────────
# Usage:
#   ./run.sh dashboard   → start dashboard only (http://localhost:8080)
#   ./run.sh listener    → start Telegram listener (auto-trades signals)
#   ./run.sh all         → start both (dashboard + listener)
#   ./run.sh backfill    → import channel history
#   ./run.sh simulate    → create Trade entries from history
#   ./run.sh test        → run a test trade on Binance Demo
# ──────────────────────────────────────────────────
set -e
cd "$(dirname "$0")"

# Use venv if it exists, else system python
PYTHON=".venv/bin/python"
if [ ! -f "$PYTHON" ]; then
    PYTHON="python3"
fi

case "${1:-dashboard}" in

    dashboard)
        echo "→ Starting dashboard on http://localhost:8080"
        "$PYTHON" -m uvicorn app.dashboard.server:app --host 0.0.0.0 --port 8080 --reload
        ;;

    listener)
        echo "→ Starting Telegram listener (Ctrl+C to stop)"
        "$PYTHON" -m app.telegram.listener
        ;;

    all)
        echo "→ Starting dashboard + listener..."
        trap 'kill 0' EXIT
        "$PYTHON" -m uvicorn app.dashboard.server:app --host 0.0.0.0 --port 8080 &
        sleep 2
        "$PYTHON" -m app.telegram.listener &
        wait
        ;;

    backfill)
        echo "→ Importing channel history (last 200 messages)..."
        "$PYTHON" scripts/backfill.py
        ;;

    simulate)
        echo "→ Simulating trades from history..."
        "$PYTHON" scripts/simulate_trades.py
        echo "→ Done. Check dashboard: http://localhost:8080"
        ;;

    test)
        echo "→ Running test trade on Binance Demo..."
        "$PYTHON" scripts/test_signal.py
        ;;

    docker)
        echo "→ Starting with Docker Compose..."
        docker compose up -d
        echo "→ Dashboard: http://localhost:8080"
        ;;

    *)
        echo "Usage: ./run.sh {dashboard|listener|all|backfill|simulate|test|docker}"
        exit 1
        ;;
esac
