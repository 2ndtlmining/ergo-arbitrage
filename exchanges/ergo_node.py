import asyncio
import logging
from typing import Optional

import aiohttp

import config

logger = logging.getLogger("ergo_arb.ergo_node")


def parse_wallet_balances(data) -> Optional[dict]:
    """/wallet/balances as {"erg", "tokens": {id: {"amount"}}}, or None for a shape this bot does not know:
    an unknown shape must read as "unreadable", never as a wallet without SigUSD."""
    if not isinstance(data, dict) or not isinstance(data.get("balance"), (int, float)):
        return None
    assets = data.get("assets", {})
    if isinstance(assets, dict):
        tokens = {tid: {"amount": amount} for tid, amount in assets.items()}
    elif isinstance(assets, list) and all(isinstance(a, dict) and "tokenId" in a and "amount" in a for a in assets):
        tokens = {a["tokenId"]: {"amount": a["amount"]} for a in assets}
    else:
        return None
    return {"erg": data["balance"] / 1e9, "tokens": tokens}

MAX_HEADER_LAG = 3  # blocks between headersHeight and fullHeight to count as synced


class ErgoNodeClient:
    def __init__(self):
        self.base_url = config.ERGO_NODE_URL
        self.api_key = config.ERGO_NODE_API_KEY
        self.session: Optional[aiohttp.ClientSession] = None
        self._failing: Optional[str] = None   # set during an outage: one log line per outage, not per request

    async def connect(self):
        self.session = aiohttp.ClientSession(
            headers={"api_key": self.api_key, "Content-Type": "application/json"}
        )
        logger.info(f"Ergo node client connected to {self.base_url}")

    async def disconnect(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def _get(self, endpoint: str) -> Optional[dict | list]:
        if not self.session:
            return None
        url = f"{self.base_url}{endpoint}"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    self._recovered()
                    return await resp.json()
                logger.warning(f"Node GET {endpoint} returned {resp.status}")
                return None
        except Exception as e:
            self._failed(f"Node GET {endpoint} error: {e}")
            return None

    async def _post(self, endpoint: str, data: dict | list = None) -> Optional[dict | list]:
        if not self.session:
            return None
        url = f"{self.base_url}{endpoint}"
        try:
            async with self.session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    self._recovered()
                    return await resp.json()
                logger.warning(f"Node POST {endpoint} returned {resp.status}")
                return None
        except Exception as e:
            self._failed(f"Node POST {endpoint} error: {e}")
            return None

    def _failed(self, message: str):
        if self._failing is None:
            logger.error(f"{message} (further node errors are logged at debug level until it answers again)")
        else:
            logger.debug(message)
        self._failing = self._failing or message

    def _recovered(self):
        if self._failing is not None:
            logger.info("Ergo node answering again")
            self._failing = None

    async def get_wallet_status(self) -> Optional[dict]:
        return await self._get("/wallet/status")

    async def is_wallet_unlocked(self) -> bool:
        status = await self.get_wallet_status()
        if status:
            return status.get("isUnlocked", False)
        return False

    async def get_wallet_balances(self) -> Optional[dict]:
        """Get wallet balance in ERG and tokens."""
        data = await self._get("/wallet/balances")
        if data is None:
            return None
        balances = parse_wallet_balances(data)
        if balances is None:
            logger.warning(f"/wallet/balances in an unexpected shape, treated as unreadable: {str(data)[:200]}")
        return balances

    async def get_erg_balance(self) -> float:
        balances = await self.get_wallet_balances()
        if balances:
            return balances["erg"]
        return 0.0

    async def get_node_info(self) -> Optional[dict]:
        return await self._get("/info")

    async def get_health(self) -> dict:
        """Reachability, sync and wallet-lock state. `ok_to_trade` requires all three."""
        info, status = await asyncio.gather(self.get_node_info(), self.get_wallet_status())
        if not info:
            return {"reachable": False, "synced": False, "unlocked": False,
                    "height": 0, "headers": 0, "ok_to_trade": False}
        height = info.get("fullHeight") or 0
        headers = info.get("headersHeight") or 0
        synced = height > 0 and abs(headers - height) < MAX_HEADER_LAG
        unlocked = bool(status and status.get("isUnlocked"))
        return {"reachable": True, "synced": synced, "unlocked": unlocked,
                "height": height, "headers": headers, "ok_to_trade": synced and unlocked}

    async def check_connection(self) -> bool:
        """True if the node is reachable and synced (logs a warning otherwise)."""
        health = await self.get_health()
        if health["reachable"] and not health["synced"]:
            logger.warning(f"Node not fully synced: height={health['height']}, headers={health['headers']}")
        return health["reachable"] and health["synced"]
