"""Swap directly against an ErgoDEX/Spectrum N2T pool box (no Crux or batcher fee).

The pool contract (contracts/amm/cfmm/v1/n2t/Pool.sc in spectrum-finance/ergo-dex)
lets anyone spend the pool box if OUTPUTS(0) is its successor: same script and
fee register (R4), the same pool NFT and LP amount, exactly three tokens, more
than 0.01 ERG, and reserves that satisfy the constant-product-with-fee check.
The swapped amount is floored exactly like `amm_output_raw`, which is the
largest output that check accepts.

Because the TX spends one specific pool box, it simply becomes invalid if the
pool moves first: there is no way to get filled at a worse price.
"""
from ergo.sigmausd_tx import register_int
from ergo.tx_guard import MINER_FEE_TREE
from ergo.wallet import select_boxes, sum_assets
from exchanges.spectrum import amm_output_raw

MINER_FEE = 1_100_000      # 0.0011 ERG
MIN_BOX_VALUE = 1_000_000  # 0.001 ERG, kept in the user's output box
POOL_MIN_VALUE = 10_000_000


def serialized_registers(box: dict) -> dict:
    """Registers as serialized hex, from node (hex) or explorer (dict) box JSON."""
    regs = {}
    for k, v in (box.get("additionalRegisters") or {}).items():
        regs[k] = v["serializedValue"] if isinstance(v, dict) else v
    return regs


def build_pool_swap_tx(pool_box: dict, wallet_boxes: list[dict], token_id: str, *, sell_erg: bool,
                       amount_in: int, height: int, our_tree: str,
                       miner_fee: int = MINER_FEE) -> tuple[dict, dict]:
    """Unsigned TX swapping `amount_in` (nanoERG if sell_erg, else raw token units) through the pool.

    Returns (tx, info) where info has amount_out, fee_num, new reserves, price impact
    and the wallet boxes used.
    """
    nft, lp, y = pool_box["assets"][:3]
    if len(pool_box["assets"]) != 3 or y["tokenId"] != token_id:
        raise ValueError(f"pool box does not trade token {token_id[:8]}")
    if amount_in <= 0:
        raise ValueError("amount_in must be positive")

    fee_num = register_int(pool_box, "R4")
    rx, ry = int(pool_box["value"]), int(y["amount"])
    if sell_erg:
        out = amm_output_raw(rx, ry, amount_in, fee_num)
        new_x, new_y = rx + amount_in, ry - out
        spot_out = amount_in * ry / rx
    else:
        out = amm_output_raw(ry, rx, amount_in, fee_num)
        new_x, new_y = rx - out, ry + amount_in
        spot_out = amount_in * rx / ry
    if out <= 0:
        raise ValueError("swap output is zero at this size")
    if new_x <= POOL_MIN_VALUE:
        raise ValueError("swap would drain the pool below its storage-rent minimum")

    if sell_erg:
        user_boxes = select_boxes(wallet_boxes, None, 0, min_erg=amount_in + miner_fee + MIN_BOX_VALUE)
    else:
        user_boxes = select_boxes(wallet_boxes, token_id, amount_in, min_erg=miner_fee + MIN_BOX_VALUE)

    user_erg = sum(int(b["value"]) for b in user_boxes)
    user_assets = sum_assets(user_boxes)
    if sell_erg:
        user_value = user_erg - amount_in - miner_fee
        user_assets[token_id] = user_assets.get(token_id, 0) + out
    else:
        user_value = user_erg + out - miner_fee
        user_assets[token_id] -= amount_in
    if user_value < MIN_BOX_VALUE:
        raise ValueError(f"selected boxes leave {user_value} nanoERG for the change box, need {MIN_BOX_VALUE} ERG minimum")

    outputs = [
        {   # 0: pool successor (the contract requires OUTPUTS(0))
            "value": new_x,
            "ergoTree": pool_box["ergoTree"],
            "assets": [
                {"tokenId": nft["tokenId"], "amount": int(nft["amount"])},
                {"tokenId": lp["tokenId"], "amount": int(lp["amount"])},
                {"tokenId": token_id, "amount": new_y},
            ],
            "additionalRegisters": serialized_registers(pool_box),
            "creationHeight": height,
        },
        {   # 1: swap output + change
            "value": user_value,
            "ergoTree": our_tree,
            "assets": [{"tokenId": t, "amount": a} for t, a in user_assets.items() if a > 0],
            "additionalRegisters": {},
            "creationHeight": height,
        },
        {   # 2: miner fee
            "value": miner_fee, "ergoTree": MINER_FEE_TREE, "assets": [], "additionalRegisters": {},
            "creationHeight": height,
        },
    ]
    tx = {
        "inputs": [{"boxId": pool_box["boxId"], "extension": {}}]
                  + [{"boxId": b["boxId"], "extension": {}} for b in user_boxes],
        "dataInputs": [],
        "outputs": outputs,
    }
    info = {
        "amount_out": out,
        "fee_num": fee_num,
        "reserves_before": (rx, ry),
        "reserves_after": (new_x, new_y),
        "price_impact_percent": (1 - out / spot_out) * 100 if spot_out else 0.0,
        "miner_fee": miner_fee,
        "user_boxes": user_boxes,
    }
    return tx, info
