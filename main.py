import argparse
import asyncio
import sys

import config
from logging_config import setup_logging, console
from arbitrage.scanner import ArbitrageScanner


def main():
    parser = argparse.ArgumentParser(description="Ergo Arbitrage Monitor")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--notify", action="store_true",
        help="Notification mode: monitor + send Discord alerts (no trade execution)"
    )
    mode_group.add_argument(
        "--live", action="store_true",
        help="Live mode: monitor + Discord alerts + auto-execute profitable trades"
    )
    args = parser.parse_args()

    if args.live:
        mode = "live"
    elif args.notify:
        mode = "notify"
    else:
        mode = "monitor"

    logger = setup_logging("INFO")

    console.print("[bold magenta]" + "=" * 60 + "[/bold magenta]")
    console.print("[bold magenta]       ERGO ARBITRAGE MONITOR[/bold magenta]")
    console.print("[bold magenta]       Goal: Accumulate more ERG[/bold magenta]")
    console.print("[bold magenta]" + "=" * 60 + "[/bold magenta]")
    console.print()

    mode_labels = {
        "monitor": ("[dim]MONITOR ONLY[/dim]", "Console output only, no notifications or trades"),
        "notify": ("[bold green]NOTIFICATION MODE[/bold green]", "Console + Discord alerts, no trades"),
        "live": ("[bold red]LIVE TRADING MODE[/bold red]", "Console + Discord + auto-execute trades"),
    }
    label, desc = mode_labels[mode]
    console.print(f"  Mode: {label}")
    console.print(f"  {desc}")
    console.print()

    if mode in ("notify", "live"):
        if config.DISCORD_ENABLED:
            console.print("[bold green]  Discord: ENABLED[/bold green]")
            console.print(f"    Min profit to notify: {config.DISCORD_MIN_PROFIT_PERCENT}%")
            console.print(f"    Cooldown per path: {config.DISCORD_COOLDOWN_SECONDS}s")
            if config.DISCORD_USER_ID:
                console.print(f"    User ping: <@{config.DISCORD_USER_ID}>")
            else:
                console.print("    User ping: not configured (set DISCORD_USER_ID in .env)")
        else:
            console.print("[bold yellow]  Discord: NOT CONFIGURED[/bold yellow]")
            console.print("    Set DISCORD_WEBHOOK_URL in .env to enable notifications")
    console.print()

    if mode == "live":
        console.print("[bold red]  WARNING: Live trading will execute real transactions![/bold red]")
        console.print()

    scanner = ArbitrageScanner(mode=mode)

    try:
        asyncio.run(scanner.run())
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Interrupted. Goodbye![/bold yellow]")
        sys.exit(0)


if __name__ == "__main__":
    main()
