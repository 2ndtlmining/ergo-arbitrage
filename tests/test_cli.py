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

    def test_doctor(self):
        assert self.parse("doctor").no_sign is False and self.parse("doctor", "--no-sign").no_sign is True


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


def test_arb_path_option():
    p = build_parser()
    assert p.parse_args(["arb", "--erg", "10"]).path == "redeem"
    assert p.parse_args(["arb", "--erg", "10", "--path", "mint", "--check"]).path == "mint"
    with pytest.raises(SystemExit):
        p.parse_args(["arb", "--erg", "10", "--path", "sideways"])


def test_balance_lines_flag_stale_entries():
    lines = balance_lines(confirmed={"erg": 20.7119, "sigusd": 0}, unconfirmed={"erg": 20.7119, "sigusd": 0},
                          oracle_usd_per_erg=0.3274, pool_sigusd_per_erg=0.31, reserve_ratio=333.0,
                          stale={"erg": 4.8625, "sigusd": 0, "count": 1})
    text = "\n".join(lines)
    assert "Pending" not in text
    assert "Ignored" in text and "4.8625 ERG" in text and "no longer valid" in text


def test_arb_size_defaults_to_best():
    p = build_parser()
    assert p.parse_args(["arb"]).erg is None
    assert p.parse_args(["arb", "--erg", "best"]).erg is None
    assert p.parse_args(["arb", "--erg", "12.5"]).erg == 12.5
    with pytest.raises(SystemExit):
        p.parse_args(["arb", "--erg", "-3"])


def test_quote_without_amount_is_allowed():
    args = build_parser().parse_args(["quote"])
    assert args.sell is None and args.amount is None


def test_best_size_lines_cover_both_paths():
    from arbitrage.sizing import Market
    from ergo.actions import best_size_lines
    from tests.test_bank_redeem_tx import BANK_BOX, ORACLE_BOX
    from tests.test_chain_arb import pool_box
    text = "\n".join(best_size_lines(Market.from_boxes(pool_box(0.35, 2_000 * 10**9), BANK_BOX, ORACLE_BOX), 100))
    assert "pool buy -> bank redeem" in text and "break-even" in text
    assert "bank mint -> pool sell" in text and "400%" in text  # BANK_BOX RR ~322%: mint closed


def test_unreachable_node_is_one_line(monkeypatch, capsys):
    import aiohttp
    import arb

    async def boom(args):
        raise aiohttp.ClientConnectionError("semaphore timeout")

    monkeypatch.setattr(arb, "run", boom)
    with pytest.raises(SystemExit) as e:
        arb.main(["balance"])
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "Cannot reach your Ergo node" in out and "ERGO_NODE_URL" in out and "Traceback" not in out
