import hashlib
import hmac
import time
import logging
from typing import Optional

import aiohttp

import config
from exchanges.base import (
    CEXBase, PriceQuote, OrderBook, OrderBookLevel, RateLimiter,
)

logger = logging.getLogger("ergo_arb.nonkyc")


class NonKYCExchange(CEXBase):
    def __init__(self):
        self.base_url = config.NONKYC_BASE_URL
        self.api_key = config.NONKYC_API_KEY
        self.api_secret = config.NONKYC_API_SECRET
        self.session: Optional[aiohttp.ClientSession] = None
        self._withdraw_fees: dict[str, float] = {}
        self._trading_fees: dict[str, float] = {}
        self._rate_limiter = RateLimiter(max_calls=10, period_seconds=1.0)

    async def connect(self):
        self.session = aiohttp.ClientSession()
        logger.info("[exchange]NonKYC[/exchange] connected")

    async def disconnect(self):
        if self.session:
            await self.session.close()
            self.session = None
        logger.info("NonKYC disconnected")

    def _sign_request(self, url: str, body: str = "") -> dict:
        nonce = str(int(time.time() * 1000))
        message = self.api_key + url + body + nonce
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-API-KEY": self.api_key,
            "X-API-NONCE": nonce,
            "X-API-SIGN": signature,
            "Content-Type": "application/json",
        }

    async def _public_get(self, endpoint: str, params: dict = None) -> Optional[dict | list]:
        if not self.session:
            return None
        await self._rate_limiter.acquire()
        url = f"{self.base_url}/{endpoint}"
        try:
            async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"NonKYC GET {endpoint} returned {resp.status}")
                return None
        except Exception as e:
            logger.error(f"NonKYC GET {endpoint} error: {e}")
            return None

    async def _private_get(self, endpoint: str, params: dict = None) -> Optional[dict | list]:
        if not self.session:
            return None
        await self._rate_limiter.acquire()
        url = f"{self.base_url}/{endpoint}"
        full_url = url
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            full_url = f"{url}?{query}"
        headers = self._sign_request(full_url)
        try:
            async with self.session.get(full_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"NonKYC private GET {endpoint} returned {resp.status}")
                return None
        except Exception as e:
            logger.error(f"NonKYC private GET {endpoint} error: {e}")
            return None

    async def get_price(self, pair: str = "ERG/USDT") -> Optional[PriceQuote]:
        symbol = pair.replace("/", "_")
        data = await self._public_get(f"ticker/{symbol}")
        if not data:
            return None
        try:
            return PriceQuote(
                exchange="NonKYC",
                pair=pair,
                bid=float(data.get("bid", 0)),
                ask=float(data.get("ask", 0)),
                bid_volume=float(data.get("base_volume", 0)),
                ask_volume=float(data.get("base_volume", 0)),
            )
        except (ValueError, KeyError) as e:
            logger.error(f"NonKYC price parse error: {e}")
            return None

    async def get_orderbook(self, pair: str = "ERG/USDT", depth: int = 20) -> Optional[OrderBook]:
        symbol = pair.replace("/", "_")
        data = await self._public_get("market/orderbook", {"symbol": symbol, "limit": depth})
        if not data:
            return None
        try:
            bids = [
                OrderBookLevel(price=float(b["price"]), quantity=float(b["quantity"]))
                for b in data.get("bids", [])
            ]
            asks = [
                OrderBookLevel(price=float(a["price"]), quantity=float(a["quantity"]))
                for a in data.get("asks", [])
            ]
            return OrderBook(
                exchange="NonKYC",
                pair=pair,
                bids=sorted(bids, key=lambda x: x.price, reverse=True),
                asks=sorted(asks, key=lambda x: x.price),
            )
        except (ValueError, KeyError) as e:
            logger.error(f"NonKYC orderbook parse error: {e}")
            return None

    async def get_balance(self, asset: str = "ERG") -> float:
        data = await self._private_get("balances")
        if not data:
            return 0.0
        for item in data:
            name = item.get("asset", item.get("name", item.get("ticker", "")))
            if name.upper() == asset.upper():
                return float(item.get("available", 0))
        return 0.0

    async def get_all_balances(self) -> list[dict]:
        """Get all non-zero balances."""
        data = await self._private_get("balances")
        if not data:
            return []
        results = []
        for item in data:
            available = float(item.get("available", 0))
            held = float(item.get("held", 0))
            pending = float(item.get("pending", 0))
            if available > 0 or held > 0 or pending > 0:
                results.append({
                    "asset": item.get("asset", item.get("name", "?")),
                    "available": available,
                    "held": held,
                    "pending": pending,
                })
        return results

    async def get_withdraw_fee(self, asset: str = "ERG") -> float:
        if asset in self._withdraw_fees:
            return self._withdraw_fees[asset]
        data = await self._public_get("asset/info", {"ticker": asset})
        if data:
            fee = float(data.get("withdrawFee", config.NONKYC_ERG_WITHDRAW_FEE))
            self._withdraw_fees[asset] = fee
            return fee
        return config.NONKYC_ERG_WITHDRAW_FEE

    async def get_trading_fee(self, pair: str = "ERG/USDT") -> float:
        # NonKYC doesn't expose per-pair fee via public API easily
        # Default to configured value
        return config.NONKYC_TRADING_FEE

    async def fetch_erg_usdt_price(self) -> Optional[float]:
        """Convenience method to get ERG price in USDT."""
        quote = await self.get_price("ERG/USDT")
        if quote:
            return quote.mid_price
        return None
