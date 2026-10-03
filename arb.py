"""One command for the wallet actions. Dry run by default; --check validates on your node
without broadcasting; --execute sends and follows the transaction until it confirms.

    python arb.py balance
    python arb.py quote  [--sell sigusd --amount 10]        (always shows the best arb sizes)
    python arb.py swap   --sell erg    --amount 5          [--check | --execute]
    python arb.py swap   --sell sigusd --amount all         --execute
    python arb.py redeem --sigusd all                       --execute
    python arb.py send   --to 9f... --erg 1.5 [--sigusd 2]  --execute
    python arb.py arb    [--erg best|10] [--path redeem|mint] [--check | --execute] [--force]
    python arb.py doctor [--no-sign]                        (is the node ready for the bot?)
    python arb.py config                                    (effective settings, mistakes in .env)
    python arb.py backup [--to backups] [--keep 14]         (consistent copy of the tracker database)
    python arb.py resume                                    (clear a live-mode pause, then restart the bot)

--execute (and main.py --live) refuse to run with dangerous settings, e.g. SLIPPAGE_TOLERANCE above 0.05.
"""
import argparse
import asyncio
import sys

import aiohttp

import config
import config_check
from tracker.backup import backup_database
from ergo import actions, doctor
from ergo.amounts import parse_amount


def parse_size(text: str):
    """Leg 1 size in ERG, or "best" (None): the runner picks it on the boxes it spends."""
    if str(text).strip().lower() == "best":
        return None
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("size must be positive (or 'best')")
    return value


def _mode_flags(p: argparse.ArgumentParser):
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="sign and validate on your node, do not broadcast")
    g.add_argument("--execute", action="store_true", help="send it, then follow it until confirmed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arb.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("balance", help="wallet balances, value, pending changes, bank/pool state")

    q = sub.add_parser("quote", help="best arbitrage sizes now, and pool vs bank for an amount (no transaction)")
    q.add_argument("--sell", choices=["erg", "sigusd"])
    q.add_argument("--amount", type=float)

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

    a = sub.add_parser("arb", help="two-leg arbitrage: pool buy -> bank redeem, or bank mint -> pool sell")
    a.add_argument("--erg", type=parse_size, default=None,
                   help="ERG for leg 1 (the mint budget for --path mint), or 'best' (default): the size "
                        "with the best profit on the current pool and bank, capped by wallet and MAX_TRADE_SIZE_ERG")
    a.add_argument("--path", choices=["redeem", "mint"], default="redeem",
                   help="redeem: pool buy -> bank redeem (default); mint: bank mint -> pool sell")
    a.add_argument("--force", action="store_true", help="execute even below MIN_PROFIT_PERCENT (testing)")
    _mode_flags(a)

    d = sub.add_parser("doctor", help="check the node is ready for the bot (ends with a sign check that is never broadcast)")
    d.add_argument("--no-sign", action="store_true", help="skip the final sign-and-validate check")

    sub.add_parser("config", help="effective settings and their source; unknown keys, placeholders, renamed "
                                  "settings in .env (secrets masked)")

    b = sub.add_parser("backup", help="consistent copy of the tracker database (safe while the bot runs)")
    b.add_argument("--db", default=str(config.repo_path("arbitrage_tracker.db")))
    b.add_argument("--to", default=str(config.repo_path("backups")), help="folder for the copies")
    b.add_argument("--keep", type=int, default=14, help="newest copies to keep (default 14)")

    rs = sub.add_parser("resume", help="clear the live-mode pause left by a failed or interrupted trade")
    rs.add_argument("--db", default=str(config.repo_path("arbitrage_tracker.db")))
    return parser


def mode_of(args) -> str:
    return "execute" if getattr(args, "execute", False) else ("check" if getattr(args, "check", False) else "dry")


async def run(args):
    log = print
    mode = mode_of(args)
    if args.command not in ("balance", "quote", "doctor"):
        log(f"=== {args.command} [{ {'dry': 'DRY RUN', 'check': 'CHECK (no broadcast)', 'execute': 'EXECUTE'}[mode] }] ===")
    headers = {"api_key": config.ERGO_NODE_API_KEY, "Content-Type": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as ns:
        if args.command == "balance":
            await actions.balance(ns, log)
        elif args.command == "quote":
            if (args.sell is None) != (args.amount is None):
                log("quote: give both --sell and --amount, or neither.")
                return
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
            await actions.arb(ns, args.erg, mode, args.force, log, path=args.path)
        elif args.command == "doctor":
            return await doctor.run_doctor(ns, sign=not args.no_sign, log=log)


def resume(db_path: str):
    """Clear the stored live-mode pause. The running bot keeps its pause until it is restarted."""
    from arbitrage.scanner import LIVE_PAUSE_KEY
    from tracker.profit_tracker import ProfitTracker
    tracker = ProfitTracker(db_path)
    try:
        reason = tracker.get_meta(LIVE_PAUSE_KEY)
        if reason is None:
            print("Live mode is not paused.", flush=True)
            return
        tracker.delete_meta(LIVE_PAUSE_KEY)
    finally:
        tracker.close()
    print(f"Cleared the live pause: {reason}\n"
          "Check `python arb.py balance` first (redeem leftover SigUSD with `python arb.py redeem --sigusd all "
          "--execute`), then restart the bot to trade again.", flush=True)


def main(argv=None):
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.command == "config":
        print("\n".join(config_check.report()), flush=True)
        return
    if args.command == "resume":
        resume(args.db)
        return
    if mode_of(args) == "execute" and (errors := config.live_config_errors()):
        print("Refusing to --execute with these settings (.env):", *[f"  {e}" for e in errors], sep="\n", flush=True)
        sys.exit(2)
    if args.command == "backup":
        try:
            print(f"Backed up to {backup_database(args.db, args.to, keep=args.keep)}", flush=True)
        except FileNotFoundError as e:
            print(e, flush=True)
            sys.exit(1)
        return
    try:
        code = asyncio.run(run(args))
    except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
        print(f"Cannot reach your Ergo node at {config.ERGO_NODE_URL} ({e.__class__.__name__}: {e}). "
              f"Is it running? Check ERGO_NODE_URL in .env.", flush=True)
        sys.exit(1)
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
