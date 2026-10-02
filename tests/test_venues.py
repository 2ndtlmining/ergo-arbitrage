"""Venues registry: one row per monitored venue (spec: dashboard)."""
import time
from datetime import datetime, timedelta

from arbitrage.venues import VENUES, VenueContext, describe_all
from exchanges.base import PoolState, PriceQuote

POOL = PoolState(exchange="t", pool_id="p", token_x="ERG", token_y="SigUSD", reserve_x=20_114, reserve_y=6_290,
                 fee_num=995, fee_denom=1000)
PRICES = {"spectrum_erg_sigusd": 0.3127, "spectrum_pool": POOL,
          "bank": {"oracle_erg_usd": 0.3242, "reserve_ratio": 330.1, "can_mint_sigusd": False}}


def ctx(**kw):
    now = time.time()
    base = dict(prices=dict(PRICES), timestamps={"spectrum": now - 1, "bank": now - 1}, now=now, chain_error=None,
                pending=frozenset(), read_ms=6.0, enable_cex=False, enable_use=False, cex_watch=False)
    base.update(kw)
    return VenueContext(**base)


def by_name(rows):
    return {r.name: r for r in rows}


def test_on_chain_rows():
    rows = by_name(describe_all(ctx()))
    pool, bank, oracle = rows["ErgoDEX pool"], rows["SigmaUSD bank"], rows["Oracle"]
    assert pool.state == "live" and "0.3127 SigUSD/ERG" in pool.quote and "20,114 ERG" in pool.quote
    assert pool.age_s is not None and pool.latency_ms == 6.0
    assert bank.quote == "RR 330%, mint ✗" and oracle.quote == "$0.3242/ERG"


def test_pending_oracle_and_chain_outage():
    assert by_name(describe_all(ctx(pending=frozenset({"oracle"}))))["Oracle"].state == "pending"
    rows = by_name(describe_all(ctx(chain_error="node down")))
    assert all(rows[n].state == "down" and rows[n].error == "node down"
               for n in ("ErgoDEX pool", "SigmaUSD bank", "Oracle"))


def test_disabled_venues_stay_listed():
    rows = by_name(describe_all(ctx()))
    assert rows["Dexy USE"].state == "disabled" and rows["Dexy USE"].quote == "ENABLE_USE=false"
    assert rows["Kucoin"].state == "disabled" and rows["NonKYC"].state == "disabled"


def test_cex_watch_quote_and_missing_quote():
    q = PriceQuote(exchange="Kucoin", pair="ERG/USDT", bid=0.3301, ask=0.3305,
                   timestamp=datetime.now() - timedelta(seconds=4))
    rows = by_name(describe_all(ctx(cex_watch=True, prices=dict(PRICES, cex_watch={"Kucoin": q}))))
    assert rows["Kucoin"].state == "watch" and rows["Kucoin"].quote == "$0.3301 / $0.3305"
    assert 3 <= rows["Kucoin"].age_s <= 6
    assert rows["NonKYC"].state == "down" and rows["NonKYC"].error == "no quote"


def test_a_failing_venue_does_not_take_the_others_down(monkeypatch):
    import arbitrage.venues as venues

    def broken(c):
        raise ValueError("boom")

    monkeypatch.setattr(venues, "VENUES", (broken,) + venues.VENUES[1:])
    rows = describe_all(ctx())
    assert rows[0].state == "down" and "boom" in rows[0].error
    assert by_name(rows)["SigmaUSD bank"].state == "live"


def test_registry_fits_the_panel():
    assert len(VENUES) <= 7
