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
    python arb.py status                                    (is the bot running and is everything OK? no node needed)

--execute (and main.py --live) refuse to run with dangerous settings, e.g. SLIPPAGE_TOLERANCE above 0.05.
"""
import argparse
import asyncio
import sys
from pathlib import Path

import aiohttp

import config
import bot_status
import config_check
from instance_lock import InstanceLock, LockHeld
from tracker.backup import backup_database

DEFAULT_DB = str(config.repo_path("arbitrage_tracker.db"))
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
    g.add_argument("--execute", action="store_true", help="send it, then follow it until confirmed (without "
                                                           "--check or --execute: dry run)")
    p.add_argument("--ignore-lock", action="store_true",
                   help="--execute even while a --live bot runs on this database (it may spend the same boxes)")


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keeps the examples' line breaks and adds "(default: X)" to options that have a real default."""

    def _get_help_string(self, action):
        text = action.help or ""
        if "%(default)" not in text and action.default not in (None, False, argparse.SUPPRESS) \
                and action.option_strings:
            text += " (default: %(default)s)"
        return text


DRY = "Dry run by default (shows what it would do); --check signs and validates it on your node without " \
      "broadcasting; --execute sends it."


def _sub(sub, name: str, help: str, description: str, examples: list[str]):
    return sub.add_parser(name, help=help, description=f"{description}\n\n{DRY}" if "--check" in "".join(examples)
                          else description, epilog="examples:\n" + "\n".join(f"  python arb.py {e}" for e in examples),
                          formatter_class=HelpFormatter)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arb.py", description=__doc__, formatter_class=HelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    _sub(sub, "balance", "wallet balances, value, pending changes, bank/pool state",
         "Wallet balances (confirmed and pending), their value at the oracle price, and the pool and bank state.",
         ["balance"])

    q = _sub(sub, "quote", "best arbitrage sizes now, and pool vs bank for an amount (no transaction)",
             "The best arbitrage sizes on the current pool and bank, and (with --sell and --amount) what the pool "
             "and the bank give for that amount. Never builds a transaction.",
             ["quote", "quote --sell sigusd --amount 10", "quote --sell erg --amount 5"])
    q.add_argument("--sell", choices=["erg", "sigusd"], help="what you would sell (give --amount too)")
    q.add_argument("--amount", type=float, help="how much of it")

    s = _sub(sub, "swap", "swap ERG <-> SigUSD directly against the pool (no service fee)",
             "Swap against the ErgoDEX SigUSD/ERG pool with a transaction the bot builds itself: 0.5% pool fee plus "
             "the miner fee, no service fee.",
             ["swap --sell erg --amount 5", "swap --sell erg --amount 5 --check", "swap --sell sigusd --amount all --execute"])
    s.add_argument("--sell", choices=["erg", "sigusd"], required=True, help="what to sell")
    s.add_argument("--amount", type=parse_amount, required=True, help="number or 'all'")
    _mode_flags(s)

    r = _sub(sub, "redeem", "redeem SigUSD for ERG at the SigmaUSD bank",
             "Redeem SigUSD for ERG at the SigmaUSD bank at the oracle price (2.229% fee). Always allowed, whatever "
             "the reserve ratio; it is also how a failed live trade is finished.",
             ["redeem --sigusd 10 --check", "redeem --sigusd all --execute"])
    r.add_argument("--sigusd", type=parse_amount, required=True, help="number or 'all'")
    _mode_flags(r)

    se = _sub(sub, "send", "send ERG and/or SigUSD to another address",
              "Send ERG and/or SigUSD from the node wallet. The TX guard only lets that payee receive that amount; "
              "SEND_ALLOWED_ADDRESSES and SEND_MAX_SIGUSD (.env) limit it further, and --execute asks for the last "
              "4 characters of the address.",
              ["send --to 9f... --erg 1.5", "send --to 9f... --erg 1.5 --sigusd 2 --check", "send --to 9f... --erg 1.5 --execute"])
    se.add_argument("--to", required=True, help="destination Ergo address")
    se.add_argument("--erg", type=float, default=0.0, help="ERG to send")
    se.add_argument("--sigusd", type=float, default=0.0, help="SigUSD to send")
    _mode_flags(se)

    a = _sub(sub, "arb", "two-leg arbitrage: pool buy -> bank redeem, or bank mint -> pool sell",
             "One two-leg arbitrage on the current pool, bank and oracle boxes, sized for the best profit unless "
             "--erg is given. Refuses --execute while the STOP file exists or live mode is paused.",
             ["arb", "arb --check", "arb --erg 10 --path mint --check", "arb --execute"])
    a.add_argument("--erg", type=parse_size, default=None,
                   help="ERG for leg 1 (the mint budget for --path mint), or 'best' (default): the size "
                        "with the best profit on the current pool and bank, capped by wallet and MAX_TRADE_SIZE_ERG")
    a.add_argument("--path", choices=["redeem", "mint"], default="redeem",
                   help="redeem: pool buy -> bank redeem; mint: bank mint -> pool sell")
    a.add_argument("--force", action="store_true",
                   help="go ahead below MIN_PROFIT_PERCENT (asks you to type the expected result) or above "
                        "LIVE_MAX_PROFIT_PERCENT, and while an oracle update is pending (testing)")
    _mode_flags(a)

    d = _sub(sub, "doctor", "check the node is ready for the bot (ends with a sign check that is never broadcast)",
             "Checks .env, the Discord webhook (without posting) and everything the bot needs from your node: "
             "reachable, synced, API key, wallet, balances, extra index, mempool lookup, and finally a 1 ERG trade "
             "the node signs and validates but never broadcasts. Exits 1 when a node check fails.",
             ["doctor", "doctor --no-sign"])
    d.add_argument("--no-sign", action="store_true", help="skip the final sign-and-validate check")

    _sub(sub, "config", "effective settings and their source; unknown keys, placeholders, renamed settings in .env",
         "Every setting with the value in use and where it comes from (.env, environment, default), secrets masked; "
         "then keys in .env the bot does not read, leftover .env.example placeholders and renamed settings. "
         "Needs no node.", ["config"])

    b = _sub(sub, "backup", "consistent copy of the tracker database (safe while the bot runs)",
             "A consistent copy of the tracker database, taken with SQLite's backup API (a plain cp can miss rows "
             "while the bot runs); keeps the newest --keep copies.",
             ["backup", "backup --to /mnt/nas/arb --keep 30"])
    b.add_argument("--db", default=DEFAULT_DB, help="the database to copy")
    b.add_argument("--to", default=str(config.repo_path("backups")), help="folder for the copies")
    b.add_argument("--keep", type=int, default=14, help="newest copies to keep")

    st = _sub(sub, "status", "is the bot running and is everything OK? (reads the database; no node needed)",
              "Whether the bot is running (and in which mode), its last full scan and prices, open opportunities, "
              "today's trades, a live pause or STOP file, and the next Discord digest. Reads the bot's database "
              "and lock file only; `arb.py doctor` checks the node.", ["status"])
    st.add_argument("--db", default=DEFAULT_DB, help="the bot's database")

    rs = _sub(sub, "resume", "clear the live-mode pause left by a failed or interrupted trade",
              "Clears the stored live-mode pause. Check `arb.py balance` first; the running bot keeps its pause "
              "until it is restarted.", ["resume"])
    rs.add_argument("--db", default=DEFAULT_DB, help="the bot's database")
    return parser


def mode_of(args) -> str:
    return "execute" if getattr(args, "execute", False) else ("check" if getattr(args, "check", False) else "dry")


async def run(args):
    log = print
    mode = mode_of(args)
    if args.command not in ("balance", "quote", "doctor"):
        log(f"=== {args.command} [{ {'dry': 'DRY RUN', 'check': 'CHECK (no broadcast)', 'execute': 'EXECUTE'}[mode] }] ===")
    if warning := config.node_url_warning(config.ERGO_NODE_URL):
        log(f"WARNING: {warning}.")
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
            await actions.send(ns, args.to, args.erg, args.sigusd, mode, log, confirm=confirm_address)
        elif args.command == "arb":
            await actions.arb(ns, args.erg, mode, args.force, log, path=args.path, confirm=confirm_loss)
        elif args.command == "doctor":
            return await doctor.run_doctor(ns, sign=not args.no_sign, log=log)


def confirm_address(address: str) -> bool:
    """--execute send: the last 4 characters of the destination must be typed back (catches a pasted wrong one)."""
    print(f"Sending to {address}", flush=True)
    try:
        typed = input("Type the last 4 characters of the address to send (anything else cancels): ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    return typed == address[-4:]


def confirm_loss(message: str, expected_erg: float) -> bool:
    """--force --execute below MIN_PROFIT_PERCENT: the expected result must be typed back exactly."""
    want = f"{expected_erg:.4f}"
    print(f"--force: this trade is below MIN_PROFIT_PERCENT, {message}.", flush=True)
    try:
        typed = input(f"Type {want} to execute it anyway (anything else cancels): ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    return typed == want


def execute_refusals(args) -> list[str]:
    """Reasons a manual arbitrage must not run now: the kill switch and a live-mode pause. Single-leg
    commands (swap, redeem, send) stay allowed: they are how a failed trade is finished by hand."""
    if args.command != "arb":
        return []
    reasons = []
    stop = config.repo_path(config.LIVE_STOP_FILE)
    if stop.exists():
        reasons.append(f"STOP file present ({stop}); delete it to trade")
    if Path(DEFAULT_DB).exists():
        from arbitrage.scanner import LIVE_PAUSE_KEY
        from tracker.profit_tracker import ProfitTracker
        tracker = ProfitTracker(DEFAULT_DB)
        try:
            paused = tracker.get_meta(LIVE_PAUSE_KEY)
        finally:
            tracker.close()
        if paused:
            reasons.append(f"live mode is paused after: {paused}. Finish the recovery (python arb.py balance), "
                           f"then python arb.py resume")
    return reasons


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
    if args.command == "status":
        print("\n".join(bot_status.status_lines(args.db)), flush=True)
        return
    if args.command == "resume":
        resume(args.db)
        return
    if mode_of(args) == "execute" and (errors := config.live_config_errors()):
        print("Refusing to --execute with these settings (.env):", *[f"  {e}" for e in errors], sep="\n", flush=True)
        sys.exit(2)
    lock = None
    if mode_of(args) == "execute":
        if reasons := execute_refusals(args):
            print("Refusing to --execute:", *[f"  {r}" for r in reasons], sep="\n", flush=True)
            sys.exit(2)
        try:
            lock = InstanceLock(DEFAULT_DB, f"arb.py {args.command}").acquire()
        except LockHeld as e:
            if e.holder.get("mode") == "live" and not args.ignore_lock:
                print(f"Refusing to --execute: {e}. A live bot may spend the same boxes; stop it, create the STOP "
                      f"file, or pass --ignore-lock if you are sure.", flush=True)
                sys.exit(2)
    try:
        _dispatch(args)
    finally:
        if lock:
            lock.release()


def _dispatch(args):
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
