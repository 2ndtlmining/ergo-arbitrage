"""Exact trade sizing (issue #10): contract-exact profit curves and the size choice."""
import random

import pytest

import config
from arbitrage.sizing import Market, best_size, cents_for_size, profit_nanoerg
from ergo.chain_arb import plan_bank_mint_pool_sell, plan_pool_buy_redeem
from exchanges.base import PoolState
from exchanges.sigmausd import BankState, quote_mint_sigusd
from tests.test_bank_redeem_tx import BANK_BOX, ORACLE_BOX, UI_TREE
from tests.test_chain_arb import OUR_TREE, pool_box
from tests.test_mint_tx import ORACLE_BOX as MINT_ORACLE, bank_box as mint_bank

BIG_WALLET = [{"boxId": "w", "value": 10**15, "ergoTree": OUR_TREE, "assets": []}]
ORACLE_R4 = 3_100_000_000


def market(sigusd_per_erg, pool_erg=100_000, bank=None, oracle=None):
    return Market.from_boxes(pool_box(sigusd_per_erg, pool_erg * 10**9), bank or BANK_BOX, oracle or ORACLE_BOX)


def redeem_market(pool_erg=2_000):
    """Pool sells SigUSD cheap (0.35/ERG) vs the bank redeem (~3.1 ERG each)."""
    return market(0.35, pool_erg)


def mint_market(pool_erg=2_000):
    """Pool buys SigUSD dear (0.30/ERG) vs the bank mint (~0.316/ERG); bank RR ~1008%."""
    return market(0.30, pool_erg, mint_bank(), MINT_ORACLE)


class TestExactProfit:
    @pytest.mark.parametrize("spot", [0.31, 0.33, 0.35])
    @pytest.mark.parametrize("erg", [1, 7.3, 50, 400])
    def test_redeem_matches_planner(self, spot, erg):
        erg_in = int(erg * 1e9)
        plan = plan_pool_buy_redeem(pool_box(spot), BANK_BOX, ORACLE_BOX, BIG_WALLET, erg_in,
                                    height=1, our_tree=OUR_TREE, ui_fee_tree=UI_TREE)
        assert profit_nanoerg("redeem", market(spot), erg_in) == (plan["profit_nanoerg"], erg_in)

    @pytest.mark.parametrize("spot", [0.28, 0.30, 0.35])
    @pytest.mark.parametrize("erg", [1, 7.3, 50, 400])
    def test_mint_matches_planner(self, spot, erg):
        budget = int(erg * 1e9)
        plan = plan_bank_mint_pool_sell(pool_box(spot), mint_bank(), MINT_ORACLE, BIG_WALLET, budget,
                                        height=1, our_tree=OUR_TREE, ui_fee_tree=UI_TREE)
        got = profit_nanoerg("mint", market(spot, bank=mint_bank(), oracle=MINT_ORACLE), budget)
        assert got == (plan["profit_nanoerg"], plan["erg_in"])

    def test_mint_blocked_by_reserve_ratio_is_none(self):
        # BANK_BOX: 1.6M ERG vs 160k USD * 3.1 -> RR ~322%, minting is closed
        assert profit_nanoerg("mint", market(0.30), 10 * 10**9) is None

    def test_from_pool_state_matches_from_boxes(self):
        box = pool_box(0.35, 2_000 * 10**9)
        state = BankState(BANK_BOX["value"], 16_000_000, ORACLE_R4)
        pool = PoolState(exchange="t", pool_id="p", token_x="ERG", token_y="SigUSD",
                         reserve_x=box["value"] / 1e9, reserve_y=box["assets"][2]["amount"] / 100,
                         fee_num=995, fee_denom=1000)
        assert Market.from_pool_state(pool, state) == redeem_market()


def grid_max(path, m, lo, hi, step=0.05):
    best = None
    x = lo
    while x <= hi + 1e-9:
        r = profit_nanoerg(path, m, int(x * 1e9))
        if r is not None and (best is None or r[0] > best):
            best = r[0]
        x += step
    return best / 1e9


class TestBestSize:
    def test_pure_max_beats_dense_grid(self):
        m = redeem_market()
        c = best_size("redeem", m, cap_erg=200, min_pct=0, capture=1.0)
        assert c.ok
        assert c.profit_erg >= grid_max("redeem", m, 1, 200) - 1e-6

    @pytest.mark.parametrize("seed", range(12))
    def test_random_markets_never_worse_than_grid(self, seed):
        rnd = random.Random(seed)
        spot = rnd.uniform(0.33, 0.40)
        m = market(spot, pool_erg=rnd.choice([500, 2_000, 20_000]))
        cap = rnd.choice([20, 150])
        c = best_size("redeem", m, cap_erg=cap, min_pct=0, capture=1.0)
        assert c.max_profit_erg >= grid_max("redeem", m, 1, cap, step=0.25) - 1e-6

    def test_capped(self):
        c = best_size("redeem", market(0.35, pool_erg=100_000), cap_erg=10, min_pct=0, capture=1.0)
        assert c.size_erg <= 10

    def test_capture_trades_a_little_profit_for_a_smaller_size(self):
        m = redeem_market()
        full = best_size("redeem", m, cap_erg=500, min_pct=0, capture=1.0)
        part = best_size("redeem", m, cap_erg=500, min_pct=0, capture=0.95)
        assert part.size_erg < full.size_erg * 0.85
        assert part.profit_erg >= 0.95 * full.profit_erg - 1e-6

    def test_min_profit_percent_shrinks_size(self):
        m = redeem_market()
        free = best_size("redeem", m, cap_erg=500, min_pct=0, capture=1.0)
        floor = free.profit_percent + 2  # the peak-profit size does not reach this %
        c = best_size("redeem", m, cap_erg=500, min_pct=floor, capture=1.0)
        assert c.ok
        assert c.profit_percent >= floor
        assert c.size_erg < free.size_erg

    def test_unreachable_percent_is_not_ok(self):
        c = best_size("redeem", redeem_market(), cap_erg=500, min_pct=50, capture=1.0)
        assert not c.ok and "MIN_PROFIT_PERCENT" in c.reason

    def test_break_even(self):
        m = redeem_market()
        c = best_size("redeem", m, cap_erg=500, min_pct=0, capture=1.0)
        assert profit_nanoerg("redeem", m, int(c.break_even_erg * 1e9))[0] >= 0
        assert profit_nanoerg("redeem", m, int((c.break_even_erg - 0.01) * 1e9))[0] < 0

    def test_unprofitable(self):
        c = best_size("redeem", market(0.31), cap_erg=500, min_pct=0.5, capture=0.95)
        assert not c.ok and c.break_even_erg is None and "no profitable size" in c.reason

    def test_cap_below_minimum(self):
        c = best_size("redeem", redeem_market(), cap_erg=0.5, min_pct=0, capture=1.0)
        assert not c.ok and "MIN_TRADE_SIZE_ERG" in c.reason

    def test_mint_path(self):
        m = mint_market()
        c = best_size("mint", m, cap_erg=500, min_pct=0, capture=1.0)
        assert c.ok and c.profit_erg >= grid_max("mint", m, 1, 500, step=0.25) - 1e-6
        assert c.cost_erg == pytest.approx(profit_nanoerg("mint", m, int(c.size_erg * 1e9))[1] / 1e9)

    def test_mint_blocked(self):
        c = best_size("mint", market(0.30), cap_erg=500, min_pct=0, capture=1.0)
        assert not c.ok and "400%" in c.reason

    def test_mint_capped_by_reserve_ratio(self):
        # bank just above 400%: only a little SigUSD can be minted before RR hits 400%
        bank = mint_bank(erg=2_000_000, circ_cents=16_000_000)  # 2M ERG vs 496k ERG liabilities -> ~403%
        m = market(0.30, pool_erg=100_000, bank=bank, oracle=MINT_ORACLE)
        c = best_size("mint", m, cap_erg=100_000, min_pct=0, capture=1.0)
        assert c.ok
        allowed = quote_mint_sigusd(m.bank, 10**18)
        assert profit_nanoerg("mint", m, int(c.size_erg * 1e9)) is not None
        assert c.size_erg * 1e9 <= m.mint_budget_for(allowed) + 1

    def test_defaults_from_config(self, monkeypatch):
        monkeypatch.setattr(config, "MIN_TRADE_SIZE_ERG", 5.0)
        monkeypatch.setattr(config, "SIZE_PROFIT_CAPTURE", 1.0)
        monkeypatch.setattr(config, "MIN_PROFIT_PERCENT", 0.0)
        c = best_size("redeem", redeem_market(), cap_erg=500)
        assert c.size_erg >= 5.0
        assert c.size_erg == pytest.approx(
            best_size("redeem", redeem_market(), cap_erg=500, min_pct=0, capture=1.0).size_erg)


class TestCentBoundaries:
    def test_redeem_size_is_least_erg_for_its_cents(self):
        m = redeem_market()
        c = best_size("redeem", m, cap_erg=500, min_pct=0, capture=0.95)
        assert cents_for_size("redeem", m, c.size_nanoerg) == c.sigusd_cents
        assert cents_for_size("redeem", m, c.size_nanoerg - 1) == c.sigusd_cents - 1

    @pytest.mark.parametrize("path,mk", [("redeem", redeem_market), ("mint", mint_market)])
    def test_planner_delivers_the_chosen_profit(self, path, mk):
        m = mk()
        c = best_size(path, m, cap_erg=300, min_pct=0, capture=0.95)
        if path == "redeem":
            plan = plan_pool_buy_redeem(pool_box(0.35, 2_000 * 10**9), BANK_BOX, ORACLE_BOX, BIG_WALLET,
                                        c.size_nanoerg, height=1, our_tree=OUR_TREE, ui_fee_tree=UI_TREE)
        else:
            plan = plan_bank_mint_pool_sell(pool_box(0.30, 2_000 * 10**9), mint_bank(), MINT_ORACLE, BIG_WALLET,
                                            c.size_nanoerg, height=1, our_tree=OUR_TREE, ui_fee_tree=UI_TREE)
        assert plan["sigusd_cents"] == c.sigusd_cents
        assert plan["profit_nanoerg"] / 1e9 == pytest.approx(c.profit_erg, abs=1e-9)


@pytest.mark.parametrize("path,mk", [("redeem", redeem_market), ("mint", mint_market)])
def test_never_below_min_trade_size(path, mk):
    for lo in (1.0, 2.5, 40.0):
        c = best_size(path, mk(), cap_erg=300, min_erg=lo, min_pct=0, capture=0.0)  # capture 0 -> smallest size
        assert c.ok and c.size_erg >= lo
        assert c.max_profit_size_erg >= lo


@pytest.mark.parametrize("lo", [1.0, 2.5, 40.0])
def test_lower_bound_is_strict_when_the_best_is_the_smallest(lo):
    c = best_size("redeem", market(0.31), cap_erg=300, min_erg=lo)  # losing market: least loss at the bottom
    assert not c.ok and c.max_profit_size_erg >= lo


@pytest.mark.parametrize("path,mk", [("redeem", redeem_market), ("mint", mint_market)])
def test_capture_zero_is_the_smallest_size_meeting_the_floor(path, mk):
    m = mk()
    free = best_size(path, m, cap_erg=300, min_erg=0.01, min_pct=0, capture=0.0)
    assert free.ok and free.size_erg == pytest.approx(free.break_even_erg)  # smallest non-losing size
    floored = best_size(path, m, cap_erg=300, min_erg=0.01, min_pct=1.0, capture=0.0)
    assert floored.profit_percent >= 1.0
    one_cent_less = profit_nanoerg(path, m, int(floored.size_erg * 1e9) - 1)  # previous cent boundary and below
    assert one_cent_less is None or one_cent_less[0] / one_cent_less[1] * 100 < 1.0


def test_capture_is_monotone():
    m = redeem_market()
    sizes = [best_size("redeem", m, cap_erg=500, min_pct=0, capture=k).size_erg for k in (0.5, 0.8, 0.95, 1.0)]
    assert sizes == sorted(sizes) and sizes[0] < sizes[-1]


def test_empty_wallet_is_not_reported_as_mint_blocked():
    c = best_size("mint", mint_market(), cap_erg=0.0)
    assert not c.ok and "400%" not in c.reason and "MIN_TRADE_SIZE_ERG" in c.reason
