import base64
import hashlib
import hmac
import json
import time
import logging
from typing import Optional

import aiohttp

import config
from exchanges.base import (
    CEXBase, PriceQuote, OrderBook, OrderBookLevel, RateLimiter,
)

logger = logging.getLogger("ergo_arb.kucoin")

KUCOIN_BASE_URL = "https://api.kucoin.com"


class KucoinExchange(CEXBase):
    def __init__(self):
        self.base_url = KUCOIN_BASE_URL
        self.api_key = config.KUCOIN_API_KEY
        self.api_secret = config.KUCOIN_API_SECRET
        self.passphrase = config.KUCOIN_API_PASSPHRASE
        self.session: Optional[aiohttp.ClientSession] = None
        self._withdraw_fee: Optional[float] = None
        self._trading_fee: Optional[float] = None
        self._rate_limiter = RateLimiter(max_calls=25, period_seconds=1.0)

    async def connect(self):
        self.session = aiohttp.ClientSession()
        if self.api_key:
            logger.info("[exchange]Kucoin[/exchange] connected (authenticated)")
        else:
            logger.info("[exchange]Kucoin[/exchange] connected (public only)")

    async def disconnect(self):
        if self.session:
            await self.session.close()
            self.session = None
        logger.info("Kucoin disconnected")

    def _sign_headers(self, method: str, endpoint: str, body: str = "") -> dict:
        """Generate Kucoin API v2 authentication headers."""
        timestamp = str(int(time.time() * 1000))
        str_to_sign = timestamp + method.upper() + endpoint + body
        signature = base64.b64encode(
            hmac.new(
                self.api_secret.encode("utf-8"),
                str_to_sign.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")

        # Passphrase must also be signed for API key version 2
        passphrase_signed = base64.b64encode(
            hmac.new(
                self.api_secret.encode("utf-8"),
                self.passphrase.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")

        return {
            "KC-API-KEY": self.api_key,
            "KC-API-SIGN": signature,
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-PASSPHRASE": passphrase_signed,
            "KC-API-KEY-VERSION": "2",
            "Content-Type": "application/json",
        }

    async def _public_get(self, endpoint: str) -> Optional[dict]:
        if not self.session:
            return None
        await self._rate_limiter.acquire()
        url = f"{self.base_url}{endpoint}"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == "200000":
                        return data.get("data")
                    logger.warning(f"Kucoin {endpoint}: {data.get('msg', 'unknown error')}")
                    return None
                logger.warning(f"Kucoin GET {endpoint} returned {resp.status}")
                return None
        except Exception as e:
            logger.error(f"Kucoin GET {endpoint} error: {e}")
            return None

    async def _private_get(self, endpoint: str) -> Optional[dict]:
        if not self.session or not self.api_key:
            return None
        await self._rate_limiter.acquire()
        url = f"{self.base_url}{endpoint}"
        headers = self._sign_headers("GET", endpoint)
        try:
            async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == "200000":
                        return data.get("data")
                    logger.warning(f"Kucoin private {endpoint}: {data.get('msg')}")
                    return None
                logger.warning(f"Kucoin private GET {endpoint} returned {resp.status}")
                return None
        except Exception as e:
            logger.error(f"Kucoin private GET {endpoint} error: {e}")
            return None

    async def _private_post(self, endpoint: str, body: dict) -> Optional[dict]:
        if not self.session or not self.api_key:
            return None
        await self._rate_limiter.acquire()
        url = f"{self.base_url}{endpoint}"
        body_str = json.dumps(body)
        headers = self._sign_headers("POST", endpoint, body_str)
        try:
            async with self.session.post(url, headers=headers, data=body_str,
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == "200000":
                        return data.get("data")
                    logger.warning(f"Kucoin POST {endpoint}: {data.get('msg')}")
                    return None
                logger.warning(f"Kucoin POST {endpoint} returned {resp.status}")
                return None
        except Exception as e:
            logger.error(f"Kucoin POST {endpoint} error: {e}")
            return None

    async def get_price(self, pair: str = "ERG/USDT") -> Optional[PriceQuote]:
        symbol = pair.replace("/", "-")
        data = await self._public_get(f"/api/v1/market/orderbook/level1?symbol={symbol}")
        if not data:
            return None
        try:
            return PriceQuote(
                exchange="Kucoin",
                pair=pair,
                bid=float(data.get("bestBid", 0)),
                ask=float(data.get("bestAsk", 0)),
                bid_volume=float(data.get("bestBidSize", 0)),
                ask_volume=float(data.get("bestAskSize", 0)),
            )
        except (ValueError, KeyError) as e:
            logger.error(f"Kucoin price parse error: {e}")
            return None

    async def get_orderbook(self, pair: str = "ERG/USDT", depth: int = 20) -> Optional[OrderBook]:
        symbol = pair.replace("/", "-")
        data = await self._public_get(f"/api/v1/market/orderbook/level2_20?symbol={symbol}")
        if not data:
            return None
        try:
            bids = [
                OrderBookLevel(price=float(b[0]), quantity=float(b[1]))
                for b in data.get("bids", [])
            ]
            asks = [
                OrderBookLevel(price=float(a[0]), quantity=float(a[1]))
                for a in data.get("asks", [])
            ]
            return OrderBook(
                exchange="Kucoin",
                pair=pair,
                bids=sorted(bids, key=lambda x: x.price, reverse=True),
                asks=sorted(asks, key=lambda x: x.price),
            )
        except (ValueError, KeyError, IndexError) as e:
            logger.error(f"Kucoin orderbook parse error: {e}")
            return None

    async def get_balance(self, asset: str = "ERG") -> float:
        data = await self._private_get(f"/api/v1/accounts?currency={asset}&type=trade")
        if not data:
            return 0.0
        if isinstance(data, list) and len(data) > 0:
            return float(data[0].get("available", 0))
        return 0.0

    async def get_all_balances(self) -> list[dict]:
        """Get all non-zero trading account balances."""
        data = await self._private_get("/api/v1/accounts?type=trade")
        if not data or not isinstance(data, list):
            return []
        return [
            {
                "asset": item.get("currency", "?"),
                "available": float(item.get("available", 0)),
                "held": float(item.get("holds", 0)),
            }
            for item in data
            if float(item.get("available", 0)) > 0 or float(item.get("holds", 0)) > 0
        ]

    async def get_withdraw_fee(self, asset: str = "ERG") -> float:
        if not hasattr(self, "_withdraw_fees"):
            self._withdraw_fees = {}
        if asset in self._withdraw_fees:
            return self._withdraw_fees[asset]
        data = await self._public_get(f"/api/v1/currencies/{asset}")
        if data:
            fee = float(data.get("withdrawalMinFee", 0.73))
            self._withdraw_fees[asset] = fee
            return fee
        fallback = 0.73 if asset == "ERG" else 1.0
        return fallback

    async def get_trading_fee(self, pair: str = "ERG/USDT") -> float:
        if self._trading_fee is not None:
            return self._trading_fee
        if not self.api_key:
            return 0.001  # default 0.1%
        symbol = pair.replace("/", "-")
        data = await self._private_get(f"/api/v1/trade-fees?symbols={symbol}")
        if data and isinstance(data, list) and len(data) > 0:
            fee = float(data[0].get("takerFeeRate", 0.001))
            self._trading_fee = fee
            return fee
        return 0.001

    async def get_24h_stats(self) -> Optional[dict]:
        """Get 24h trading stats for ERG/USDT."""
        data = await self._public_get("/api/v1/market/stats?symbol=ERG-USDT")
        if data:
            return {
                "last": float(data.get("last", 0)),
                "high": float(data.get("high", 0)),
                "low": float(data.get("low", 0)),
                "volume_erg": float(data.get("vol", 0)),
                "volume_usdt": float(data.get("volValue", 0)),
                "change_rate": float(data.get("changeRate", 0)),
                "maker_fee": float(data.get("makerFeeRate", 0.001)),
                "taker_fee": float(data.get("takerFeeRate", 0.001)),
            }
        return None

    async def fetch_erg_usdt_price(self) -> Optional[float]:
        """Convenience method to get ERG mid price in USDT."""
        quote = await self.get_price("ERG/USDT")
        if quote:
            return quote.mid_price
        return None

    async def get_deposit_address(self, asset: str = "ERG") -> Optional[dict]:
        """Get deposit address for an asset (requires auth)."""
        data = await self._private_get(f"/api/v1/deposit-addresses?currency={asset}")
        if data:
            return data
        # If no address exists, create one
        data = await self._private_post("/api/v1/deposit-addresses", {"currency": asset})
        return data

    async def get_deposits(self, asset: str = "ERG", limit: int = 10) -> Optional[list]:
        """Get recent deposits."""
        data = await self._private_get(f"/api/v1/deposits?currency={asset}&pageSize={limit}")
        if data and isinstance(data, dict):
            return data.get("items", [])
        return None

    async def get_withdrawals(self, asset: str = "ERG", limit: int = 10) -> Optional[list]:
        """Get recent withdrawals."""
        data = await self._private_get(f"/api/v1/withdrawals?currency={asset}&pageSize={limit}")
        if data and isinstance(data, dict):
            return data.get("items", [])
        return None
