"""
Async Binance Futures Demo broker.

Uses raw REST calls (httpx) — no SDK, no CCXT.
Endpoints: https://demo-fapi.binance.com.

Features:
- Market and limit orders
- Cancel orders (limit orders that didn't fill)
- Position tracking (SL/TP in DB, not on exchange — demo limitation)
- Live unrealized PnL polling
- Open positions with mark price
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


    async def get_symbol_info(self, symbol: str) -> Optional[dict]:
        """Get LOT_SIZE filter for a symbol. Returns minQty, stepSize, minNotional."""
        try:
            data = await self._get("/fapi/v1/exchangeInfo", signed=False)
            for s in data.get("symbols", []):
                if s["symbol"] == symbol:
                    for f in s["filters"]:
                        if f["filterType"] == "LOT_SIZE":
                            return {
                                "minQty": float(f["minQty"]),
                                "stepSize": float(f["stepSize"]),
                            }
                        if f["filterType"] == "MIN_NOTIONAL":
                            return_val = {}  # will merge later
                    # Combine filters
                    filters = {}
                    for f in s["filters"]:
                        if f["filterType"] == "LOT_SIZE":
                            filters["minQty"] = float(f["minQty"])
                            filters["stepSize"] = float(f["stepSize"])
                        if f["filterType"] == "MIN_NOTIONAL":
                            filters["minNotional"] = float(f.get("notional", 0))
                    return filters
        except Exception as exc:
            logger.warning("Symbol info fetch failed: %s", exc)
        return None


    @staticmethod
    def round_quantity(qty: float, step_size: float) -> float:
        """Round quantity down to the nearest valid step size."""
        if step_size <= 0:
            return qty
        import math
        precision = int(round(-math.log10(step_size)))
        return math.floor(qty / step_size) * step_size


    async def market_buy(self, symbol: str, quantity: float) -> dict:
        return await self._post("/fapi/v1/order", {
            "symbol": symbol, "side": "BUY",
            "type": "MARKET", "quantity": quantity,
        })


    async def limit_buy(self, symbol: str, price: float, quantity: float) -> dict:
        """Place a GTC limit buy order. Returns order dict with orderId."""
        return await self._post("/fapi/v1/order", {
            "symbol": symbol, "side": "BUY",
            "type": "LIMIT", "quantity": quantity,
            "price": price, "timeInForce": "GTC",
        })


    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        """Cancel a specific order by ID."""
        return await self._delete("/fapi/v1/order", {
            "symbol": symbol, "orderId": order_id,
        })


    async def cancel_all_open_orders(self, symbol: str) -> dict | None:
        try:
            return await self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
        except Exception as exc:
            logger.debug("Cancel orders skipped: %s", exc)
            return None


    async def get_open_orders(self, symbol: str) -> list[dict]:
        """Get all open orders for a symbol."""
        try:
            data = await self._get("/fapi/v1/openOrders", params={"symbol": symbol})
            return data if isinstance(data, list) else []
        except Exception:
            return []


    async def get_order(self, symbol: str, order_id: int) -> Optional[dict]:
        """Get order status by ID."""
        try:
            return await self._get("/fapi/v1/order", params={
                "symbol": symbol, "orderId": order_id,
            })
        except Exception:
            return None


    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Get the current mark price for a symbol (public endpoint, no auth)."""
        try:
            data = await self._get(
                "/fapi/v1/premiumIndex",
                signed=False,
                params={"symbol": symbol},
            )
            return float(data["markPrice"])
        except Exception:
            return None


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


    async def update_sl(self, symbol: str, new_stop: float, quantity: float) -> dict | None:
        """Update stop-loss in database (not on exchange — demo limitation)."""
        logger.info("SL updated (DB): %s @ %s", symbol, new_stop)
        return None


    async def update_tp(self, symbol: str, new_tp: float, quantity: float) -> dict | None:
        """Update take-profit in database."""
        logger.info("TP updated (DB): %s @ %s", symbol, new_tp)
        return None


    async def close_position(self, symbol: str) -> Optional[dict]:
        """Close the current position at market price. Returns order dict with avgPrice."""
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


    async def check_sl_tp(self, symbol: str, sl_price: float, tp_price: float) -> Optional[str]:
        """
        Check if current price has hit SL or TP.
        Returns 'stop_loss', 'take_profit', or None.
        Used because Binance Demo doesn't support conditional orders.
        """
        mark = await self.get_current_price(symbol)
        if mark is None:
            return None
        if mark <= sl_price:
            logger.info("[SL/TP] %s hit STOP at %.6f (mark=%.6f)", symbol, sl_price, mark)
            return "stop_loss"
        if mark >= tp_price:
            logger.info("[SL/TP] %s hit TP at %.6f (mark=%.6f)", symbol, tp_price, mark)
            return "take_profit"
        return None


    async def get_position(self, symbol: str) -> Optional[dict]:
        """Get position info for a specific symbol."""
        data = await self._get("/fapi/v2/positionRisk", params={"symbol": symbol})
        positions = data if isinstance(data, list) else []
        for pos in positions:
            if pos.get("symbol") == symbol:
                return pos
        return None


    async def get_all_positions(self) -> list[dict]:
        """
        Get ALL positions that have non-zero amount.
        Returns list of position dicts with unrealizedPnL.
        """
        try:
            data = await self._get("/fapi/v2/positionRisk")
            positions = data if isinstance(data, list) else []
            return [
                p for p in positions
                if float(p.get("positionAmt", 0)) != 0
            ]
        except Exception as exc:
            logger.error("Failed to get positions: %s", exc)
            return []


    async def get_balance(self) -> dict:
        return await self._get("/fapi/v2/account")


    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None


exchange = BinanceDemoFutures()
