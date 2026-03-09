import logging
from typing import Optional

import aiohttp

import config
from exchanges.base import DEXBase, PriceQuote, PoolState

logger = logging.getLogger("ergo_arb.sigmausd")

# SigmaUSD Bank contract details
ORACLE_POOL_API = "https://erg-oracle-ergusd.spirepools.com/frontendData"
EXPLORER_API = "https://api.ergoplatform.com/api/v1"


class SigmaUSDBank(DEXBase):
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self._oracle_price: Optional[float] = None  # USD per ERG from oracle
        self._reserve_ratio: Optional[float] = None
        self._bank_erg_reserve: Optional[float] = None
        self._sigusd_circulating: Optional[float] = None

    async def connect(self):
        self.session = aiohttp.ClientSession()
        logger.info("[exchange]SigmaUSD Bank[/exchange] connected")

    async def disconnect(self):
        if self.session:
            await self.session.close()
            self.session = None
        logger.info("SigmaUSD Bank disconnected")

    async def fetch_oracle_price(self) -> Optional[float]:
        """Fetch ERG/USD price from oracle pool."""
        if not self.session:
            return None
        try:
            async with self.session.get(
                ORACLE_POOL_API,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    raw = await resp.json()
                    # API returns a JSON string that needs to be parsed again
                    import json
                    data = json.loads(raw) if isinstance(raw, str) else raw
                    price = data.get("latest_price", 0)
                    if isinstance(price, (int, float)) and price > 0:
                        # latest_price is already in USD per ERG
                        self._oracle_price = price
                        return self._oracle_price
                logger.warning(f"Oracle API returned {resp.status}")
                return None
        except Exception as e:
            logger.error(f"Oracle price fetch error: {e}")
            return None

    async def fetch_bank_state(self) -> bool:
        """Fetch current bank state (reserves, circulating supply)."""
        if not self.session:
            return False
        try:
            # Query the bank box by its NFT token ID
            url = f"{EXPLORER_API}/boxes/unspent/byTokenId/{config.SIGMAUSD_BANK_NFT}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get("items", data) if isinstance(data, dict) else data
                    if items and len(items) > 0:
                        bank_box = items[0]
                        # Bank box value is in nanoERG
                        self._bank_erg_reserve = bank_box.get("value", 0) / 1e9
                        # Get circulating SigUSD from registers
                        # R4 contains circulating SigUSD, R5 contains circulating SigRSV
                        registers = bank_box.get("additionalRegisters", {})
                        if "R4" in registers:
                            r4 = registers["R4"]
                            r4_value = r4.get("renderedValue", r4.get("serializedValue", "0"))
                            try:
                                self._sigusd_circulating = int(r4_value) / 100  # 2 decimals
                            except (ValueError, TypeError):
                                pass
                        return True
                logger.warning(f"Explorer bank box query returned {resp.status}")
                return False
        except Exception as e:
            logger.error(f"Bank state fetch error: {e}")
            return False

    @property
    def reserve_ratio(self) -> Optional[float]:
        """Calculate current reserve ratio as percentage."""
        if (
            self._bank_erg_reserve is not None
            and self._oracle_price is not None
            and self._sigusd_circulating is not None
            and self._sigusd_circulating > 0
        ):
            # reserve_ratio = (ERG_reserve * ERG_USD_price) / SigUSD_circulating
            reserve_value_usd = self._bank_erg_reserve * self._oracle_price
            return (reserve_value_usd / self._sigusd_circulating) * 100
        return None

    def can_mint_sigusd(self) -> bool:
        """Check if minting SigUSD is allowed (reserve ratio > 400%)."""
        rr = self.reserve_ratio
        return rr is not None and rr > 400

    def can_redeem_sigusd(self) -> bool:
        """Check if redeeming SigUSD is allowed (reserve ratio < 800%)."""
        rr = self.reserve_ratio
        return rr is not None and rr < 800

    def erg_to_sigusd(self, erg_amount: float) -> float:
        """Calculate SigUSD received for minting with ERG (after fees)."""
        if self._oracle_price is None or self._oracle_price == 0:
            return 0
        # _oracle_price is USD per ERG
        gross_sigusd = erg_amount * self._oracle_price
        # Apply 2% protocol fee first, then 0.229% UI fee on the remainder
        after_protocol = gross_sigusd * (1 - config.SIGMAUSD_PROTOCOL_FEE)
        net_sigusd = after_protocol * (1 - config.SIGMAUSD_FRONTEND_FEE)
        return net_sigusd

    def sigusd_to_erg(self, sigusd_amount: float) -> float:
        """Calculate ERG received for redeeming SigUSD (after fees)."""
        if self._oracle_price is None or self._oracle_price == 0:
            return 0
        gross_erg = sigusd_amount / self._oracle_price
        # Apply 2% protocol fee first, then 0.229% UI fee on the remainder
        after_protocol = gross_erg * (1 - config.SIGMAUSD_PROTOCOL_FEE)
        net_erg = after_protocol * (1 - config.SIGMAUSD_FRONTEND_FEE)
        # Subtract receipt box + miner fee
        net_erg -= config.SIGMAUSD_REDEEM_EXTRA_ERG
        return max(net_erg, 0)

    async def get_price(self, pair: str = "ERG/SigUSD") -> Optional[PriceQuote]:
        oracle_price = await self.fetch_oracle_price()
        if oracle_price is None:
            return None

        # oracle_price is USD per ERG
        # Bid = what you get selling ERG (mint SigUSD) after fees
        # Apply 2% protocol fee then 0.229% UI fee
        bid = oracle_price * (1 - config.SIGMAUSD_PROTOCOL_FEE) * (1 - config.SIGMAUSD_FRONTEND_FEE)
        # Ask = what you pay buying ERG (redeem SigUSD) including fees
        ask = oracle_price / ((1 - config.SIGMAUSD_PROTOCOL_FEE) * (1 - config.SIGMAUSD_FRONTEND_FEE))

        return PriceQuote(
            exchange="SigmaUSD Bank",
            pair=pair,
            bid=bid,
            ask=ask,
        )

    async def get_pool_state(self, pair: str = "ERG/SigUSD") -> Optional[PoolState]:
        # SigmaUSD bank is not an AMM pool, but we can represent its state
        await self.fetch_bank_state()
        if self._bank_erg_reserve is None:
            return None
        return PoolState(
            exchange="SigmaUSD Bank",
            pool_id="sigmausd_bank",
            token_x="ERG",
            token_y="SigUSD",
            reserve_x=self._bank_erg_reserve or 0,
            reserve_y=self._sigusd_circulating or 0,
        )

    async def get_full_state(self) -> dict:
        """Get comprehensive bank state for logging."""
        await self.fetch_oracle_price()
        await self.fetch_bank_state()
        return {
            "oracle_erg_usd": self._oracle_price,
            "bank_erg_reserve": self._bank_erg_reserve,
            "sigusd_circulating": self._sigusd_circulating,
            "reserve_ratio": self.reserve_ratio,
            "can_mint_sigusd": self.can_mint_sigusd(),
            "can_redeem_sigusd": self.can_redeem_sigusd(),
        }
