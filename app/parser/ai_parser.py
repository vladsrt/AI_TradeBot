"""
AI-powered signal parser using OpenAI-compatible LLM.
Takes raw Telegram message text → returns structured signal JSON.
"""
from __future__ import annotations
import json
import time
from typing import Optional
from openai import AsyncOpenAI
from app.config import config


SYSTEM_PROMPT = """You are a trading signal parser. Extract structured data from Telegram trading channel messages.

Messages may be:
1. A NEW SIGNAL to open a position (always LONG, never SHORT)
2. An UPDATE referencing a previous position (via reply): move stop-loss, change take-profit, close position

Return ONLY valid JSON. No explanations.

For NEW POSITIONS (action: OPEN):
{
  "action": "OPEN",
  "pair": "DOGEUSDT",          // coin + "USDT" (e.g. "PRL" → "PRLUSDT")
  "direction": "LONG",
  "entry": 0.10401,            // entry price (numeric)
  "entry_type": "LIMIT",       // "MARKET" or "LIMIT"
  "take_profit": 0.15,         // TP price
  "stop_loss": 0.10028,        // SL price
  "leverage": 20,              // default 20 if not specified
  "deposit": 1000,             // mentioned deposit amount or null
  "risk_pct": 1.0,             // risk per trade % or null
  "risk_category": "high"      // "low"/"medium"/"high" if mentioned
}

For UPDATES (reply to a prior signal):
{
  "action": "UPDATE_STOP",     // one of: UPDATE_STOP, UPDATE_TP, PARTIAL_CLOSE, MOVE_TO_BE, CLOSE
  "new_stop": 0.02042,         // new stop-loss price (if applicable)
  "new_tp": null,              // new take-profit price (if applicable)
  "message": "двигаем стоп на 0.02042"  // raw instruction for logging
}

Rules:
- "двигаем стоп на X" or "стоп сдвигаем на X" → UPDATE_STOP with new_stop = X
- "часть фиксируем" or "стоп в бу" → PARTIAL_CLOSE with move_stop_to_breakeven = true
- "стоп в бу" or "breakeven" → MOVE_TO_BE
- "закрываем" / "выходим из позиции" / "фиксируем прибыль" / "close"/"exit" → CLOSE
- "тейк на X" / "профит X" / "TP X" → UPDATE_TP with new_tp = X
- If a coin ticker is given without USDT suffix (e.g. "DOGE"), append "USDT" → "DOGEUSDT"
- Price values: extract as-is (float)
- If leverage not mentioned, default to 20
"""


class SignalParser:
    def __init__(self):
        self.client = AsyncOpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY,
            timeout=8.0,  # fast parse, low timeout
        )

    async def parse(self, text: str, is_reply: bool = False) -> tuple[Optional[dict], float]:
        """
        Parse raw signal text → structured dict.
        Returns (parsed_dict_or_None, latency_ms).
        """
        t0 = time.monotonic()

        extra_prompt = ""
        if is_reply:
            extra_prompt = "\nThis message is a REPLY to a previous position signal. Parse as an UPDATE action."

        try:
            resp = await self.client.chat.completions.create(
                model=config.LLM_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"{text}\n{extra_prompt}"},
                ],
                temperature=0.0,
                max_tokens=300,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content.strip()
            parsed = json.loads(raw)
            latency_ms = (time.monotonic() - t0) * 1000
            return parsed, latency_ms

        except Exception as e:
            latency_ms = (time.monotonic() - t0) * 1000
            return None, latency_ms


# Singleton
parser = SignalParser()
