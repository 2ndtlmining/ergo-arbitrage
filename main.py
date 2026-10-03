import argparse
import asyncio
import sys

from rich.live import Live

import config
from arbitrage.dashboard_view import render_safe
from arbitrage.scanner import ArbitrageScanner
from logging_config import EventLogHandler, console, setup_logging


def positive(kind):
    def parse(text):
        value = kind(text)
        if value <= 0:
            raise argparse.ArgumentTypeError(f"must be greater than 0, got {text}")
        return value
    return parse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Ergo Arbitrage Monitor (live dashboard by default)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--notify", action="store_true", help="monitor + Discord alerts (no trades)")
    mode.add_argument("--live", action="store_true", help="monitor + Discord alerts + auto-execute trades")
    view = p.add_mutually_exclusive_group()
    view.add_argument("--plain", action="store_true", help="scrolling output instead of the dashboard")
    view.add_argument("--json", action="store_true", help="one JSON line per full scan on stdout")
    p.add_argument("--once", action="store_true", help="one full scan, then exit")
    p.add_argument("--interval", type=positive(int), help="seconds between full scans (SCAN_INTERVAL_SECONDS)")
    p.add_argument("--max-trade-erg", type=positive(float), help="cap on any executed trade (MAX_TRADE_SIZE_ERG)")
    p.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"],
                   help="log file level (arbitrage.log, rotated daily, 14 days kept)")
    p.add_argument("--db", default=str(config.repo_path("arbitrage_tracker.db")),
                   help="tracker database (default: arbitrage_tracker.db in the bot folder)")
    p.add_argument("--no-wallet", action="store_true", help="hide the wallet panel and wallet analysis")
    return p


def view_of(args) -> str:
    return "plain" if args.plain else ("json" if args.json else "dashboard")


def mode_of(args) -> str:
    return "live" if args.live else ("notify" if args.notify else "monitor")


def apply_overrides(args):
    if args.interval is not None:
        config.SCAN_INTERVAL_SECONDS = args.interval
    if args.max_trade_erg is not None:
        config.MAX_TRADE_SIZE_ERG = args.max_trade_erg


def print_banner(mode: str):
    labels = {"monitor": "[dim]MONITOR ONLY[/dim]", "notify": "[bold green]NOTIFICATION MODE[/bold green]",
              "live": "[bold red]LIVE TRADING MODE[/bold red]"}
    console.print("[bold magenta]ERGO ARBITRAGE MONITOR[/bold magenta]  " + labels[mode])
    if mode == "live":
        console.print("[bold red]WARNING: live trading executes real transactions.[/bold red]")


async def run(scanner: ArbitrageScanner, view: str, once: bool):
    if view != "dashboard":
        await scanner.run(once=once)
        return
    with Live(get_renderable=lambda: render_safe(scanner.state), console=console, refresh_per_second=2,
              screen=not once, redirect_stdout=False, redirect_stderr=False):
        await scanner.run(once=once)


def ensure_utf8(stream=None):
    """Piped output on Windows defaults to cp1252, which cannot encode the dashboard's symbols."""
    stream = stream or sys.stdout
    if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
        stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    ensure_utf8(sys.stdout)
    ensure_utf8(sys.stderr)
    if args.max_trade_erg is not None and args.max_trade_erg > config.MAX_TRADE_SIZE_ERG:
        parser.error(f"--max-trade-erg {args.max_trade_erg:g} is above MAX_TRADE_SIZE_ERG "
                     f"({config.MAX_TRADE_SIZE_ERG:g}); it can only lower the cap. Raise it in .env instead.")
    apply_overrides(args)
    if args.live and (errors := config.live_config_errors()):
        parser.error("refusing to start --live with these settings (.env):\n  " + "\n  ".join(errors))
    view, mode = view_of(args), mode_of(args)
    logger = setup_logging(args.log_level, console_handler=view == "plain")
    logger.info(f"Files: database {args.db}, log {config.repo_path('arbitrage.log')}, "
                f"kill switch {config.repo_path(config.LIVE_STOP_FILE)}")
    if view == "plain":
        print_banner(mode)
    scanner = ArbitrageScanner(mode=mode, db_path=args.db, view=view, show_wallet=not args.no_wallet)
    if view == "dashboard":
        logger.addHandler(EventLogHandler(scanner.state))
    try:
        asyncio.run(run(scanner, view, args.once))
    except KeyboardInterrupt:
        if view != "json":
            console.print("[bold yellow]Interrupted. Goodbye![/bold yellow]")
        sys.exit(0)


if __name__ == "__main__":
    main()
