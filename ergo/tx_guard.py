"""Verify an unsigned transaction before asking the node wallet to sign it.

Third-party APIs (Crux) build the swap/mint transactions we sign. If such an
API is compromised, buggy or MITM'd it could build a TX that sends wallet
funds elsewhere, and the node would sign it. The guard only accepts a TX when:

- the input boxes (resolved by the caller from our own node) match the TX;
- every non-wallet input is a known contract box (pool, bank, ... identified by
  its NFT, see KNOWN_CONTRACT_NFTS) that the TX recreates under the same
  ErgoTree with that NFT, and never someone else's wallet (P2PK) box;
- every other output goes to our wallet, the miner fee contract, or a
  whitelisted service-fee address, within fee caps;
- the wallet's net ERG/token flows stay inside the policy: ERG spent under
  the cap, requested tokens received at or above the minimum, and no other
  wallet token lost unless explicitly allowed.
"""
from dataclasses import dataclass, field
from typing import Optional

import config

MINER_FEE_TREE = (
    "1005040004000e36100204a00b08cd0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    "ea02d192a39a8cc7a701730073011001020402d19683030193a38cc7b2a57300000193c2b2a573010074730273"
    "03830108cdeeac93b1a57304"
)


# Singleton contracts a transaction may spend and recreate. Any other foreign input is refused, so the
# policy slack cannot be routed into a box that only looks like a contract (an amount-1 token on a
# script that needs no signature).
KNOWN_CONTRACT_NFTS = frozenset({config.SPECTRUM_SIGUSD_POOL_NFT, config.SIGMAUSD_BANK_NFT,
                                 config.DEXY_USE_LP_NFT})
P2PK_PREFIX = "0008cd"


class TxGuardError(Exception):
    """The transaction does not match what we intended to sign."""


@dataclass
class SignPolicy:
    max_erg_spent: int                                       # nanoERG net leaving the wallet, all fees included
    min_received: dict[str, int] = field(default_factory=dict)     # token id -> min raw amount received
    max_token_spent: dict[str, int] = field(default_factory=dict)  # token id -> max raw amount spent
    min_erg_received: int = 0                                # nanoERG net the wallet must gain (token -> ERG swaps)
    max_service_fee: int = 1_000_000_000                    # nanoERG to whitelisted service-fee trees
    max_miner_fee: int = 10_000_000                         # 0.01 ERG
    service_fee_trees: frozenset = field(default_factory=lambda: frozenset(config.SERVICE_FEE_ERGO_TREES))
    # Explicit recipients for a send: ErgoTree -> {"ERG": max nanoERG, token id: max raw amount}
    payees: dict = field(default_factory=dict)
    # NFTs a foreign input may carry; None accepts any singleton token (only for third-party contracts
    # whose NFTs this bot does not know, e.g. Crux's Dexy mint boxes).
    contract_nfts: Optional[frozenset] = field(default_factory=lambda: KNOWN_CONTRACT_NFTS)


@dataclass
class GuardReport:
    erg_spent: int
    received: dict[str, int]
    spent: dict[str, int]
    service_fee: int
    miner_fee: int
    paid: dict = field(default_factory=dict)  # payee tree -> {"ERG"/token id: amount}


def _amount(x) -> int:
    return int(x)


def _tokens(box: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for a in box.get("assets", []):
        out[a["tokenId"]] = out.get(a["tokenId"], 0) + _amount(a["amount"])
    return out


def verify_unsigned_tx(tx: dict, input_boxes: list[dict], wallet_trees: set[str], policy: SignPolicy) -> GuardReport:
    """Raise TxGuardError unless `tx` is safe to sign under `policy`.

    `input_boxes` must be the TX's input boxes as resolved from our own node,
    in input order; the values claimed inside `tx` are not trusted.
    """
    tx_input_ids = [i["boxId"] for i in tx.get("inputs", [])]
    if [b["boxId"] for b in input_boxes] != tx_input_ids:
        raise TxGuardError("resolved input boxes do not match the transaction inputs")
    if not tx_input_ids:
        raise TxGuardError("transaction has no inputs")

    outputs = tx.get("outputs", [])
    contract_outputs: set[int] = set()
    wallet_in_erg = 0
    wallet_in_tok: dict[str, int] = {}

    for box in input_boxes:
        if box["ergoTree"] in wallet_trees:
            wallet_in_erg += _amount(box["value"])
            for t, a in _tokens(box).items():
                wallet_in_tok[t] = wallet_in_tok.get(t, 0) + a
            continue
        if box["ergoTree"].startswith(P2PK_PREFIX):
            raise TxGuardError(f"foreign input {box['boxId'][:12]} is someone else's wallet box")
        singletons = [t for t, a in _tokens(box).items() if a == 1]
        if not singletons:
            raise TxGuardError(f"foreign input {box['boxId'][:12]} is not an NFT-identified contract box")
        if policy.contract_nfts is not None:
            singletons = [t for t in singletons if t in policy.contract_nfts]
            if not singletons:
                raise TxGuardError(f"foreign input {box['boxId'][:12]} is not a known contract box")
        match = next(
            (i for i, o in enumerate(outputs)
             if i not in contract_outputs
             and o["ergoTree"] == box["ergoTree"]
             and any(_tokens(o).get(t, 0) >= 1 for t in singletons)),
            None,
        )
        if match is None:
            raise TxGuardError(f"contract input {box['boxId'][:12]} is not recreated by any output")
        contract_outputs.add(match)

    wallet_out_erg = miner_fee = service_fee = 0
    wallet_out_tok: dict[str, int] = {}
    paid: dict[str, dict[str, int]] = {}
    for i, o in enumerate(outputs):
        if i in contract_outputs:
            continue
        tree = o["ergoTree"]
        if tree in wallet_trees:
            wallet_out_erg += _amount(o["value"])
            for t, a in _tokens(o).items():
                wallet_out_tok[t] = wallet_out_tok.get(t, 0) + a
        elif tree in policy.payees:
            got = paid.setdefault(tree, {})
            got["ERG"] = got.get("ERG", 0) + _amount(o["value"])
            for t, a in _tokens(o).items():
                got[t] = got.get(t, 0) + a
        elif tree == MINER_FEE_TREE and not o.get("assets"):
            miner_fee += _amount(o["value"])
        elif tree in policy.service_fee_trees and not o.get("assets"):
            service_fee += _amount(o["value"])
        else:
            raise TxGuardError(f"unexpected output #{i} to {tree[:24]}... ({_amount(o['value'])} nanoERG, {len(o.get('assets', []))} tokens)")

    for tree, got in paid.items():
        allowed = policy.payees[tree]
        for asset, amount in got.items():
            if amount > allowed.get(asset, 0):
                raise TxGuardError(f"payee {tree[:24]}... would receive {amount} of {asset[:12]}, "
                                   f"allowed {allowed.get(asset, 0)}")

    if miner_fee > policy.max_miner_fee:
        raise TxGuardError(f"miner fee {miner_fee} exceeds {policy.max_miner_fee}")
    if service_fee > policy.max_service_fee:
        raise TxGuardError(f"service fee {service_fee} exceeds {policy.max_service_fee}")

    erg_spent = wallet_in_erg - wallet_out_erg
    trade_cap = int(config.MAX_TRADE_SIZE_ERG * 1e9) + policy.max_service_fee + policy.max_miner_fee
    if erg_spent > trade_cap:
        raise TxGuardError(f"wallet spends {erg_spent} nanoERG, over MAX_TRADE_SIZE_ERG={config.MAX_TRADE_SIZE_ERG} + fee budget")
    if erg_spent > policy.max_erg_spent:
        raise TxGuardError(f"wallet spends {erg_spent} nanoERG, policy allows {policy.max_erg_spent}")

    if policy.min_erg_received > 0 and -erg_spent < policy.min_erg_received:
        raise TxGuardError(f"wallet receives {-erg_spent} nanoERG, minimum is {policy.min_erg_received}")

    received: dict[str, int] = {}
    spent: dict[str, int] = {}
    for t in set(wallet_in_tok) | set(wallet_out_tok):
        net = wallet_out_tok.get(t, 0) - wallet_in_tok.get(t, 0)
        if net > 0:
            received[t] = net
        elif net < 0:
            spent[t] = -net
            if -net > policy.max_token_spent.get(t, 0):
                raise TxGuardError(f"wallet loses token {t[:12]} ({-net} raw), policy allows {policy.max_token_spent.get(t, 0)}")
    for t, minimum in policy.min_received.items():
        if received.get(t, 0) < minimum:
            raise TxGuardError(f"wallet receives {received.get(t, 0)} raw of {t[:12]}, minimum is {minimum}")

    return GuardReport(erg_spent=erg_spent, received=received, spent=spent, service_fee=service_fee,
                       miner_fee=miner_fee, paid=paid)
