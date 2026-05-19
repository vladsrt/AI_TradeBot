"""
Application configuration.

Loads settings from a .env file at the project root.
Every value has a sensible default, so the app can start
without any environment variables set.
"""
import os
from pathlib import Path
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Load .env file — must happen before reading any values
# ---------------------------------------------------------------------------


load_dotenv(Path(__file__).parent.parent / ".env")


# ---------------------------------------------------------------------------
# Config class — all settings in one place
# ---------------------------------------------------------------------------


class Config:
    """
    Central configuration holder.
    Access via `from app.config import config`.
    """

    # --- Telegram userbot ------------------------------------------------

    TG_API_ID: int = int(os.getenv("TG_API_ID", "0"))

    TG_API_HASH: str = os.getenv("TG_API_HASH", "")

    TG_PHONE: str = os.getenv("TG_PHONE", "")

    TG_CHANNEL_NAME: str = os.getenv(
        "TG_CHANNEL_NAME", "SHARK|VIP 🔒"
    )

    # --- AI signal parser (Ollama Cloud) ---------------------------------

    LLM_BASE_URL: str = os.getenv(
        "LLM_BASE_URL", "https://ollama.com/v1"
    )

    LLM_API_KEY: str = os.getenv(
        "LLM_API_KEY",
        "872a0ea596964dc3bfad3db0a27149d5.2VlHAwgtQQ4908H0KFuAFfBs",
    )

    LLM_MODEL: str = os.getenv(
        "LLM_MODEL", "deepseek-v4-flash"
    )

    # --- Binance Futures Demo (testnet) ----------------------------------

    BINANCE_API_KEY: str = os.getenv("BINANCE_API_KEY", "")

    BINANCE_API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")

    # --- Database (SQLite — zero setup) ----------------------------------

    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "sqlite+aiosqlite:///signal_bot.db",
    )

    DATABASE_ECHO: bool = (
        os.getenv("DATABASE_ECHO", "false").lower() == "true"
    )

    # --- Trading defaults ------------------------------------------------

    DEFAULT_DEPOSIT: float = float(
        os.getenv("DEFAULT_DEPOSIT", "1000")
    )

    DEFAULT_RISK_PERCENT: float = float(
        os.getenv("DEFAULT_RISK_PERCENT", "1.0")
    )

    DEFAULT_LEVERAGE: int = int(
        os.getenv("DEFAULT_LEVERAGE", "20")
    )


# ---------------------------------------------------------------------------
# Singleton instance
# ---------------------------------------------------------------------------


config = Config()
