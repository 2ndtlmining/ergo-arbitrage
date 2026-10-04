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
    assert [c[0] for c in hook.calls] == ["POST"] * 3          # a 5xx is retried twice; the edit is dropped


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



def test_text_alerts_are_queued_not_awaited(notifier, monkeypatch):
    """Review: notify_live/notify_watch must not hold the poll while Discord hangs."""
    sent = []

    async def hanging_send(content):
        sent.append(content)
        await asyncio.Event().wait()

    monkeypatch.setattr(notifier, "_send", hanging_send)

    async def go():
        await asyncio.wait_for(notifier.notify_live("executed"), 0.5)
        await asyncio.wait_for(notifier.notify_watch("Kucoin", "gap"), 0.5)
        await notifier.stop(timeout=0.2)

    run(go())
    assert sent and "executed" in sent[0]


def test_text_alerts_are_delivered_in_order(notifier, monkeypatch):
    sent = []

    async def fast_send(content):
        sent.append(content)
        return True

    monkeypatch.setattr(notifier, "_send", fast_send)

    async def go():
        notifier.post({"title": "x"})  # an embed job in between keeps FIFO
        await notifier.notify_live("a", ping=False)
        await notifier.notify_watch("Kucoin", "b")
        await notifier.stop()

    notifier._session = FakeHook(Resp(200, {"id": "m"}))
    run(go())
    assert [s.split("**")[-1].strip(": ") if "LIVE" in s else "watch" for s in sent] == ["a", "watch"]



class HtmlResp(Resp):
    """A Cloudflare-style 429: HTML body, so .json() fails; only the Retry-After header helps."""

    async def json(self, content_type=None):
        raise ValueError("not json")


def test_non_json_429_waits_on_the_header_and_retries(notifier):
    hook = FakeHook(HtmlResp(429, "<html>", {"Retry-After": "4"}), Resp(200, {"id": "m1"}))
    notifier._session = hook
    status, body = run(notifier._request("POST", HOOK, {}))
    assert status == 200 and notifier.slept == [4.0] and len(hook.calls) == 2


def test_thread_webhook_keeps_its_query_string(notifier, monkeypatch):
    notifier.webhook_url = HOOK + "?thread_id=9"
    hook = FakeHook(Resp(200, {"id": "m1"}))
    notifier._session = hook
    got = {}

    async def go():
        notifier.post({"title": "a"}, on_id=lambda mid: got.setdefault("id", mid))
        notifier.edit(lambda: got.get("id"), {"title": "b"})
        await notifier.stop()

    run(go())
    assert hook.calls[0][1] == HOOK + "?thread_id=9&wait=true"
    assert hook.calls[1][1] == HOOK + "/messages/m1?thread_id=9"


def test_post_outside_an_event_loop_is_kept_for_later(notifier):
    hook = FakeHook(Resp(200, {"id": "m1"}))
    notifier._session = hook
    notifier.post({"title": "early"})  # no running loop: must not raise
    run(notifier.stop())
    assert [c[2]["embeds"][0]["title"] for c in hook.calls] == ["early"]



def test_a_full_queue_never_drops_a_live_alert(notifier):
    """Review: notify_live is the only ping for a live failure; queue pressure must not evict it."""
    notifier._start_worker = lambda: None
    run(notifier.notify_live("LEG 2 FAILED", ping=False))
    for i in range(QUEUE_MAX + 5):
        notifier.post({"title": f"p{i}"})
    assert any(j[0] == "text" and "LEG 2 FAILED" in j[1] for j in notifier._jobs)
    assert len(notifier._jobs) == QUEUE_MAX


def test_shutdown_does_not_wait_out_a_long_rate_limit(notifier):
    """Review: stop() drains for 10 s; a 25 s retry_after must not keep the worker past it."""
    hook = FakeHook(Resp(429, {"retry_after": 25}), Resp(200, {"id": "m1"}))
    notifier._session = hook

    async def go():
        notifier.post({"title": "closing"})
        await notifier.stop(timeout=10)

    run(go())
    assert all(s < 10 for s in notifier.slept) and len(hook.calls) == 1
