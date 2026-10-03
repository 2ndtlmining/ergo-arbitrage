"""#13: each opportunity declares the price sources it depends on; staleness follows them, not the
words in the path's name."""
import time

import pytest

import config
from arbitrage.scanner import ArbitrageScanner
from tests.test_scanner_paths import TestCexPaths as _CexPaths   # helper only (not re-collected)
from exchanges.sigmausd import BankState
from tests.test_scanner_paths import ORACLE_R4

KNOWN = {"spectrum", "bank", "kucoin", "nonkyc", "use"}


@pytest.fixture
def scanner(tmp_path):
    s = ArbitrageScanner(db_path=str(tmp_path / "s.db"), enable_cex=True)
    yield s
    s.tracker.close()


def test_every_opportunity_declares_its_sources(scanner):
    state = BankState(bank_erg_nano=3_000_000 * 10**9, sigusd_circ_cents=10_000_000, oracle_r4=ORACLE_R4)
    opps = scanner._find_opportunities(_CexPaths().prices(state))
    assert opps and all(o.sources and set(o.sources) <= KNOWN for o in opps)
    by = {o.path_key: set(o.sources) for o in opps}
    assert by["Spectrum buy->Bank redeem"] == {"spectrum", "bank"}
    assert by["Kucoin<>Bank"] == {"kucoin", "bank"} and by["NonKYC<>Spectrum"] == {"nonkyc", "spectrum"}


def test_staleness_follows_the_declared_sources_not_the_name(scanner):
    state = BankState(bank_erg_nano=3_000_000 * 10**9, sigusd_circ_cents=10_000_000, oracle_r4=ORACLE_R4)
    opp = next(o for o in scanner._find_opportunities(_CexPaths().prices(state))
               if o.path_key == "Spectrum buy->Bank redeem")
    opp.path = "Pool buy->SigmaUSD redeem [10 ERG]"            # a rename must not switch the check off
    now = time.time()
    scanner._price_timestamps.update(spectrum=now, bank=now - config.PRICE_STALE_SECONDS - 5)
    assert scanner._is_price_stale(opp)
    scanner._price_timestamps["bank"] = now
    assert not scanner._is_price_stale(opp)
