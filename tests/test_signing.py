"""guarded_sign: resolve inputs from the node, verify, and only then sign (issue #7)."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

import config
from ergo.signing import DryRun, check_on_node, guarded_sign
from ergo.tx_guard import SignPolicy, TxGuardError

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "crux_swap_erg_to_sigusd.json").read_text())
WALLET_TREE = FIXTURE["input_boxes"][1]["ergoTree"]
WALLET_PUBKEY = WALLET_TREE[6:]
NODE = "http://node"


class FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeNode:
    """Minimal stand-in for an aiohttp session talking to the node."""

    def __init__(self, boxes):
        self.boxes = {b["boxId"]: b for b in boxes}
        self.sign_requests = []
        self.checked = []
        self.check_status = 200
        self.unlocked = True

    def get(self, url, **kw):
        path = url[len(NODE):]
        if path == "/wallet/status":
            return FakeResp(200, {"isUnlocked": self.unlocked})
        if path == "/wallet/addresses":
            return FakeResp(200, ["9wallet"])
        if path == "/utils/addressToRaw/9wallet":
            return FakeResp(200, {"raw": WALLET_PUBKEY})
        if path.startswith("/utxo/withPool/byIdBinary/"):
            return FakeResp(200, {"bytes": "aa"})
        if path.startswith("/utxo/withPool/byId/"):
            box = self.boxes.get(path.rsplit("/", 1)[1])
            return FakeResp(200 if box else 404, box or {})
        raise AssertionError(f"unexpected GET {path}")

    def post(self, url, json=None, **kw):
        if url == f"{NODE}/transactions/check":
            self.checked.append(json)
            return FakeResp(self.check_status, "txid" if self.check_status == 200 else {"detail": "Script reduced to false"})
        assert url == f"{NODE}/wallet/transaction/sign"
        self.sign_requests.append(json)
        return FakeResp(200, {"id": "signed"})


def run(coro):
    return asyncio.run(coro)


POLICY = SignPolicy(max_erg_spent=int(3e9), min_received={config.SIGUSD_TOKEN_ID: 60})


def test_signs_when_guard_passes():
    node = FakeNode(FIXTURE["input_boxes"])
    signed = run(guarded_sign(node, NODE, copy.deepcopy(FIXTURE["unsigned_tx"]), POLICY, execute=True))
    assert signed == {"id": "signed"}
    assert len(node.sign_requests) == 1
    assert len(node.sign_requests[0]["inputsRaw"]) == len(FIXTURE["input_boxes"])


def test_uses_node_box_values_not_the_api_claims():
    # API lies about the wallet input value; the node's view is what counts
    tx = copy.deepcopy(FIXTURE["unsigned_tx"])
    for i in tx["inputs"]:
        i["value"] = "1"
    node = FakeNode(FIXTURE["input_boxes"])
    run(guarded_sign(node, NODE, tx, POLICY, execute=True))
    assert node.sign_requests


def test_never_signs_when_guard_fails():
    tx = copy.deepcopy(FIXTURE["unsigned_tx"])
    tx["outputs"][3]["ergoTree"] = "0008cd03" + "ab" * 32
    node = FakeNode(FIXTURE["input_boxes"])
    with pytest.raises(TxGuardError):
        run(guarded_sign(node, NODE, tx, POLICY, execute=True))
    assert node.sign_requests == []


def test_unknown_input_box_is_rejected():
    node = FakeNode(FIXTURE["input_boxes"][1:])
    with pytest.raises(TxGuardError, match="not found"):
        run(guarded_sign(node, NODE, copy.deepcopy(FIXTURE["unsigned_tx"]), POLICY, execute=True))
    assert node.sign_requests == []


def test_dry_run_verifies_but_never_signs():
    node = FakeNode(FIXTURE["input_boxes"])
    with pytest.raises(DryRun):
        run(guarded_sign(node, NODE, copy.deepcopy(FIXTURE["unsigned_tx"]), POLICY, execute=False))
    assert node.sign_requests == []


def test_dry_run_still_reports_guard_failures():
    tx = copy.deepcopy(FIXTURE["unsigned_tx"])
    tx["outputs"][3]["ergoTree"] = "0008cd03" + "ab" * 32
    node = FakeNode(FIXTURE["input_boxes"])
    with pytest.raises(TxGuardError):
        run(guarded_sign(node, NODE, tx, POLICY, execute=False))


def test_check_on_node_valid():
    node = FakeNode([])
    ok, detail = run(check_on_node(node, NODE, {"id": "signed"}))
    assert ok and node.checked == [{"id": "signed"}]


def test_check_on_node_rejected():
    node = FakeNode([])
    node.check_status = 400
    ok, detail = run(check_on_node(node, NODE, {"id": "signed"}))
    assert not ok and "Script reduced to false" in detail


def test_locked_wallet_is_reported_before_signing():
    node = FakeNode(FIXTURE["input_boxes"])
    node.unlocked = False
    with pytest.raises(TxGuardError, match="locked"):
        run(guarded_sign(node, NODE, copy.deepcopy(FIXTURE["unsigned_tx"]), POLICY, execute=True))
    assert node.sign_requests == []


def test_locked_wallet_does_not_block_dry_run():
    node = FakeNode(FIXTURE["input_boxes"])
    node.unlocked = False
    with pytest.raises(DryRun):
        run(guarded_sign(node, NODE, copy.deepcopy(FIXTURE["unsigned_tx"]), POLICY, execute=False))
