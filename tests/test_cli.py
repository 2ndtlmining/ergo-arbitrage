"""arb.py command line and the pure parts of ergo/actions.py."""
import pytest

import config
from arb import build_parser, mode_of
from ergo.actions import balance_lines, payment_request, send_policy

SIGUSD = config.SIGUSD_TOKEN_ID
PAYEE = "0008cd03" + "cd" * 32


class TestParser:
    def parse(self, *argv):
        return build_parser().parse_args(list(argv))

    def test_swap_all_execute(self):
        a = self.parse("swap", "--sell", "sigusd", "--amount", "all", "--execute")
        assert a.command == "swap" and a.sell == "sigusd" and a.amount == "all" and mode_of(a) == "execute"

    def test_default_is_dry_run(self):
        assert mode_of(self.parse("redeem", "--sigusd", "2")) == "dry"

    def test_check_and_execute_are_exclusive(self):
        with pytest.raises(SystemExit):
            self.parse("swap", "--sell", "erg", "--amount", "1", "--check", "--execute")

    def test_send_needs_destination(self):
        with pytest.raises(SystemExit):
            self.parse("send", "--erg", "1")

    @pytest.mark.parametrize("argv", [("balance",), ("quote", "--sell", "erg", "--amount", "10"),
                                      ("arb", "--erg", "10", "--check")])
    def test_other_commands(self, argv):
        assert self.parse(*argv).command == argv[0]


class TestSend:
    def test_payment_request_erg_only(self):
        req = payment_request("9addr", erg_nano=1_500_000_000, cents=0)
        assert req["requests"] == [{"address": "9addr", "value": 1_500_000_000, "assets": []}]
        assert req["fee"] == 1_100_000

    def test_payment_request_sigusd_carries_min_box_value(self):
        req = payment_request("9addr", erg_nano=0, cents=250)
        r = req["requests"][0]
        assert r["value"] == config.ERG_MIN_BOX_NANO
        assert r["assets"] == [{"tokenId": SIGUSD, "amount": 250}]

    def test_send_policy_allows_only_that_payee_and_amount(self):
        p = send_policy(PAYEE, erg_nano=config.ERG_MIN_BOX_NANO, cents=250)
        assert p.payees == {PAYEE: {"ERG": config.ERG_MIN_BOX_NANO, SIGUSD: 250}}
        assert p.max_token_spent == {SIGUSD: 250}
        assert p.max_erg_spent == config.ERG_MIN_BOX_NANO + 1_100_000
        assert p.max_service_fee == 0


class TestBalance:
    def test_lines(self):
        lines = balance_lines(
            confirmed={"erg": 10.0, "sigusd": 2.5},
            unconfirmed={"erg": 9.0, "sigusd": 2.5},
            oracle_usd_per_erg=0.32, pool_sigusd_per_erg=0.31, reserve_ratio=330.0,
        )
        text = "\n".join(lines)
        assert "10.0000 ERG" in text and "2.50 SigUSD" in text
        assert "pending" in text.lower() and "-1.0000 ERG" in text  # unconfirmed change shown
        assert "SigUSD on the pool: $1.03" in text
        assert "RR 330%" in text and "mint blocked" in text


def test_balance_lines_flag_stale_entries():
    lines = balance_lines(confirmed={"erg": 20.7119, "sigusd": 0}, unconfirmed={"erg": 20.7119, "sigusd": 0},
                          oracle_usd_per_erg=0.3274, pool_sigusd_per_erg=0.31, reserve_ratio=333.0,
                          stale={"erg": 4.8625, "sigusd": 0, "count": 1})
    text = "\n".join(lines)
    assert "Pending" not in text
    assert "Ignored" in text and "4.8625 ERG" in text and "no longer valid" in text
