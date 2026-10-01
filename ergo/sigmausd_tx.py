"""Build a SigmaUSD bank SigUSD-redeem transaction (EIP-15 bank contract)."""
import config
from ergo.codec import decode_int_register, encode_slong
from ergo.tx_guard import MINER_FEE_TREE
from ergo.wallet import select_boxes, sum_assets
from exchanges.sigmausd import BankState, PROTOCOL_FEE_PERCENT, quote_redeem_sigusd, ui_fee_nanoerg

MINER_FEE = 1_100_000  # 0.0011 ERG
RECEIPT_BOX_VALUE = 1_000_000  # 0.001 ERG, receipt box required by the bank contract


def register_int(box: dict, reg: str) -> int:
    """Integer register from a node box (hex string) or explorer box (dict)."""
    value = box.get("additionalRegisters", {})[reg]
    if isinstance(value, dict):
        if value.get("renderedValue") is not None:
            return int(value["renderedValue"])
        value = value["serializedValue"]
    return decode_int_register(value)


def build_redeem_tx(bank_box: dict, oracle_box: dict, wallet_boxes: list[dict], cents: int,
                    height: int, our_tree: str, ui_fee_tree: str) -> tuple[dict, dict]:
    """Unsigned TX redeeming `cents` SigUSD, plus the computed amounts.

    Wallet boxes are selected until they cover `cents` SigUSD and the miner fee +
    receipt box; leftover SigUSD and any other tokens go back in the payout box.
    """
    sigusd_circ = register_int(bank_box, "R4")
    sigrsv_circ = register_int(bank_box, "R5")
    oracle_r4 = register_int(oracle_box, "R4")
    state = BankState(int(bank_box["value"]), sigusd_circ, oracle_r4)

    br_delta_expected = state.nominal_price() * (-cents)
    fee = abs(br_delta_expected) * PROTOCOL_FEE_PERCENT // 100
    bc_delta = -(br_delta_expected + fee)  # ERG leaving the bank
    ui_fee = ui_fee_nanoerg(bc_delta)
    user_receives = quote_redeem_sigusd(state, cents)
    assert user_receives == bc_delta - ui_fee

    user_boxes = select_boxes(
        wallet_boxes, config.SIGUSD_TOKEN_ID, cents, min_erg=MINER_FEE + RECEIPT_BOX_VALUE
    )
    user_erg = sum(int(b["value"]) for b in user_boxes)
    user_assets = sum_assets(user_boxes)
    user_assets[config.SIGUSD_TOKEN_ID] -= cents
    payout_assets = [{"tokenId": t, "amount": a} for t, a in user_assets.items() if a > 0]

    bank_assets = []
    for a in bank_box["assets"]:
        amount = int(a["amount"]) + (cents if a["tokenId"] == config.SIGUSD_TOKEN_ID else 0)
        bank_assets.append({"tokenId": a["tokenId"], "amount": amount})

    outputs = [
        {   # 0: bank box, SigUSD returned, ERG paid out
            "value": int(bank_box["value"]) - bc_delta,
            "ergoTree": bank_box["ergoTree"],
            "assets": bank_assets,
            "additionalRegisters": {"R4": encode_slong(sigusd_circ - cents), "R5": encode_slong(sigrsv_circ)},
            "creationHeight": height,
        },
        {   # 1: receipt box (scCircDelta, bcReserveDelta)
            "value": RECEIPT_BOX_VALUE,
            "ergoTree": our_tree,
            "assets": [],
            "additionalRegisters": {"R4": encode_slong(-cents), "R5": encode_slong(-bc_delta)},
            "creationHeight": height,
        },
        {   # 2: payout + change
            "value": user_receives + user_erg - MINER_FEE - RECEIPT_BOX_VALUE,
            "ergoTree": our_tree,
            "assets": payout_assets,
            "additionalRegisters": {},
            "creationHeight": height,
        },
        {   # 3: UI fee
            "value": ui_fee, "ergoTree": ui_fee_tree, "assets": [], "additionalRegisters": {},
            "creationHeight": height,
        },
        {   # 4: miner fee
            "value": MINER_FEE, "ergoTree": MINER_FEE_TREE, "assets": [], "additionalRegisters": {},
            "creationHeight": height,
        },
    ]
    tx = {
        "inputs": [{"boxId": bank_box["boxId"], "extension": {}}]
                  + [{"boxId": b["boxId"], "extension": {}} for b in user_boxes],
        "dataInputs": [{"boxId": oracle_box["boxId"]}],
        "outputs": outputs,
    }
    info = {
        "state": state, "bc_delta": bc_delta, "protocol_fee": fee, "ui_fee": ui_fee,
        "user_receives": user_receives, "miner_fee": MINER_FEE, "user_boxes": user_boxes,
    }
    return tx, info
