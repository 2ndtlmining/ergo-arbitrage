"""Wallet analysis on the exact math (#13): the ERG options are the live gate's own best size at the
wallet-funded cap, the SigUSD options are contract-exact, CEX exits use the order books and fee book."""
import pytest

import config
from arbitrage.scanner import ArbitrageScanner
from arbitrage.sizing import Market, best_size
from exchanges import cex_public as cp
from notifications import embeds
from tests.test_cex_sources import QUOTES
from tests.test_optimizer import discount_prices

WALLET = {"erg": 20.71, "sigusd": 0.0, "use": 0.0, "ok": True}


@pytest.fixture
def scanner(tmp_path):
    s = ArbitrageScanner(db_path=str(tmp_path / "w.db"), cex_watch=True)
    yield s
    s.tracker.close()


def options(analysis, asset):
    return {o["name"]: o for o in analysis[asset]["options"]}


def test_erg_options_are_the_live_gates_best_size_at_the_wallet_cap(scanner):
    prices = discount_prices(pool_erg=2_000)
    analysis = scanner._build_wallet_analysis(WALLET, prices)
    market = Market.from_pool_state(prices["spectrum_pool"], prices["bank"]["state"])
    cap = WALLET["erg"] - config.LIVE_ERG_RESERVE
    expected = best_size("redeem", market, cap)
    redeem = options(analysis, "erg")["ErgoDEX buy -> Bank redeem"]
    assert expected.ok and redeem["profit_pct"] == pytest.approx(expected.profit_percent)
    assert f"{expected.size_erg:.2f} ERG" in redeem["profit_desc"] and not redeem["blocked"]
    assert redeem["steps"]                                   # the same steps the Paths panel shows


def test_a_blocked_mint_is_reported_as_blocked(scanner):
    from exchanges.sigmausd import BankState
    prices = discount_prices(pool_erg=2_000)
    low = BankState(1_006_250 * 10**9, 10_000_000, 3_125_000_000)          # RR 322%: mint not allowed
    prices["bank"] = dict(prices["bank"], state=low, reserve_ratio=low.reserve_ratio, can_mint_sigusd=False)
    mint = options(scanner._build_wallet_analysis(WALLET, prices), "erg")["Bank mint -> ErgoDEX sell"]
    assert mint["blocked"] and "blocked" in mint["blocked_reason"].lower()


def test_cex_exits_use_the_book_and_the_fee_book(scanner):
    scanner.cex_fees.set("Gate", cp.CexFees(taker=0.002, erg_withdraw=0.403, source="live", live_fields=("taker",)))
    prices = dict(discount_prices(pool_erg=2_000), cex=dict(QUOTES))
    gate = options(scanner._build_wallet_analysis(WALLET, prices), "erg")["Sell on Gate"]
    bid = QUOTES["Gate"].book.effective_sell_price(WALLET["erg"])
    assert gate["result"] == f"${WALLET['erg'] * bid * (1 - 0.002):.2f} USDT on Gate"
    assert gate["blocked_reason"] == "exit to USDT"
    assert "Sell on MEXC" not in options(scanner._build_wallet_analysis(WALLET, prices), "erg")  # no book


def test_sigusd_options_are_contract_exact(scanner):
    prices = discount_prices(pool_erg=2_000)
    wallet = dict(WALLET, sigusd=10.0)
    sig = options(scanner._build_wallet_analysis(wallet, prices), "sigusd")
    redeem_erg = scanner._bank_redeem_erg(prices["bank"]["state"], 10.0)[1] - config.SIGMAUSD_REDEEM_EXTRA_ERG
    pool_erg = scanner._dex_sigusd_to_erg(prices, 10.0) - config.pool_service_fee() - config.ERGO_TX_FEE
    assert sig["Bank redeem"]["result"] == f"{redeem_erg:.2f} ERG in wallet"
    assert sig["ErgoDEX pool swap"]["result"] == f"{pool_erg:.2f} ERG in wallet"


def test_the_discord_wallet_embed_still_reads_it(scanner):
    prices = dict(discount_prices(pool_erg=2_000), cex=dict(QUOTES))
    e = embeds.wallet_embed(WALLET, scanner._build_wallet_analysis(dict(WALLET, sigusd=10.0), prices))
    text = " ".join(f["value"] for f in e.get("fields", []))
    assert e["title"] == "Wallet" and "ErgoDEX buy -> Bank redeem" in text


def test_a_small_wallet_has_no_erg_arbitrage_options(scanner):
    analysis = scanner._build_wallet_analysis(dict(WALLET, erg=1.5), discount_prices(pool_erg=2_000))
    assert not [o for o in analysis["erg"]["options"] if "->" in o["name"]]
