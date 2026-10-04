"""run_arb picks the trade size itself (on the boxes it is about to spend) when none is given."""
import asyncio

import pytest

import config
import ergo.arb_runner as runner
from arbitrage.sizing import Market, best_size
from ergo.signing import DryRun
from tests.test_bank_redeem_tx import BANK_BOX, ORACLE_BOX
from tests.test_chain_arb import OUR_TREE, pool_box

POOL = pool_box(0.35, 2_000 * 10**9)  # pool buy -> bank redeem is profitable


def wallet(erg):
    return [{"boxId": "w", "value": int(erg * 1e9), "ergoTree": OUR_TREE, "assets": []}]


class TestChooseSize:
    def test_matches_best_size_on_the_same_boxes(self):
        c = runner.choose_size("redeem", POOL, BANK_BOX, ORACLE_BOX, wallet(1_000))
        expected = best_size("redeem", Market.from_boxes(POOL, BANK_BOX, ORACLE_BOX), config.MAX_TRADE_SIZE_ERG)
        assert c.ok and c.size_nanoerg == expected.size_nanoerg

    def test_wallet_minus_reserve_caps_it(self):
        c = runner.choose_size("redeem", POOL, BANK_BOX, ORACLE_BOX, wallet(4))
        assert c.cap_erg == pytest.approx(4 - config.LIVE_ERG_RESERVE)
        assert c.size_erg <= 3

    def test_caller_cap(self):
        c = runner.choose_size("redeem", POOL, BANK_BOX, ORACLE_BOX, wallet(1_000), max_erg_in=2 * 10**9)
        assert c.cap_erg == pytest.approx(2) and c.size_erg <= 2

    def test_max_trade_size_caps_it(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_TRADE_SIZE_ERG", 5.0)
        c = runner.choose_size("redeem", POOL, BANK_BOX, ORACLE_BOX, wallet(1_000))
        assert c.cap_erg == pytest.approx(5)


def offline_runner(monkeypatch):
    """run_arb with node calls replaced; signing stops at the dry-run point."""
    boxes = {config.SPECTRUM_SIGUSD_POOL_NFT: POOL, config.SIGMAUSD_BANK_NFT: BANK_BOX,
             config.SIGMAUSD_ORACLE_NFT: ORACLE_BOX}
    pending: dict = {}

    async def latest(ns, nft, explorer=None):
        return boxes[nft], pending.get(nft, False)

    async def ctx(ns):
        return wallet(1_000), 1_900_000, OUR_TREE

    async def trees(ns, node):
        return {OUR_TREE}

    async def sign(ns, node, tx, policy, *, execute, log=print):
        raise DryRun()

    monkeypatch.setattr(runner, "latest_box", latest)
    monkeypatch.setattr(runner, "wallet_context", ctx)
    monkeypatch.setattr(runner, "wallet_trees", trees)
    monkeypatch.setattr(runner, "guarded_sign", sign)
    return pending


def test_run_arb_without_size_uses_best_size(offline):
    logs = []
    r = asyncio.run(runner.run_arb(None, "redeem", None, log=logs.append))
    expected = runner.choose_size("redeem", POOL, BANK_BOX, ORACLE_BOX, wallet(1_000))
    assert r.status == "dry_run"
    assert r.erg_in == expected.size_nanoerg
    assert r.profit_nanoerg / 1e9 == pytest.approx(expected.profit_erg, abs=1e-9)
    assert any("Best size" in line for line in logs)


def test_run_arb_without_size_stops_when_nothing_is_worth_trading(offline, monkeypatch):
    monkeypatch.setattr(config, "MIN_PROFIT_PERCENT", 50.0)
    logs = []
    r = asyncio.run(runner.run_arb(None, "redeem", None, log=logs.append))
    assert r.status == "not_profitable" and "MIN_PROFIT_PERCENT" in r.message


def test_run_arb_with_explicit_size_keeps_it(offline):
    r = asyncio.run(runner.run_arb(None, "redeem", 3 * 10**9, log=lambda m: None))
    assert r.erg_in == 3 * 10**9


def test_runner_spends_the_pending_pool_box():
    from tests.fake_node import FakeSession
    nft = config.SPECTRUM_SIGUSD_POOL_NFT
    pending = dict(POOL, boxId="pool-pending")
    ns = FakeSession({f"/transactions/unconfirmed/outputs/byTokenId/{nft}": (200, [pending]),
                      "/utxo/withPool/byId/pool-pending": (200, pending)})
    (got,) = asyncio.run(runner._fetch_boxes(ns, (nft,)))
    assert got["boxId"] == "pool-pending"


def test_cli_reads_the_pending_bank_box():
    from ergo import actions
    from tests.fake_node import FakeSession
    nft = config.SIGMAUSD_BANK_NFT
    pending = dict(BANK_BOX, boxId="bank-pending")
    ns = FakeSession({f"/transactions/unconfirmed/outputs/byTokenId/{nft}": (200, [pending]),
                      "/utxo/withPool/byId/bank-pending": (200, pending)})
    (got,) = asyncio.run(actions._boxes(ns, nft))
    assert got["boxId"] == "bank-pending"


def test_run_arb_waits_while_an_oracle_update_is_pending(offline):
    offline[config.SIGMAUSD_ORACLE_NFT] = True
    logs = []
    r = asyncio.run(runner.run_arb(None, "redeem", 3 * 10**9, log=logs.append))
    assert r.status == "aborted" and "oracle update pending" in r.message


def test_force_ignores_a_pending_oracle_update(offline):
    offline[config.SIGMAUSD_ORACLE_NFT] = True
    r = asyncio.run(runner.run_arb(None, "redeem", 3 * 10**9, force=True, log=lambda m: None))
    assert r.status == "dry_run"


def test_redeem_leg2_rebuild_is_not_ready_while_an_oracle_update_is_pending(offline):
    offline[config.SIGMAUSD_ORACLE_NFT] = True
    with pytest.raises(runner.LegNotReady):
        asyncio.run(runner._leg2_boxes(None, "redeem"))
    (pool,) = asyncio.run(runner._leg2_boxes(None, "mint"))  # pool sell does not use the oracle
    assert pool is POOL


def test_leg2_rebuild_boxes(offline):
    bank, oracle = asyncio.run(runner._leg2_boxes(None, "redeem"))
    assert bank is BANK_BOX and oracle is ORACLE_BOX
    (pool,) = asyncio.run(runner._leg2_boxes(None, "mint"))
    assert pool is POOL
