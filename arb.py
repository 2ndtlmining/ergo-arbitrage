"""One command for the wallet actions. Dry run by default; --check validates on your node
without broadcasting; --execute sends and follows the transaction until it confirms.

    python arb.py balance
    python arb.py quote  --sell sigusd --amount 10
    python arb.py swap   --sell erg    --amount 5          [--check | --execute]
    python arb.py swap   --sell sigusd --amount all         --execute
    python arb.py redeem --sigusd all                       --execute
    python arb.py send   --to 9f... --erg 1.5 [--sigusd 2]  --execute
    python arb.py arb    --erg 10                           [--check | --execute] [--force]
"""
import argparse
import asyncio
import io
import sys

import aiohttp
from dotenv import load_dotenv
load_dotenv()

import config
from ergo import actions
from ergo.amounts import parse_amount


def _mode_flags(p: argparse.ArgumentParser):
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="sign and validate on your node, do not broadcast")
    g.add_argument("--execute", action="store_true", help="send it, then follow it until confirmed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arb.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("balance", help="wallet balances, value, pending changes, bank/pool state")

    q = sub.add_parser("quote", help="compare the pool and the bank for an amount (no transaction)")
    q.add_argument("--sell", choices=["erg", "sigusd"], required=True)
    q.add_argument("--amount", type=float, required=True)

    s = sub.add_parser("swap", help="swap ERG <-> SigUSD directly against the pool (no service fee)")
    s.add_argument("--sell", choices=["erg", "sigusd"], required=True)
    s.add_argument("--amount", type=parse_amount, required=True, help="number or 'all'")
    _mode_flags(s)

    r = sub.add_parser("redeem", help="redeem SigUSD for ERG at the SigmaUSD bank")
    r.add_argument("--sigusd", type=parse_amount, required=True, help="number or 'all'")
    _mode_flags(r)

    se = sub.add_parser("send", help="send ERG and/or SigUSD to another address")
    se.add_argument("--to", required=True, help="destination Ergo address")
    se.add_argument("--erg", type=float, default=0.0)
    se.add_argument("--sigusd", type=float, default=0.0)
    _mode_flags(se)

    a = sub.add_parser("arb", help="two-leg arbitrage: pool buy -> bank redeem")
    a.add_argument("--erg", type=float, required=True)
    a.add_argument("--force", action="store_true", help="execute even below MIN_PROFIT_PERCENT (testing)")
    _mode_flags(a)
    return parser


def mode_of(args) -> str:
    return "execute" if getattr(args, "execute", False) else ("check" if getattr(args, "check", False) else "dry")


async def run(args):
    log = print
    mode = mode_of(args)
    if args.command not in ("balance", "quote"):
        log(f"=== {args.command} [{ {'dry': 'DRY RUN', 'check': 'CHECK (no broadcast)', 'execute': 'EXECUTE'}[mode] }] ===")
    headers = {"api_key": config.ERGO_NODE_API_KEY, "Content-Type": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as ns:
        if args.command == "balance":
            await actions.balance(ns, log)
        elif args.command == "quote":
            await actions.quote(ns, args.sell, args.amount, log)
        elif args.command == "swap":
            await actions.swap(ns, args.sell, args.amount, mode, log)
        elif args.command == "redeem":
            await actions.redeem(ns, args.sigusd, mode, log)
        elif args.command == "send":
            if not args.erg and not args.sigusd:
                log("Nothing to send: give --erg and/or --sigusd.")
                return
            await actions.send(ns, args.to, args.erg, args.sigusd, mode, log)
        elif args.command == "arb":
            await actions.arb(ns, args.erg, mode, args.force, log)


def main(argv=None):
    args = build_parser().parse_args(argv)
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
