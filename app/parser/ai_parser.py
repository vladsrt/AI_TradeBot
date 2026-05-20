"""
AI-powered signal parser using OpenAI-compatible LLM.

Takes raw Telegram message text → returns structured signal JSON.
Includes a regex-based fallback for when the LLM is slow or unavailable.
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError

from app.config import config


# ---------------------------------------------------------------------------
# System prompt for the LLM
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """You are a trading signal parser. Extract structured data from Telegram messages.

Messages may be:
1. A NEW SIGNAL to open a position (always LONG, never SHORT)
2. An UPDATE referencing a previous position (via reply): move stop-loss, change take-profit, close

Return ONLY valid JSON. No explanations. No markdown.

For NEW POSITIONS (action: OPEN):
{
  "action": "OPEN",
  "pair": "DOGEUSDT",
  "direction": "LONG",
  "entry": 0.10401,
  "entry_type": "MARKET",
  "take_profit": 0.15,
  "stop_loss": 0.10028,
  "leverage": 20,
  "deposit": null,
  "risk_pct": null,
  "risk_category": null
}

For UPDATES (reply to a prior signal):
{
  "action": "UPDATE_STOP",
  "new_stop": 0.02042,
  "new_tp": null,
  "message": "short description"
}

Rules:
- Coin ticker + "USDT" suffix: DOGE → DOGEUSDT, PEPE → PEPEUSDT
- "1000PEPE" → pair is "1000PEPEUSDT" (or "PEPEUSDT")
- "двигаем стоп на X" → UPDATE_STOP, new_stop = X
- "часть фиксируем" → PARTIAL_CLOSE, move_stop_to_breakeven = true
- "стоп в безубыток" / "стоп в бу" → MOVE_TO_BE
- "закрываем" / "фиксируем прибыль" / "close" / "✅" with "фиксируем" → CLOSE
- "тейк на X" / "профит X" → UPDATE_TP, new_tp = X
- If leverage not mentioned, default to 20
"""


# ---------------------------------------------------------------------------
# Regex-based pre-parser — catches obvious patterns without LLM
# ---------------------------------------------------------------------------


# Pattern for new position signals: #TICKER LONG ... Вход: PRICE ... ТР: PRICE ... Stop: PRICE
OPEN_PATTERN = re.compile(
    r'#(?P<raw_pair>[A-Za-z0-9]+)\s+LONG.*?'
    r'Вход:\s*(?P<entry>[\d.]+)\s*(?:\((?P<entry_type>MARKET|LIMIT)[^)]*\))?.*?'
    r'(?:T[pP]|Т[рР])\s*:?\s*(?P<tp>[\d.]+).*?'
    r'Stop\s*:?\s*(?P<sl>[\d.]+).*?'
    r'(?:Плечо\s*:?\s*(?P<lev>\d+)х)?',
    re.DOTALL | re.IGNORECASE,
)

# Pattern for deposit mention
DEPOSIT_PATTERN = re.compile(
    r'Депозит\s+(?P<deposit>[\d\s]+)\$',
    re.IGNORECASE,
)

# Pattern for risk mention
RISK_PATTERN = re.compile(
    r'Риск\s+на\s+сделку\s+(?P<risk>[\d.]+)\s*%',
    re.IGNORECASE,
)

# Pattern for stop move: "стоп сдвигаем на X" / "стоп на X"
STOP_UPDATE_PATTERN = re.compile(
    r'(?:стоп|stop)\s+(?:сдвигаем|двигаем|передвигаем|на)\s+(?:на\s+)?(?P<price>[\d.]+)',
    re.IGNORECASE,
)

# Pattern for close / fix profit
CLOSE_PATTERN = re.compile(
    r'(?:полностью\s+)?(?:фиксируем|закрываем|зафиксировал|close|выходим|fixed)',
    re.IGNORECASE,
)

# Pattern for partial fix
PARTIAL_PATTERN = re.compile(
    r'часть\s+(?:фиксируем|можно\s+зафиксировать)',
    re.IGNORECASE,
)

# Pattern for breakeven
BE_PATTERN = re.compile(
    r'стоп\s+(?:в\s+)?(?:безубыток|бу|breakeven)',
    re.IGNORECASE,
)

# Pattern for cancel order
CANCEL_PATTERN = re.compile(
    r'(?:отменяем|отменить|отмена|cancel)\s+(?:лимитный\s+)?(?:ордер|заявку|order)',
    re.IGNORECASE,
)

# Pattern for limit order filled / activate
LIMIT_FILLED_PATTERN = re.compile(
    r'(?:лимит(?:ный|ка|ный ордер)?|limit)\s+(?:заш(?:ёл|ел|ла)|активировался|сработал|filled|исполнился)',
    re.IGNORECASE,
)

# Pattern for take-profit signal: "тейк на X" / "ТР X" / "TP: X"
TP_UPDATE_PATTERN = re.compile(
    r'(?:т[еэ]йк|tp|T[pP])\s+(?:на\s+)?(?P<price>[\d.]+)',
    re.IGNORECASE,
)

# Pattern for deposit mention — more variants
DEPOSIT_PATTERN = re.compile(
    r'(?:Депозит|деп|deposit)\s+(?P<deposit>[\d\s]+)\s*\$',
    re.IGNORECASE,
)

# Pattern for risk mention — more variants
RISK_PATTERN = re.compile(
    r'(?:Риск|risk)\s+(?:на\s+сделку\s+)?(?P<risk>[\d.]+)\s*%',
    re.IGNORECASE,
)

# Pattern for leverage mention: "плечо X" / "Xx" / "leverage X"
LEVERAGE_PATTERN = re.compile(
    r'(?:плечо|lev(?:erage)?)\s*:?\s*(?P<lev>\d+)\s*x?',
    re.IGNORECASE,
)


def _regex_preparse(text: str, is_reply: bool) -> Optional[dict]:
    """
    Try to parse the signal with regex before calling the LLM.
    Catches ~95% of signals instantly with zero LLM cost.
    Returns None if regex couldn't match — caller should fall back to LLM.
    """

    text_clean = text.replace('\n', ' ').replace('\r', ' ')

    # --- Cancel order? → CANCEL (not UNKNOWN) ---

    if CANCEL_PATTERN.search(text):
        return {"action": "CANCEL_ORDER", "message": text[:80]}


    # --- Limit filled? → OPEN if not already ---

    if LIMIT_FILLED_PATTERN.search(text):
        return {"action": "LIMIT_FILLED", "message": text[:80]}


    # --- Close signal? ---

    if CLOSE_PATTERN.search(text) and not PARTIAL_PATTERN.search(text):
        return {"action": "CLOSE", "message": text[:80]}


    # --- Partial close? ---

    if PARTIAL_PATTERN.search(text):
        be = BE_PATTERN.search(text) is not None
        return {
            "action": "PARTIAL_CLOSE",
            "move_stop_to_breakeven": be,
            "message": text[:80],
        }


    # --- Breakeven? ---

    if BE_PATTERN.search(text) and "LONG" not in text.upper():
        return {"action": "MOVE_TO_BE", "message": text[:80]}


    # --- Stop update? ---

    m = STOP_UPDATE_PATTERN.search(text)
    if m and "LONG" not in text.upper():
        return {
            "action": "UPDATE_STOP",
            "new_stop": float(m.group("price")),
            "message": text[:80],
        }


    # --- TP update? ---

    m = TP_UPDATE_PATTERN.search(text)
    if m and "LONG" not in text.upper():
        return {
            "action": "UPDATE_TP",
            "new_tp": float(m.group("price")),
            "message": text[:80],
        }


    # --- Open signal? ---

    m = OPEN_PATTERN.search(text)
    if not m:
        return None  # let LLM handle it


    raw_pair = m.group("raw_pair").upper()

    # Handle special tickers: 1000PEPE → 1000PEPEUSDT, PEPE → PEPEUSDT
    pair = raw_pair
    if not pair.endswith("USDT"):
        pair = f"{pair}USDT"


    entry = float(m.group("entry"))

    entry_type_raw = m.group("entry_type")
    entry_type = "MARKET" if not entry_type_raw or entry_type_raw.upper() == "MARKET" else "LIMIT"

    tp = float(m.group("tp"))
    sl = float(m.group("sl"))

    # Try leverage from the OPEN pattern first, then from separate LEVERAGE_PATTERN
    lev_raw = m.group("lev")
    if not lev_raw:
        lm = LEVERAGE_PATTERN.search(text)
        lev_raw = lm.group("lev") if lm else None
    lev = int(lev_raw) if lev_raw else 20


    # Extract deposit
    dm = DEPOSIT_PATTERN.search(text)
    deposit = float(dm.group("deposit").replace(" ", "")) if dm else None


    # Extract risk
    rm = RISK_PATTERN.search(text)
    risk_pct = float(rm.group("risk")) if rm else None


    return {
        "action": "OPEN",
        "pair": pair,
        "direction": "LONG",
        "entry": entry,
        "entry_type": entry_type,
        "take_profit": tp,
        "stop_loss": sl,
        "leverage": lev,
        "deposit": deposit,
        "risk_pct": risk_pct,
        "risk_category": "high" if lev >= 20 else None,
    }


# ---------------------------------------------------------------------------
# SignalParser — regex first, LLM fallback
# ---------------------------------------------------------------------------


class SignalParser:

    MAX_RETRIES = 2


    def __init__(self):
        self.client = AsyncOpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY,
            timeout=15.0,  # increased from 8s — Ollama Cloud can be slow
        )


    async def parse(
        self, text: str, is_reply: bool = False
    ) -> tuple[Optional[dict], float]:
        """
        Parse raw signal text → structured dict.

        1. Try regex (fast, free, no API call).
        2. If regex fails, call the LLM with retries.

        Returns (parsed_dict_or_None, latency_ms).
        """

        t0 = time.monotonic()


        # --- Step 1: Try regex ---

        regex_result = _regex_preparse(text, is_reply)

        if regex_result is not None:
            latency_ms = (time.monotonic() - t0) * 1000
            return regex_result, latency_ms


        # --- Step 2: LLM with retries ---

        extra = ""
        if is_reply:
            extra = "\nThis message is a REPLY. Parse as an UPDATE action."

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                resp = await self.client.chat.completions.create(
                    model=config.LLM_MODEL,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"{text}\n{extra}"},
                    ],
                    temperature=0.0,
                    max_tokens=300,
                    # Note: no response_format — not all Ollama models support it
                )

                raw = resp.choices[0].message.content.strip()

                # Sometimes LLM wraps JSON in markdown code blocks
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()

                parsed = json.loads(raw)
                latency_ms = (time.monotonic() - t0) * 1000
                return parsed, latency_ms

            except (json.JSONDecodeError, KeyError) as e:
                # Bad JSON — retry
                if attempt < self.MAX_RETRIES:
                    extra += "\nPrevious response was invalid JSON. Return ONLY valid JSON."
                    continue
                latency_ms = (time.monotonic() - t0) * 1000
                return None, latency_ms

            except (APITimeoutError, RateLimitError) as e:
                if attempt < self.MAX_RETRIES:
                    extra += "\nTry again."
                    continue
                latency_ms = (time.monotonic() - t0) * 1000
                return None, latency_ms

            except APIError as e:
                # Non-retryable API error
                latency_ms = (time.monotonic() - t0) * 1000
                return None, latency_ms

            except Exception as e:
                latency_ms = (time.monotonic() - t0) * 1000
                return None, latency_ms

        latency_ms = (time.monotonic() - t0) * 1000
        return None, latency_ms


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


parser = SignalParser()
