"""
Async Binance Futures Demo broker.

Uses raw REST calls (httpx) — no SDK, no CCXT.
Endpoints: https://demo-fapi.binance.com.

Important: the Binance Demo environment does NOT support STOP_MARKET
or TAKE_PROFIT order types. SL/TP values are tracked in the database
instead. When a CLOSE signal arrives, the position is closed at market
and PnL is calculated against the tracked SL/TP.
"""
from __future__ import annotations

import hmac
import hashlib
import time
import logging
import urllib.parse
from typing import Optional

import httpx

from app.config import config

logger = logging.getLogger("binance-broker")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


BASE_URL = "https://demo-fapi.binance.com"

API_HEADERS = {"X-MBX-APIKEY": config.BINANCE_API_KEY}


def _sign(params: dict) -> str:
    qs = urllib.parse.urlencode(params)
    return hmac.new(
        config.BINANCE_API_SECRET.encode(),
        qs.encode(),
        hashlib.sha256,
    ).hexdigest()


def _signed_params(extra: dict = None) -> dict:
    params = {"timestamp": int(time.time() * 1000)}
    if extra:
        params.update(extra)
    params["signature"] = _sign(params)
    return params


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class BinanceDemoFutures:
    """Async client for Binance Futures Demo (testnet)."""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None


    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client


    async def _get(self, path: str, signed: bool = True, params: dict = None) -> dict:
        client = await self._get_client()
        query = _signed_params(params) if signed else (params or {})
        url = f"{BASE_URL}{path}"
        resp = await client.get(url, params=query, headers=API_HEADERS)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("code", 0) < 0:
            raise RuntimeError(f"Binance error: {data}")
        return data


    async def _post(self, path: str, params: dict) -> dict:
        client = await self._get_client()
        query = _signed_params(params)
        url = f"{BASE_URL}{path}"
        resp = await client.post(url, params=query, headers=API_HEADERS)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("code", 0) < 0:
            raise RuntimeError(f"Binance error: {data}")
        return data


    async def _delete(self, path: str, params: dict = None) -> dict:
        client = await self._get_client()
        query = _signed_params(params) if params else _signed_params({})
        url_parts = urllib.parse.urlparse(f"{BASE_URL}{path}")
        url = f"{BASE_URL}{path}?{urllib.parse.urlencode(query)}"
        resp = await client.delete(url, headers=API_HEADERS)
        resp.raise_for_status()
        return resp.json()


    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------


    async def connect(self) -> bool:
        try:
            data = await self._get("/fapi/v2/account")
            balance = data.get("totalWalletBalance", "?")
            logger.info("Binance Demo connected. Balance: %s USDT", balance)
            return True
        except Exception as exc:
            logger.error("Binance connection failed: %s", exc)
            return False


    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self._post("/fapi/v1/leverage", {
            "symbol": symbol, "leverage": leverage,
        })


    async def market_buy(self, symbol: str, quantity: float) -> dict:
        return await self._post("/fapi/v1/order", {
            "symbol": symbol, "side": "BUY",
            "type": "MARKET", "quantity": quantity,
        })


    async def limit_buy(self, symbol: str, price: float, quantity: float) -> dict:
        return await self._post("/fapi/v1/order", {
            "symbol": symbol, "side": "BUY",
            "type": "LIMIT", "quantity": quantity,
            "price": price, "timeInForce": "GTC",
        })


    async def place_stop_loss(self, symbol: str, stop_price: float, quantity: float) -> dict | None:
        """
        Place a stop-loss order. On Binance Demo this endpoint is NOT supported.
        We track the SL value in the database instead.
        """
        logger.info(
            "SL tracked (not placed on exchange — demo limitation): %s @ %s",
            symbol, stop_price,
        )
        return None  # not an error, just not supported on demo


    async def place_take_profit(self, symbol: str, price: float, quantity: float) -> dict | None:
        """
        Place a take-profit order. Same limitation as stop-loss on demo.
        """
        logger.info(
            "TP tracked (not placed on exchange — demo limitation): %s @ %s",
            symbol, price,
        )
        return None


    async def cancel_all_open_orders(self, symbol: str) -> dict | None:
        try:
            return await self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
        except Exception as exc:
            logger.debug("Cancel orders skipped: %s", exc)
            return None


    async def update_sl(self, symbol: str, new_stop: float, quantity: float) -> dict | None:
        """Update stop-loss in database (not on exchange — demo limitation)."""
        logger.info("SL updated (DB): %s @ %s", symbol, new_stop)
        return None


    async def update_tp(self, symbol: str, new_tp: float, quantity: float) -> dict | None:
        """Update take-profit in database."""
        logger.info("TP updated (DB): %s @ %s", symbol, new_tp)
        return None


    async def close_position(self, symbol: str) -> Optional[dict]:
        """Close the current position at market price."""
        pos = await self.get_position(symbol)
        if pos and float(pos.get("positionAmt", 0)) > 0:
            amount = abs(float(pos["positionAmt"]))
            try:
                await self.cancel_all_open_orders(symbol)
            except Exception:
                pass
            return await self._post("/fapi/v1/order", {
                "symbol": symbol, "side": "SELL",
                "type": "MARKET", "quantity": amount,
                "reduceOnly": "true",
            })
        return None


    async def get_position(self, symbol: str) -> Optional[dict]:
        data = await self._get("/fapi/v2/positionRisk", params={"symbol": symbol})
        positions = data if isinstance(data, list) else []
        for pos in positions:
            if pos.get("symbol") == symbol:
                return pos
        return None


    async def get_balance(self) -> dict:
        return await self._get("/fapi/v2/account")


    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None


exchange = BinanceDemoFutures()
