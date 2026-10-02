"""Sign third-party-built transactions with the node wallet, only after tx_guard passes."""
import sys

import aiohttp

from ergo.tx_guard import SignPolicy, TxGuardError, verify_unsigned_tx

TIMEOUT = aiohttp.ClientTimeout(total=15)


class DryRun(Exception):
    """The TX passed the guard but signing was not requested (no --execute)."""


def execute_requested() -> bool:
    """Execute scripts only sign when run with --execute."""
    return "--execute" in sys.argv


async def _get_json(ns, url: str):
    async with ns.get(url, timeout=TIMEOUT) as r:
        if r.status != 200:
            return None
        return await r.json()


async def wallet_trees(ns, node_url: str) -> set[str]:
    """ErgoTrees of the node wallet's P2PK addresses."""
    addresses = await _get_json(ns, f"{node_url}/wallet/addresses") or []
    trees = set()
    for addr in addresses:
        raw = await _get_json(ns, f"{node_url}/utils/addressToRaw/{addr}")
        if raw and raw.get("raw"):
            trees.add("0008cd" + raw["raw"])
    if not trees:
        raise TxGuardError("could not resolve wallet addresses from the node (is the wallet unlocked?)")
    return trees


async def guarded_sign(ns, node_url: str, unsigned_tx: dict, policy: SignPolicy, *, execute: bool) -> dict:
    """Verify `unsigned_tx` against `policy` using input boxes from our node, then sign it.

    Raises TxGuardError (and never calls the sign endpoint) if verification fails,
    and DryRun after a successful verification when `execute` is False.
    """
    inputs = unsigned_tx.get("inputs", [])
    data_inputs = unsigned_tx.get("dataInputs", [])

    input_boxes = []
    inputs_raw = []
    for inp in inputs:
        box_id = inp["boxId"]
        box = await _get_json(ns, f"{node_url}/utxo/withPool/byId/{box_id}")
        raw = await _get_json(ns, f"{node_url}/utxo/withPool/byIdBinary/{box_id}")
        if not box or not raw:
            raise TxGuardError(f"input box {box_id[:12]} not found in the node UTXO set or mempool")
        input_boxes.append(box)
        inputs_raw.append(raw.get("bytes", ""))

    data_raw = []
    for di in data_inputs:
        raw = await _get_json(ns, f"{node_url}/utxo/withPool/byIdBinary/{di['boxId']}")
        if not raw:
            raise TxGuardError(f"data input {di['boxId'][:12]} not found")
        data_raw.append(raw.get("bytes", ""))

    report = verify_unsigned_tx(unsigned_tx, input_boxes, await wallet_trees(ns, node_url), policy)
    summary = (
        f"wallet spends {report.erg_spent / 1e9:.6f} ERG, receives "
        f"{ {k[:8]: v for k, v in report.received.items()} }, service fee {report.service_fee / 1e9:.4f} ERG"
    )
    print(f"  TX guard OK: {summary}")
    if not execute:
        raise DryRun("TX verified but not signed (dry run). Re-run with --execute to sign and submit.")

    status = await _get_json(ns, f"{node_url}/wallet/status")
    if not status or not status.get("isUnlocked"):
        raise TxGuardError(
            "node wallet is locked, so it cannot sign. Unlock it with POST /wallet/unlock "
            "(body {\"pass\": \"<wallet password>\"}) and re-run."
        )

    sign_request = {
        "tx": {
            "inputs": [{"boxId": i["boxId"], "extension": i.get("extension", {})} for i in inputs],
            "dataInputs": [{"boxId": d["boxId"]} for d in data_inputs],
            "outputs": unsigned_tx["outputs"],
        },
        "inputsRaw": inputs_raw,
        "dataInputsRaw": data_raw,
        "secrets": {},
    }
    async with ns.post(f"{node_url}/wallet/transaction/sign", json=sign_request, timeout=aiohttp.ClientTimeout(total=30)) as r:
        body = await r.text()
        if r.status != 200:
            raise Exception(f"Sign failed: HTTP {r.status} - {body[:500]}")
        return await r.json()


async def check_on_node(ns, node_url: str, signed_tx: dict) -> tuple[bool, str]:
    """Ask the node to fully validate a signed TX (scripts included) without broadcasting it."""
    async with ns.post(f"{node_url}/transactions/check", json=signed_tx, timeout=aiohttp.ClientTimeout(total=30)) as r:
        body = await r.text()
        return r.status == 200, body[:800]
