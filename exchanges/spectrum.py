import logging
from typing import Optional

import aiohttp

import config
from exchanges.base import DEXBase, PriceQuote, PoolState

logger = logging.getLogger("ergo_arb.spectrum")


class SpectrumDEX(DEXBase):
    def __init__(self):
        self.api_url = config.SPECTRUM_API_URL
        self.session: Optional[aiohttp.ClientSession] = None
        self._pools: dict[str, PoolState] = {}

    async def connect(self):
        self.session = aiohttp.ClientSession()
        logger.info("[exchange]Spectrum DEX[/exchange] connected")

    async def disconnect(self):
        if self.session:
            await self.session.close()
            self.session = None
        logger.info("Spectrum DEX disconnected")

    async def _fetch_markets(self) -> Optional[list]:
        if not self.session:
            return None
        url = f"{self.api_url}/price-tracking/markets"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"Spectrum markets API returned {resp.status}")
                return None
        except Exception as e:
            logger.error(f"Spectrum markets fetch error: {e}")
            return None

    def _find_erg_sigusd_pools(self, markets: list) -> list[dict]:
        """Find all ERG/SigUSD pools from market data."""
        pools = []
        for m in markets:
            base = m.get("baseSymbol", "")
            quote = m.get("quoteSymbol", "")
            base_id = m.get("baseId", "")
            quote_id = m.get("quoteId", "")
            # Match ERG/SigUSD pools (not test tokens like tERG/tSigUSD)
            if (
                base == "ERG"
                and quote == "SigUSD"
                and base_id == config.ERG_TOKEN_ID
                and quote_id == config.SIGUSD_TOKEN_ID
            ):
                pools.append(m)
        return pools

    async def get_price(self, pair: str = "ERG/SigUSD") -> Optional[PriceQuote]:
        pool = await self.get_pool_state(pair)
        if not pool:
            return None
        price = pool.price_x_in_y
        return PriceQuote(
            exchange=f"Spectrum ({pool.pool_id[:8]}...)",
            pair=pair,
            bid=price,
            ask=price,
            bid_volume=pool.reserve_x,
            ask_volume=pool.reserve_y,
        )

    async def get_all_erg_sigusd_pools(self) -> list[PoolState]:
        """Get all ERG/SigUSD pools with their states."""
        markets = await self._fetch_markets()
        if not markets:
            return []

        erg_sigusd = self._find_erg_sigusd_pools(markets)
        pools = []

        for m in erg_sigusd:
            pool_id = m.get("id", "")
            base_vol = m.get("baseVolume", {})
            quote_vol = m.get("quoteVolume", {})

            base_decimals = base_vol.get("units", {}).get("asset", {}).get("decimals", 9)
            quote_decimals = quote_vol.get("units", {}).get("asset", {}).get("decimals", 2)

            base_raw = base_vol.get("value", 0)
            quote_raw = quote_vol.get("value", 0)

            # The market data gives volume, not reserves
            # We use lastPrice for price info
            last_price = m.get("lastPrice", 0)

            if last_price > 0:
                pool = PoolState(
                    exchange="Spectrum",
                    pool_id=pool_id,
                    token_x="ERG",
                    token_y="SigUSD",
                    # We don't have exact reserves from market endpoint,
                    # but we can infer pricing. For accurate reserves,
                    # we'd need to query the pool boxes directly.
                    reserve_x=0,  # Will be populated from pool box query
                    reserve_y=0,
                    fee_num=997,
                    fee_denom=1000,
                )
                # Store the last price for now
                pool._last_price = last_price
                pool._base_volume = base_raw / (10 ** base_decimals)
                pool._quote_volume = quote_raw / (10 ** quote_decimals)
                pools.append(pool)

        return pools

    async def get_pool_state(self, pair: str = "ERG/SigUSD") -> Optional[PoolState]:
        """Get the best (most liquid) ERG/SigUSD pool."""
        pools = await self.get_all_erg_sigusd_pools()
        if not pools:
            return None

        # Return the pool with the highest volume
        best = max(pools, key=lambda p: getattr(p, '_base_volume', 0))
        return best

    async def get_erg_sigusd_price(self) -> Optional[float]:
        """Get ERG price in SigUSD from the best pool."""
        pools = await self.get_all_erg_sigusd_pools()
        if not pools:
            return None

        # Use the most liquid pool's price
        best = max(pools, key=lambda p: getattr(p, '_base_volume', 0))
        return getattr(best, '_last_price', None)

    def calculate_swap_output(
        self,
        input_amount: float,
        input_is_erg: bool,
        reserve_erg: float,
        reserve_sigusd: float,
    ) -> float:
        """Calculate swap output using constant product formula with 0.3% fee."""
        if input_is_erg:
            reserve_in = reserve_erg
            reserve_out = reserve_sigusd
        else:
            reserve_in = reserve_sigusd
            reserve_out = reserve_erg

        input_with_fee = input_amount * 997
        numerator = input_with_fee * reserve_out
        denominator = (reserve_in * 1000) + input_with_fee

        if denominator == 0:
            return 0
        return numerator / denominator

    def calculate_price_impact(
        self,
        input_amount: float,
        input_is_erg: bool,
        reserve_erg: float,
        reserve_sigusd: float,
    ) -> float:
        """Calculate price impact as percentage."""
        if input_is_erg:
            spot_price = reserve_sigusd / reserve_erg if reserve_erg > 0 else 0
        else:
            spot_price = reserve_erg / reserve_sigusd if reserve_sigusd > 0 else 0

        if spot_price == 0 or input_amount == 0:
            return 0

        output = self.calculate_swap_output(input_amount, input_is_erg, reserve_erg, reserve_sigusd)
        effective_price = output / input_amount
        return abs(1 - (effective_price / spot_price)) * 100
