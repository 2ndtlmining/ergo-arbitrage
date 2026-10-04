"""Smaller robustness fixes (#74) and a safer `arb.py send` plus a remote http:// warning (#67)."""
import asyncio
import logging
import time

import pytest

import config
from exchanges.ergo_node import ErgoNodeClient, parse_wallet_balances
from tests.test_live import WALLET, live  # noqa: F401  (fixture)

SIGUSD = config.SIGUSD_TOKEN_ID


def run(coro):
    return asyncio.run(coro)


# ---------- wallet balance shape ----------

def test_scala_shape():
    b = parse_wallet_balances({"height": 1, "balance": 2_500_000_000, "assets": {SIGUSD: 150}})
    assert b == {"erg": 2.5, "tokens": {SIGUSD: {"amount": 150}}}


def test_list_shape_is_understood():
    b = parse_wallet_balances({"balance": 10**9, "assets": [{"tokenId": SIGUSD, "amount": 7}]})
    assert b["tokens"][SIGUSD]["amount"] == 7


@pytest.mark.parametrize("data", [{"assets": {}}, {"balance": "lots", "assets": {}}, {"balance": 1, "assets": "x"},
                                  {"balance": 1, "assets": [{"id": "x"}]}, [], "nope"])
def test_unexpected_shapes_are_unreadable_not_zero(data):
    assert parse_wallet_balances(data) is None


def test_doctor_checks_the_balance_shape():
    import inspect
    import ergo.doctor as doctor
    assert "parse_wallet_balances" in inspect.getsource(doctor)


# ---------- cooldown on a monotonic clock ----------

def test_cooldown_ignores_a_wall_clock_jump(live, monkeypatch):  # noqa: F811
    run(live._execute_trades(WALLET, live._prices))
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3600)          # NTP step forward by an hour
    assert any("cooldown" in b for b in run(live._live_blockers(WALLET, live._prices)))


# ---------- episode heartbeat map ----------

def test_closed_episodes_leave_the_heartbeat_map(tmp_path):
    from types import SimpleNamespace
    from arbitrage.scanner import ArbitrageScanner
    s = ArbitrageScanner(mode="notify", db_path=str(tmp_path / "t.db"))
    s.episodes.update = lambda now, paths: []
    s.episodes.open = {"a": SimpleNamespace(db_id=1), "b": SimpleNamespace(db_id=None)}
    s._tick_episodes(0.0)
    assert set(s._episode_touched) == {1}
    s.episodes.open = {}
    s._tick_episodes(1.0)
    assert s._episode_touched == {}
    s.tracker.close()


# ---------- node errors logged once per outage ----------

class Boom:
    def get(self, *a, **k):
        raise OSError("connection refused")

    post = get


def test_node_errors_are_logged_once_per_outage(caplog):
    node = ErgoNodeClient()
    node.session = Boom()
    with caplog.at_level(logging.DEBUG, logger="ergo_arb"):
        for _ in range(5):
            run(node._get("/info"))
    errors = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(errors) == 1 and "connection refused" in errors[0].getMessage()


# ---------- arb.py send ----------

ADDR = "9fRAWhdxEsTcdb8PhGNrZfwqa65zfkuYHAMmkQLcic1gdLSV5vA"


class FakeNode:
    def __init__(self):
        self.built = False

    def get(self, url, timeout=None):
        body = {"isValid": True} if "/utils/address/" in url else {"tree": "0008cd02" + "11" * 32}
        return Resp(200, body)

    def post(self, url, json=None, timeout=None):
        self.built = True
        return Resp(500, None)


class Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return "no"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def send(ns, erg=1.0, sigusd=0.0, mode="dry", confirm=None):
    from ergo import actions
    lines = []
    run(actions.send(ns, ADDR, erg, sigusd, mode, lines.append, confirm=confirm))
    return "\n".join(lines)


def test_allow_list_refuses_other_addresses(monkeypatch):
    monkeypatch.setattr(config, "SEND_ALLOWED_ADDRESSES", {"9someOtherAddress"})
    ns = FakeNode()
    out = send(ns)
    assert "SEND_ALLOWED_ADDRESSES" in out and not ns.built


def test_allow_list_accepts_listed_addresses(monkeypatch):
    monkeypatch.setattr(config, "SEND_ALLOWED_ADDRESSES", {ADDR})
    ns = FakeNode()
    send(ns)
    assert ns.built


def test_sigusd_cap(monkeypatch):
    monkeypatch.setattr(config, "SEND_MAX_SIGUSD", 100.0)
    ns = FakeNode()
    out = send(ns, erg=0, sigusd=1000)
    assert "SEND_MAX_SIGUSD" in out and not ns.built


def test_execute_needs_the_address_end_typed_back():
    ns = FakeNode()
    out = send(ns, mode="execute", confirm=lambda address: False)
    assert "not confirmed" in out and not ns.built
    ns = FakeNode()
    send(ns, mode="execute", confirm=lambda address: True)
    assert ns.built


def test_cli_confirmation_compares_the_last_four_characters(monkeypatch):
    import arb
    monkeypatch.setattr("builtins.input", lambda prompt: ADDR[-4:])
    assert arb.confirm_address(ADDR)
    monkeypatch.setattr("builtins.input", lambda prompt: "abcd")
    assert not arb.confirm_address(ADDR)


# ---------- remote http:// node URL ----------

@pytest.mark.parametrize("url", ["http://127.0.0.1:9053", "http://localhost:9053", "http://192.168.40.86:9053",
                                 "http://10.0.0.5:9053", "https://node.example.com", "http://[::1]:9053"])
def test_local_or_tls_node_urls_are_fine(url):
    assert config.node_url_warning(url) is None


@pytest.mark.parametrize("url", ["http://8.8.8.8:9053", "http://node.example.com:9053"])
def test_remote_http_node_urls_warn(url):
    w = config.node_url_warning(url)
    assert w and "API key" in w
