"""CEX path math (issues #2, #3): every fee counted exactly once, bid/ask sides, correct profit sign,
SigUSD valued at its bank redeem value, and every path watch-only (no SigUSD<->USDT venue)."""
import pytest

import config
from arbitrage.calculator import WATCH_ONLY_REASON, ArbitrageCalculator, sigusd_redeem_value_usd
from exchanges.base import PoolState
from exchanges.sigmausd import BankState, affordable_mint_cents

C = ArbitrageCalculator()
PF = config.SPECTRUM_POOL_FEE
OPEN_BANK = BankState(1_281_250 * 10**9, 10_000_000, 3_125_000_000)    # RR 410%, oracle $0.32


def cex_dex(direction, **kw):
    args = dict(input_erg=100, cex_buy_price=0.30, cex_sell_price=0.30, dex_erg_sigusd_price=0.30,
                direction=direction, cex_trading_fee=0.002, erg_withdraw_fee=3.3, dex_execution_fee=0.785,
                sigusd_usd=1.0)
    args.update(kw)
    return C.calc_cex_vs_dex(**args)


def test_redeem_value_of_sigusd():
    assert sigusd_redeem_value_usd() == pytest.approx((1 - config.SIGMAUSD_PROTOCOL_FEE)
                                                     * (1 - config.SIGMAUSD_FRONTEND_FEE))


def test_buy_cex_sell_dex_at_equal_prices_loses_exactly_the_fees():
    opp = cex_dex("buy_cex_sell_dex")
    swapped = 100 * (1 - 0.002) - 3.3 - 0.785 - config.ERGO_TX_FEE
    assert opp.profit_erg == pytest.approx(swapped * (1 - PF) - 100)      # was -7.87 with the double count
    assert opp.output_erg == pytest.approx(100 + opp.profit_erg)


def test_withdrawal_fee_is_counted_once():
    a, b = cex_dex("buy_cex_sell_dex"), cex_dex("buy_cex_sell_dex", erg_withdraw_fee=4.3)
    assert a.profit_erg - b.profit_erg == pytest.approx(1 - PF)          # one extra ERG, less the pool fee


def test_trading_fee_is_applied():
    a, b = cex_dex("buy_cex_sell_dex"), cex_dex("buy_cex_sell_dex", cex_trading_fee=0.012)
    assert a.profit_erg - b.profit_erg == pytest.approx(100 * 0.01 * (1 - PF))


def test_buy_side_uses_the_ask_and_sell_side_the_bid():
    cheap_ask = cex_dex("buy_cex_sell_dex", cex_buy_price=0.29, cex_sell_price=0.99)
    assert cheap_ask.profit_erg > cex_dex("buy_cex_sell_dex").profit_erg
    high_bid = cex_dex("buy_dex_sell_cex", cex_sell_price=0.31, cex_buy_price=0.01)
    assert high_bid.profit_erg > cex_dex("buy_dex_sell_cex").profit_erg


def test_buy_dex_sell_cex_at_equal_prices_loses_the_fees():
    opp = cex_dex("buy_dex_sell_cex")
    erg = 100 * (1 - PF) - 0.785 - 2 * config.ERGO_TX_FEE                # swap, then deposit to the CEX
    assert opp.profit_erg == pytest.approx(erg * (1 - 0.002) - 100)


def test_dex_leg_uses_the_pool_reserves_when_known():
    pool = PoolState("pool", "p", "ERG", "SigUSD", reserve_x=1_000, reserve_y=300, fee_num=995)
    deep = cex_dex("buy_cex_sell_dex")
    shallow = cex_dex("buy_cex_sell_dex", pool=pool)                     # 100 ERG into a 1,000 ERG pool
    assert shallow.profit_erg < deep.profit_erg - 5                      # price impact shows up


def test_route_service_fee_defaults_to_the_configured_route(monkeypatch):
    monkeypatch.setattr(config, "POOL_SWAP_ROUTE", "direct")
    direct = cex_dex("buy_cex_sell_dex", dex_execution_fee=None)
    assert direct.profit_erg == pytest.approx(cex_dex("buy_cex_sell_dex", dex_execution_fee=0.0).profit_erg)


def test_cex_dex_paths_are_watch_only():
    for direction in ("buy_cex_sell_dex", "buy_dex_sell_cex"):
        opp = cex_dex(direction, cex_buy_price=0.20, cex_sell_price=0.50)   # absurdly profitable
        assert opp.blocked and opp.blocked_reason == WATCH_ONLY_REASON and "redeem value" in opp.assumption


def bank_cex(cex_price, **kw):
    args = dict(input_erg=100, bank_state=OPEN_BANK, cex_buy_price=cex_price, cex_trading_fee=0.001,
                erg_withdraw_fee=0.73, sigusd_usd=1.0)
    args.update(kw)
    return C.calc_bank_to_cex(**args)


def bank_rate():
    """USD per ERG the bank mint gives for 100 ERG (contract-exact, with sigusd valued at $1)."""
    cents = affordable_mint_cents(OPEN_BANK, int((100 - config.ERGO_TX_FEE) * 1e9))
    return cents / 100 / 100


def test_bank_to_cex_at_equal_prices_loses_the_fees():
    """Issue #2: the old code reported this as break-even-ish with the sign inverted."""
    opp = bank_cex(bank_rate())
    assert opp.profit_erg < 0 and opp.profit_erg == pytest.approx(-(100 * 0.001 + 0.73), abs=0.01)


def test_bank_to_cex_is_profitable_only_when_the_bank_pays_more_than_the_cex_asks():
    assert bank_cex(bank_rate() * 0.95).profit_erg > 0      # ERG cheaper on the CEX than the bank sells it
    assert bank_cex(bank_rate() * 1.05).profit_erg < 0


def test_bank_to_cex_is_watch_only_and_reports_a_blocked_mint():
    opp = bank_cex(bank_rate() * 0.5)
    assert opp.blocked and WATCH_ONLY_REASON in opp.blocked_reason
    blocked = bank_cex(0.1, bank_state=BankState(1_006_250 * 10**9, 10_000_000, 3_125_000_000))  # RR 322%
    assert "Bank mint blocked" in blocked.blocked_reason
