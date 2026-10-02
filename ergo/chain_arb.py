"""Two chained transactions for on-chain arbitrage.

The SigmaUSD bank and the ErgoDEX pool both require their successor box at
OUTPUTS(0), so the two legs cannot share one transaction. Leg 2 spends leg 1's
wallet output instead and is submitted right after leg 1, so both normally land
in the same block. If leg 2 fails, the wallet simply holds leg 1's output
(e.g. SigUSD, which can always be redeemed).
"""
import config
from ergo.pool_swap import build_pool_swap_tx
from ergo.sigmausd_tx import build_redeem_tx
from ergo.tx_guard import SignPolicy

LEG1_OUTPUT_PLACEHOLDER = "<leg1-output>"  # box id is only known once leg 1 is signed


def tx_output_box(signed_tx: dict, index: int) -> dict:
    """Box dict for an output of a signed TX, usable as an input of the next TX."""
    out = signed_tx["outputs"][index]
    return {
        "boxId": out["boxId"],
        "value": int(out["value"]),
        "ergoTree": out["ergoTree"],
        "assets": [{"tokenId": a["tokenId"], "amount": int(a["amount"])} for a in out.get("assets", [])],
        "additionalRegisters": out.get("additionalRegisters", {}),
    }


def redeem_policy(info: dict, cents: int, ui_fee_tree: str) -> SignPolicy:
    """Guard policy for a bank redeem built by build_redeem_tx."""
    return SignPolicy(
        max_erg_spent=0,
        max_token_spent={config.SIGUSD_TOKEN_ID: cents},
        min_erg_received=info["user_receives"] - info["miner_fee"],
        max_service_fee=info["ui_fee"],
        service_fee_trees=frozenset({ui_fee_tree}),
    )


def build_redeem_leg(bank_box: dict, oracle_box: dict, leg1_output: dict, cents: int,
                     height: int, our_tree: str, ui_fee_tree: str) -> tuple[dict, dict, SignPolicy]:
    tx, info = build_redeem_tx(bank_box, oracle_box, [leg1_output], cents, height=height,
                               our_tree=our_tree, ui_fee_tree=ui_fee_tree)
    return tx, info, redeem_policy(info, cents, ui_fee_tree)


def plan_pool_buy_redeem(pool_box: dict, bank_box: dict, oracle_box: dict, wallet_boxes: list[dict],
                         erg_in: int, *, height: int, our_tree: str, ui_fee_tree: str) -> dict:
    """Plan: swap `erg_in` nanoERG -> SigUSD on the pool, then redeem that SigUSD at the bank.

    Profit = ERG the bank pays out - leg 2 miner fee - ERG swapped - leg 1 miner fee.
    (The receipt box value comes back to the wallet, so it nets out.)
    """
    leg1_tx, leg1_info = build_pool_swap_tx(
        pool_box, wallet_boxes, config.SIGUSD_TOKEN_ID, sell_erg=True,
        amount_in=erg_in, height=height, our_tree=our_tree,
    )
    sim = dict(leg1_tx["outputs"][1])
    sim.pop("creationHeight", None)
    leg1_output = {"boxId": LEG1_OUTPUT_PLACEHOLDER, **sim}

    cents = leg1_info["amount_out"]
    leg2_tx, leg2_info, leg2_policy = build_redeem_leg(
        bank_box, oracle_box, leg1_output, cents, height, our_tree, ui_fee_tree
    )
    profit = (leg2_info["user_receives"] - leg2_info["miner_fee"]) - (erg_in + leg1_info["miner_fee"])
    return {
        "leg1_tx": leg1_tx, "leg1_info": leg1_info,
        "leg1_policy": SignPolicy(max_erg_spent=erg_in + leg1_info["miner_fee"],
                                  min_received={config.SIGUSD_TOKEN_ID: cents}, max_service_fee=0),
        "leg1_output_box": leg1_output,
        "leg2_tx": leg2_tx, "leg2_info": leg2_info, "leg2_policy": leg2_policy,
        "sigusd_cents": cents,
        "profit_nanoerg": profit,
        "profit_percent": profit / erg_in * 100 if erg_in else 0.0,
    }
