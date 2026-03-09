import logging
from typing import Optional

import aiohttp

import config

logger = logging.getLogger("ergo_arb.ergo_node")


class ErgoNodeClient:
    def __init__(self):
        self.base_url = config.ERGO_NODE_URL
        self.api_key = config.ERGO_NODE_API_KEY
        self.session: Optional[aiohttp.ClientSession] = None

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
                    return await resp.json()
                logger.warning(f"Node GET {endpoint} returned {resp.status}")
                return None
        except Exception as e:
            logger.error(f"Node GET {endpoint} error: {e}")
            return None

    async def _post(self, endpoint: str, data: dict | list = None) -> Optional[dict | list]:
        if not self.session:
            return None
        url = f"{self.base_url}{endpoint}"
        try:
            async with self.session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"Node POST {endpoint} returned {resp.status}")
                return None
        except Exception as e:
            logger.error(f"Node POST {endpoint} error: {e}")
            return None

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
        if data:
            # assets is a dict of {token_id: raw_amount}
            assets = data.get("assets", {})
            if isinstance(assets, dict):
                tokens = {
                    token_id: {"amount": amount}
                    for token_id, amount in assets.items()
                }
            else:
                tokens = {}
            return {
                "erg": data.get("balance", 0) / 1e9,  # Convert nanoERG to ERG
                "tokens": tokens,
            }
        return None

    async def get_erg_balance(self) -> float:
        balances = await self.get_wallet_balances()
        if balances:
            return balances["erg"]
        return 0.0

    async def get_node_info(self) -> Optional[dict]:
        return await self._get("/info")

    async def check_connection(self) -> bool:
        """Verify node is reachable and synced."""
        info = await self.get_node_info()
        if info:
            height = info.get("fullHeight", 0)
            best_header = info.get("headersHeight", 0)
            is_synced = height > 0 and abs(best_header - height) < 5
            if not is_synced:
                logger.warning(
                    f"Node not fully synced: height={height}, headers={best_header}"
                )
            return True
        return False
