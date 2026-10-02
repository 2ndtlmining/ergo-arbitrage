"""Discord delivery: queue + worker, post/edit with ?wait=true, 429 retry, bounded queue (spec)."""
import asyncio

import pytest

import config
from notifications.discord import QUEUE_MAX, DiscordNotifier

HOOK = "https://discord.com/api/webhooks/1/abc"


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status, self._body, self.headers = status, body, headers or {}

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeHook:
    """Replies from a script per call; records (method, url, payload)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, json=None, timeout=None):
        self.calls.append((method, url, json))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, BaseException):
            raise r
        return r


@pytest.fixture
def notifier(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", HOOK)
    monkeypatch.setattr(config, "DISCORD_ENABLED", True)
    n = DiscordNotifier()
    n.slept = []

    async def fake_sleep(s):
        n.slept.append(s)

    n._sleep = fake_sleep
    return n


def run(coro):
    return asyncio.run(coro)


def test_post_then_edit_uses_the_returned_message_id(notifier):
    hook = FakeHook(Resp(200, {"id": "m1"}), Resp(200, {"id": "m1"}))
    notifier._session = hook
    got = {}

    async def go():
        notifier.post({"title": "open"}, content="<@1>", on_id=lambda mid: got.setdefault("id", mid))
        notifier.edit(lambda: got.get("id"), {"title": "closed"})
        await notifier.stop()

    run(go())
    (m1, u1, p1), (m2, u2, p2) = hook.calls
    assert (m1, u1) == ("POST", HOOK + "?wait=true") and p1 == {"content": "<@1>", "embeds": [{"title": "open"}]}
    assert (m2, u2) == ("PATCH", HOOK + "/messages/m1") and p2 == {"embeds": [{"title": "closed"}]}


def test_edit_without_message_id_is_dropped(notifier):
    hook = FakeHook(Resp(500, {"message": "boom"}))
    notifier._session = hook

    async def go():
        notifier.post({"title": "open"})
        notifier.edit(lambda: None, {"title": "closed"})
        await notifier.stop()

    run(go())
    assert [c[0] for c in hook.calls] == ["POST"]


def test_429_waits_and_retries_once_then_gives_up(notifier):
    hook = FakeHook(Resp(429, {"retry_after": 2.5}), Resp(200, {"id": "m1"}))
    notifier._session = hook
    run(notifier._request("POST", HOOK, {}))
    assert notifier.slept == [2.5] and len(hook.calls) == 2
    hook2 = FakeHook(Resp(429, {"retry_after": 99}))
    notifier._session, notifier.slept = hook2, []
    status, _ = run(notifier._request("POST", HOOK, {}))
    assert status == 429 and notifier.slept == [30] and len(hook2.calls) == 2


def test_rate_limit_headers_delay_the_next_request(notifier):
    hook = FakeHook(Resp(200, {"id": "a"}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "1.5"}),
                    Resp(200, {"id": "b"}))

    async def go():
        notifier._session = hook
        await notifier._request("POST", HOOK, {})
        await notifier._request("POST", HOOK, {})

    run(go())
    assert notifier.slept and 0 < notifier.slept[0] <= 1.5


def test_full_queue_drops_the_oldest_post_never_an_edit(notifier):
    notifier._start_worker = lambda: None  # keep jobs queued
    notifier.edit(lambda: "m0", {"title": "edit"})
    for i in range(QUEUE_MAX + 5):
        notifier.post({"title": f"p{i}"})
    kinds = [job[0] for job in notifier._jobs]
    assert len(notifier._jobs) == QUEUE_MAX and kinds[0] == "edit"
    assert notifier._jobs[1][1]["title"] == "p6"


def test_worker_survives_exceptions(notifier):
    hook = FakeHook(RuntimeError("network down"), Resp(200, {"id": "m2"}))
    notifier._session = hook
    got = []

    async def go():
        notifier.post({"title": "a"})
        notifier.post({"title": "b"}, on_id=got.append)
        await notifier.stop()

    run(go())
    assert got == ["m2"]


def test_disabled_notifier_queues_nothing(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_ENABLED", False)
    n = DiscordNotifier()
    n.post({"title": "x"})
    assert len(n._jobs) == 0


def test_wallet_analysis_is_one_embed(notifier):
    hook = FakeHook(Resp(200, {"id": "w"}))
    notifier._session = hook

    async def go():
        await notifier.send_wallet_analysis({"erg": 20.7}, {"erg": {"balance": 20.7, "options": []}})
        await notifier.stop()

    run(go())
    assert len(hook.calls) == 1 and hook.calls[0][2]["embeds"][0]["title"] == "Wallet"
