"""ERG/SigUSD AMM pool (ErgoDEX/Spectrum N2T contract), read directly from chain.

Spectrum's off-chain API has been sunset, but the pool contracts are still live
and are what Crux routes swaps through. Reserves and the fee come from the pool
box itself, so quotes match what the contract will accept.
"""
import logging
from typing import Optional

import aiohttp

import config
from exchanges.base import DEXBase, PriceQuote, PoolState

logger = logging.getLogger("ergo_arb.spectrum")

FEE_DENOM = 1000


def amm_output_raw(reserve_in: int, reserve_out: int, amount_in: int, fee_num: int, fee_denom: int = FEE_DENOM) -> int:
    """Constant-product swap output in raw units, floored like the contract."""
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    return reserve_out * amount_in * fee_num // (reserve_in * fee_denom + amount_in * fee_num)


def parse_n2t_pool_box(box: dict, token_id: str, decimals_y: int, symbol_y: str = "SigUSD",
                       fee_num: Optional[int] = None, exchange: str = "ErgoDEX pool (via Crux)") -> PoolState:
    """PoolState from an ERG/token pool box: value = ERG reserve, token Y reserve.

    The fee comes from R4 (ErgoDEX N2T pools) unless `fee_num` is given (Dexy LP).
    """
    assets = box.get("assets", [])
    pool_nft = assets[0]["tokenId"] if assets else ""
    reserve_y_raw = next((a["amount"] for a in assets if a["tokenId"] == token_id), None)
    if reserve_y_raw is None:
        raise ValueError(f"Pool box does not hold token {token_id[:8]}")
    if fee_num is None:
        r4 = box.get("additionalRegisters", {}).get("R4", {})
        fee_num = int(r4.get("renderedValue") if isinstance(r4, dict) else r4)
    return PoolState(
        exchange=exchange,
        pool_id=pool_nft,
        token_x="ERG",
        token_y=symbol_y,
        reserve_x=box["value"] / 10**config.ERG_DECIMALS,
        reserve_y=reserve_y_raw / 10**decimals_y,
        fee_num=fee_num,
        fee_denom=FEE_DENOM,
    )


class SpectrumDEX(DEXBase):
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.pool: Optional[PoolState] = None

    async def connect(self):
        self.session = aiohttp.ClientSession()
        logger.info("[exchange]ErgoDEX pool[/exchange] connected")

    async def disconnect(self):
        if self.session:
            await self.session.close()
            self.session = None
        logger.info("ErgoDEX pool disconnected")

    async def get_pool_state(self, pair: str = "ERG/SigUSD") -> Optional[PoolState]:
        """Read the ERG/SigUSD pool box from the explorer."""
        if not self.session:
            return None
        url = f"{config.ERGO_EXPLORER_API_URL}/boxes/unspent/byTokenId/{config.SPECTRUM_SIGUSD_POOL_NFT}?limit=1"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning(f"Pool box query returned {resp.status}")
                    return None
                data = await resp.json()
            items = data.get("items", []) if isinstance(data, dict) else data
            if not items:
                logger.warning("ERG/SigUSD pool box not found")
                return None
            self.pool = parse_n2t_pool_box(items[0], config.SIGUSD_TOKEN_ID, config.SIGUSD_DECIMALS)
            return self.pool
        except Exception as e:
            logger.error(f"Pool box fetch error: {e}")
            return None

    async def get_price(self, pair: str = "ERG/SigUSD") -> Optional[PriceQuote]:
        pool = await self.get_pool_state(pair)
        if not pool:
            return None
        price = pool.price_x_in_y
        return PriceQuote(
            exchange=f"ErgoDEX ({pool.pool_id[:8]}...)",
            pair=pair,
            bid=price,
            ask=price,
            bid_volume=pool.reserve_x,
            ask_volume=pool.reserve_y,
        )

    async def get_erg_sigusd_price(self) -> Optional[float]:
        """Spot SigUSD per ERG from the pool reserves."""
        pool = await self.get_pool_state()
        return pool.price_x_in_y if pool else None
