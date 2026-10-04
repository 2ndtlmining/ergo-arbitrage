"""Dexy USE: bank mint math (FreeMint/ArbMint) and the USE/ERG LP read on-chain."""
import asyncio
import logging
from typing import Optional

import aiohttp

import config
from exchanges.base import PoolState
from exchanges.spectrum import parse_n2t_pool_box

logger = logging.getLogger("ergo_arb.dexy")

LP_FEE_NUM = 997  # Dexy LP swap fee 0.3%


def _unit_rate(box_state: dict) -> int:
    """nanoERG per raw USE unit at the oracle (oracle_rate is per whole USD)."""
    return int(box_state["oracle_rate"]) // 10**config.USE_DECIMALS


def dexy_mint_cost_nanoerg(use_raw: int, box_state: dict) -> int:
    """nanoERG the bank charges to mint `use_raw` USE units (oracle + bank + buyback fee)."""
    if use_raw <= 0:
        return 0
    rate = _unit_rate(box_state)
    denom = int(box_state.get("fee_denom", 1000))
    bank_rate = rate * (denom + int(box_state.get("bank_fee_num", 3))) // denom
    buyback_rate = rate * int(box_state.get("buyback_fee_num", 2)) // denom
    return use_raw * (bank_rate + buyback_rate)


def quote_dexy_mint(budget_nanoerg: int, box_state: dict) -> int:
    """Max raw USE units mintable with `budget_nanoerg` (ignores the mint's max amount)."""
    per_unit = dexy_mint_cost_nanoerg(1, box_state)
    return budget_nanoerg // per_unit if per_unit > 0 and budget_nanoerg > 0 else 0


def mint_available(use_mint: Optional[dict]) -> bool:
    """True if FreeMint or ArbMint is currently open (Crux `is_available`)."""
    if not use_mint:
        return False
    return any((use_mint.get(t) or {}).get("is_available", False) for t in ("free_mint", "arb_mint"))


def mint_box_state(use_mint: Optional[dict]) -> Optional[dict]:
    """Bank/oracle state for the open mint type (FreeMint preferred)."""
    if not use_mint:
        return None
    for t in ("free_mint", "arb_mint"):
        info = use_mint.get(t) or {}
        if info.get("is_available") and info.get("box_state"):
            return info["box_state"]
    for t in ("free_mint", "arb_mint"):
        if (use_mint.get(t) or {}).get("box_state"):
            return use_mint[t]["box_state"]
    return None


def parse_use_lp_box(box: dict) -> PoolState:
    return parse_n2t_pool_box(
        box, config.USE_TOKEN_ID, config.USE_DECIMALS,
        symbol_y="USE", fee_num=LP_FEE_NUM, exchange="Dexy USE LP",
    )


async def fetch_use_lp(session: aiohttp.ClientSession) -> Optional[PoolState]:
    """Read the Dexy USE/ERG LP box from the explorer."""
    url = f"{config.ERGO_EXPLORER_API_URL}/boxes/unspent/byTokenId/{config.DEXY_USE_LP_NFT}?limit=1"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning(f"USE LP box query returned {resp.status}")
                return None
            data = await resp.json()
        items = data.get("items", []) if isinstance(data, dict) else data
        return parse_use_lp_box(items[0]) if items else None
    except Exception as e:
        logger.error(f"USE LP box fetch error: {e}")
        return None


async def fetch_use_mint_status(session) -> Optional[dict]:
    """Crux's USE free_mint / arb_mint status ({mint_type: status}), or None when neither answers."""
    async def one(mint_type: str):
        try:
            async with session.get(f"{config.CRUX_API_URL}/dexy/mint_status/use?mint_type={mint_type}",
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return mint_type, await r.json()
        except Exception as e:
            logger.error(f"Crux {mint_type} status error: {e}")
        return mint_type, None

    pairs = await asyncio.gather(one("free_mint"), one("arb_mint"))
    return {k: v for k, v in pairs if v is not None} or None
