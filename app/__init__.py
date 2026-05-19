"""
Signal Bot — Telegram VIP channel signal tracker + auto-trading (demo).
Architecture: Telethon (listener) → AI Parser (LLM) → CCXT (exchange) → PostgreSQL (stats)
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
