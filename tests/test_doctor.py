"""`arb.py doctor`: one command that checks the node is ready for the bot (Rust node migration)."""
import asyncio

import aiohttp
import pytest

import ergo.doctor as doctor
from ergo.arb_runner import ArbResult
from tests.test_chain_scanner import snap


class Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Node:
    def __init__(self, info=None, wallet=None, unreachable=False):
        self.info = info if info is not None else {"name": "ergo-rust", "appVersion": "0.9", "fullHeight": 1_886_100,
                                                   "headersHeight": 1_886_100}
        self.wallet = wallet if wallet is not None else (200, {"isInitialized": True, "isUnlocked": True})
        self.unreachable = unreachable

    def get(self, url, timeout=None):
        if self.unreachable:
            raise aiohttp.ClientConnectionError("Cannot connect to host 192.168.40.86:9053")
        if url.endswith("/info"):
            return Resp(200, self.info)
        if url.endswith("/wallet/status"):
            return Resp(*self.wallet)
        return Resp(404, None)


@pytest.fixture
def healthy(monkeypatch):
    calls = {}

    async def ctx(ns):
        return [{"boxId": "w", "value": 20_710_000_000, "ergoTree": "0008cd02" + "22" * 32, "assets": []}], 1, "t"

    async def chain(ns, explorer=None):
        return snap()

    async def mempool(ns, nft):
        doctor.chain_state._MEMPOOL_BY_TOKEN_POST = True        # as if the node only had the POST form
        return []

    async def arb(ns, path, erg_in, **kw):
        calls["arb"] = (path, erg_in, kw)
        return ArbResult("checked", erg_in=erg_in, path=path)

    monkeypatch.setattr(doctor, "wallet_context", ctx)
    monkeypatch.setattr(doctor, "read_snapshot", chain)
    monkeypatch.setattr(doctor.chain_state, "_mempool_outputs_by_token", mempool)
    monkeypatch.setattr(doctor, "run_arb", arb)
    monkeypatch.setattr(doctor.config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(doctor.config, "DISCORD_ENABLED", False)
    monkeypatch.setattr(doctor.config, "PLACEHOLDERS", [])
    monkeypatch.setattr(doctor, "unknown_settings", lambda: [])
    return calls


def run(node, sign=True):
    lines = []
    code = asyncio.run(doctor.run_doctor(node, sign=sign, log=lines.append))
    return code, "\n".join(lines)


def test_a_ready_node_passes_every_check_and_the_sign_check_never_broadcasts(healthy):
    code, out = run(Node())
    assert code == 0 and "FAIL" not in out
    for needle in ("ergo-rust", "height 1886100", "API key", "unlocked", "20.71 ERG", "RR ", "POST", "checked"):
        assert needle in out, needle
    path, erg_in, kw = healthy["arb"]
    assert kw.get("check") is True and not kw.get("execute")              # signed + validated, not sent


def test_unreachable_node_explains_the_likely_causes(healthy):
    code, out = run(Node(unreachable=True))
    assert code == 1 and "FAIL" in out and "127.0.0.1" in out and "SKIP" in out


def test_still_syncing_is_a_warning(healthy):
    code, out = run(Node(info={"name": "ergo-rust", "fullHeight": 1_200_000, "headersHeight": 1_886_100}))
    assert "WARN" in out and "686100 blocks behind" in out


def test_rejected_api_key(healthy):
    code, out = run(Node(wallet=(403, {"error": 403})))
    assert code == 1 and "ERGO_NODE_API_KEY" in out


def test_locked_wallet(healthy):
    code, out = run(Node(wallet=(200, {"isInitialized": True, "isUnlocked": False})))
    assert code == 1 and "/wallet/unlock" in out


def test_no_wallet_yet(healthy):
    code, out = run(Node(wallet=(200, {"isInitialized": False, "isUnlocked": False})))
    assert code == 1 and "restore" in out.lower()


def test_sign_check_can_be_skipped(healthy):
    code, out = run(Node(), sign=False)
    assert code == 0 and "arb" not in healthy and "SKIP" in out


def test_a_refused_sign_check_fails(healthy, monkeypatch):
    async def refused(ns, path, erg_in, **kw):
        return ArbResult("refused", "node rejected the transaction", path=path)

    monkeypatch.setattr(doctor, "run_arb", refused)
    code, out = run(Node())
    assert code == 1 and "node rejected the transaction" in out


def test_extra_index_missing_is_explained(healthy, monkeypatch):
    async def no_index(ns, explorer=None):
        raise RuntimeError("HTTP 404 /blockchain/box/unspent/byTokenId")

    monkeypatch.setattr(doctor, "read_snapshot", no_index)
    code, out = run(Node())
    assert code == 1 and "extra index" in out.lower()


# ---------- settings and Discord (#84) ----------

@pytest.fixture
def webhook(monkeypatch, healthy):
    seen = {}

    def use(status, body):
        async def info(url):
            seen["url"] = url
            return status, body
        monkeypatch.setattr(doctor, "webhook_info", info)
        monkeypatch.setattr(doctor.config, "DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/tok")
        monkeypatch.setattr(doctor.config, "DISCORD_ENABLED", True)
    return use, seen


def test_discord_webhook_name_is_reported_without_posting(webhook):
    use, seen = webhook
    use(200, {"name": "arb-bot", "channel_id": "42"})
    code, out = run(Node())
    assert code == 0 and "arb-bot" in out and seen["url"].endswith("/webhooks/1/tok")


def test_a_deleted_webhook_is_a_warning_not_a_node_failure(webhook):
    use, _ = webhook
    use(404, {"message": "Unknown Webhook"})
    code, out = run(Node())
    assert code == 0 and "WARN" in out and "Unknown Webhook" in out


def test_no_webhook_says_notify_posts_nothing(healthy):
    code, out = run(Node())
    assert code == 0 and "DISCORD_WEBHOOK_URL" in out


def test_placeholders_and_unknown_settings_are_warned(healthy, monkeypatch):
    monkeypatch.setattr(doctor.config, "PLACEHOLDERS", ["DISCORD_USER_ID"])
    monkeypatch.setattr(doctor, "unknown_settings", lambda: [("DISCORD_WEBHOOK", "DISCORD_WEBHOOK_URL")])
    code, out = run(Node())
    assert code == 0 and "DISCORD_USER_ID" in out and "DISCORD_WEBHOOK_URL?" in out and "arb.py config" in out
