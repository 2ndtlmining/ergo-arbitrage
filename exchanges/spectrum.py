"""ERG/SigUSD AMM pool (ErgoDEX/Spectrum N2T contract), read directly from chain.

Spectrum's off-chain API has been sunset, but the pool contracts are still live
and are what Crux routes swaps through. Reserves and the fee come from the pool
box itself, so quotes match what the contract will accept.
"""
import logging
from typing import Optional

import config
from exchanges.base import PoolState

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
