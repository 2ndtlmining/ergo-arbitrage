"""Sanity bounds: implausible opportunities are blocked and alerted, never traded (#66)."""
import asyncio
from types import SimpleNamespace

import pytest

import config
import ergo.arb_runner as runner
from notifications.health import HealthMonitor
from tests.test_live import KEY, WALLET


def run(coro):
    return asyncio.run(coro)


def quote(bid, ask):
    return SimpleNamespace(book=object(), bid=bid, ask=ask, error=None)


def with_cex(bot, *mids):
    bot._last_prices = dict(bot._last_prices or {}, cex={f"X{i}": quote(m * 0.999, m * 1.001)
                                                        for i, m in enumerate(mids)})


def oracle(bot):
    return bot._prices["bank"]["oracle_erg_usd"]


def test_an_implausible_profit_blocks_live_trading(live, monkeypatch):
    profit = live.last_sizing[KEY].profit_percent
    monkeypatch.setattr(config, "LIVE_MAX_PROFIT_PERCENT", profit / 2)
    blockers = run(live._live_blockers(WALLET, live._prices))
    assert any("implausible" in b and "LIVE_MAX_PROFIT_PERCENT" in b for b in blockers)
    run(live._execute_trades(WALLET, live._prices))
    assert live.calls == []


def test_a_plausible_profit_trades(live):
    run(live._execute_trades(WALLET, live._prices))
    assert len(live.calls) == 1


def test_oracle_far_from_the_exchanges_blocks_live_trading(live):
    o = oracle(live)
    with_cex(live, o * 1.10, o * 1.12, o * 1.11)
    blockers = run(live._live_blockers(WALLET, live._prices))
    assert any("oracle" in b and "LIVE_MAX_ORACLE_DEVIATION_PERCENT" in b for b in blockers)
    run(live._execute_trades(WALLET, live._prices))
    assert live.calls == []


def test_oracle_close_to_the_exchanges_is_fine(live):
    o = oracle(live)
    with_cex(live, o * 1.01, o * 0.99)
    assert not any("oracle" in b for b in run(live._live_blockers(WALLET, live._prices)))


def test_one_exchange_is_not_enough_to_judge_the_oracle(live):
    with_cex(live, oracle(live) * 2)
    assert not any("LIVE_MAX_ORACLE_DEVIATION_PERCENT" in b for b in run(live._live_blockers(WALLET, live._prices)))


def test_implausible_data_alerts_with_a_ping():
    m = HealthMonitor(chain_s=120, venue_s=300, oracle_s=600, repeat_s=1800, started_at=0)
    state = SimpleNamespace(chain_error=None, venues=[], live="blocked", live_detail=[], mode="live",
                            live_guards=["implausible profit +25.00% on pool→redeem > LIVE_MAX_PROFIT_PERCENT 10"])
    ev = m.update(0, state)
    assert [(e.subject, e.ping) for e in ev] == [("live:implausible", True)] and "+25.00%" in ev[0].text


@pytest.mark.parametrize("name,value", [("LIVE_MAX_PROFIT_PERCENT", 0.2), ("LIVE_MAX_PROFIT_PERCENT", 500.0),
                                        ("LIVE_MAX_ORACLE_DEVIATION_PERCENT", 0.0),
                                        ("LIVE_MAX_ORACLE_DEVIATION_PERCENT", 80.0)])
def test_bounds_are_validated(monkeypatch, name, value):
    monkeypatch.setattr(config, name, value)
    assert any(name in e for e in config.live_config_errors())


def test_manual_execute_refuses_an_implausible_profit(offline, monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_PROFIT_PERCENT", 0.5)
    r = run(runner.run_arb(None, "redeem", 3 * 10**9, execute=True, log=lambda m: None))
    assert r.status == "implausible" and "LIVE_MAX_PROFIT_PERCENT" in r.message


def test_manual_check_still_runs_on_an_implausible_profit(offline, monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_PROFIT_PERCENT", 0.5)
    r = run(runner.run_arb(None, "redeem", 3 * 10**9, check=True, log=lambda m: None))
    assert r.status == "dry_run"


def test_force_overrides_the_bound_for_a_manual_trade(offline, monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_PROFIT_PERCENT", 0.5)
    r = run(runner.run_arb(None, "redeem", 3 * 10**9, execute=True, force=True, log=lambda m: None))
    assert r.status == "dry_run"
