"""Phase 1 of the review plan (#90): the bot must not trust a syncing/stalled node (#68), mempool lookup
errors fail closed (#69), the explorer fallback backs off and a poll cannot stall (#70), and the STOP
file, database and log do not depend on the current directory (#62)."""
import asyncio
from pathlib import Path

import pytest

import config
import ergo.chain as chain
import ergo.chain_state as cs

REPO = Path(__file__).resolve().parent.parent


class Resp:
    def __init__(self, status, body=None):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Node:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, timeout=None, **kw):
        self.calls.append(url)
        for part, resp in self.routes.items():
            if part in url:
                if isinstance(resp, BaseException):
                    raise resp
                return resp
        return Resp(404)

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append("POST " + url)
        return self.routes.get("POST", Resp(404))


def run(coro):
    return asyncio.run(coro)


# --- #68: syncing / stalled / wrong network --------------------------------------------------------

def info(full, headers=None, network=None):
    body = {"fullHeight": full}
    if headers is not None:
        body["headersHeight"] = headers
    if network is not None:
        body["network"] = network
    return Node({"/info": Resp(200, body)})


def test_a_synced_node_gives_its_height():
    assert run(cs._height(info(1_886_100, 1_886_100, "mainnet"))) == 1_886_100


def test_a_syncing_node_is_refused():
    with pytest.raises(RuntimeError, match="syncing: 686100 blocks behind"):
        run(cs._height(info(1_200_000, 1_886_100)))


def test_headers_height_missing_counts_as_synced():
    assert run(cs._height(info(1_886_100))) == 1_886_100          # a node that does not report it


def test_a_node_on_another_network_is_refused():
    with pytest.raises(RuntimeError, match="testnet"):
        run(cs._height(info(100, 100, "testnet")))


def test_a_stalled_node_is_refused_until_a_new_block_arrives(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cs.time, "monotonic", lambda: now[0])
    node = info(1_886_100, 1_886_100)
    assert run(cs._height(node)) == 1_886_100
    now[0] += config.CHAIN_STALL_SECONDS - 1
    assert run(cs._height(node)) == 1_886_100                      # quiet but not yet stalled
    now[0] += 2
    with pytest.raises(RuntimeError, match="no new block"):
        run(cs._height(node))
    assert run(cs._height(info(1_886_101, 1_886_101))) == 1_886_101   # a new block clears it


# --- #69: mempool lookup errors fail closed --------------------------------------------------------

NFT = "ab" * 32


def test_a_mempool_get_error_fails_the_read():
    node = Node({"/transactions/unconfirmed/outputs/byTokenId/": Resp(503)})
    with pytest.raises(RuntimeError, match="mempool lookup.*503"):
        run(cs._mempool_outputs_by_token(node, NFT))


def test_a_mempool_post_error_fails_the_read():
    cs._MEMPOOL_BY_TOKEN_POST = True
    node = Node({"POST": Resp(500)})
    with pytest.raises(RuntimeError, match="mempool lookup.*500"):
        run(cs._mempool_outputs_by_token(node, NFT))


def test_an_empty_mempool_is_still_fine():
    node = Node({"/transactions/unconfirmed/outputs/byTokenId/": Resp(200, [])})
    assert run(cs._mempool_outputs_by_token(node, NFT)) == []


# --- #70: explorer backoff, one quick attempt on the poll path, poll timeout --------------------

class Explorer:
    def __init__(self, resp):
        self.resp, self.calls = resp, 0

    def get(self, url, timeout=None):
        self.calls += 1
        if isinstance(self.resp, BaseException):
            raise self.resp
        return self.resp


def test_a_failed_explorer_lookup_backs_off(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(chain.time, "monotonic", lambda: now[0])
    explorer = Explorer(Resp(500))
    with pytest.raises(RuntimeError):
        run(chain.find_box_id(NFT, None, explorer, attempts=1))
    with pytest.raises(RuntimeError, match="backing off"):
        run(chain.find_box_id(NFT, None, explorer, attempts=1))
    assert explorer.calls == 1                                     # the second call made no request
    now[0] += chain.EXPLORER_BACKOFF_MAX + 1
    explorer.resp = Resp(200, {"items": [{"boxId": "b1"}]})
    assert run(chain.find_box_id(NFT, None, explorer, attempts=1)) == "b1"
    assert NFT not in chain._EXPLORER_BACKOFF                      # success clears the backoff


def test_the_poll_path_makes_one_quick_explorer_attempt(monkeypatch):
    seen = {}

    async def fake_find(token_id, ns=None, explorer=None, retry_delay=2.0, attempts=3, timeout=None):
        seen.update(attempts=attempts, timeout=timeout)
        return "b1"

    async def read(ns, box_id):
        return {"boxId": box_id}

    monkeypatch.setattr(cs, "find_box_id", fake_find)
    cs._BOX_IDS.pop(NFT, None)
    run(cs._confirmed(None, NFT, None, read))
    assert seen["attempts"] == 1 and seen["timeout"] is not None and seen["timeout"].total <= 5


def test_a_hanging_chain_read_cannot_stall_the_poll(tmp_path, monkeypatch):
    from arbitrage.scanner import ArbitrageScanner
    import arbitrage.scanner as scanner_module

    async def hang(ns, explorer=None):
        await asyncio.sleep(30)

    monkeypatch.setattr(scanner_module, "read_snapshot", hang)
    monkeypatch.setattr(scanner_module, "CHAIN_READ_TIMEOUT_SECONDS", 0.2)
    s = ArbitrageScanner(db_path=str(tmp_path / "t.db"))
    try:
        assert run(asyncio.wait_for(s._read_chain(), 2)) is None
        assert "timed out" in s._chain_error
    finally:
        s.tracker.close()


# --- #62: paths do not depend on the current directory ----------------------------------------

def test_stop_file_database_and_log_resolve_against_the_repo(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                                   # started from another folder
    assert config.repo_path("STOP") == REPO / "STOP"
    assert config.repo_path(str(tmp_path / "x.db")) == tmp_path / "x.db"   # absolute stays as given
    import main
    assert Path(main.build_parser().parse_args([]).db) == REPO / "arbitrage_tracker.db"
    import logging_config
    import inspect
    assert inspect.signature(logging_config.setup_logging).parameters["log_path"].default == str(REPO / "arbitrage.log")


def test_the_live_gate_looks_for_stop_in_the_repo(monkeypatch, tmp_path):
    from arbitrage.scanner import ArbitrageScanner
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "LIVE_STOP_FILE", "STOP_TEST_MARKER")
    marker = REPO / "STOP_TEST_MARKER"
    s = ArbitrageScanner(mode="live", db_path=str(tmp_path / "t.db"))
    try:
        marker.write_text("stop")
        assert any("STOP file present" in b for b in s._sync_global_blockers(None, {}))
    finally:
        marker.unlink(missing_ok=True)
        s.tracker.close()


def test_an_unsupported_endpoint_is_not_an_error():
    """Older Scala nodes answer 400 on the GET form; a node with neither form means nothing pending."""
    node = Node({"/transactions/unconfirmed/outputs/byTokenId/": Resp(400), "POST": Resp(404)})
    assert run(cs._mempool_outputs_by_token(node, NFT)) == []


@pytest.mark.parametrize("name", ["mainnet", "Mainnet", "main", ""])
def test_spellings_of_mainnet_are_accepted(name):
    assert run(cs._height(info(100, 100, name))) == 100
