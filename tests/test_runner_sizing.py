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


@pytest.fixture
def offline(monkeypatch):
    """run_arb with node calls replaced; signing stops at the dry-run point."""
    async def fetch(ns, nfts):
        boxes = {config.SPECTRUM_SIGUSD_POOL_NFT: POOL, config.SIGMAUSD_BANK_NFT: BANK_BOX,
                 config.SIGMAUSD_ORACLE_NFT: ORACLE_BOX}
        return [boxes[n] for n in nfts]

    async def ctx(ns):
        return wallet(1_000), 1_900_000, OUR_TREE

    async def trees(ns, node):
        return {OUR_TREE}

    async def sign(ns, node, tx, policy, *, execute):
        raise DryRun()

    monkeypatch.setattr(runner, "_fetch_boxes", fetch)
    monkeypatch.setattr(runner, "wallet_context", ctx)
    monkeypatch.setattr(runner, "wallet_trees", trees)
    monkeypatch.setattr(runner, "guarded_sign", sign)


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
