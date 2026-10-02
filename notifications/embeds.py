"""Discord embed payloads. Pure functions: data in, dict out, always within Discord's limits."""
from datetime import datetime, timezone
from typing import Optional

import config

GREEN, GREY, RED, YELLOW, BLUE = 0x2ECC71, 0x95A5A6, 0xE74C3C, 0xF1C40F, 0x3498DB
TITLE_MAX, FIELD_MAX, DESC_MAX, FOOTER_MAX, FIELDS_MAX = 256, 1024, 4096, 2048, 25
EXPLORER_TOKEN = "https://explorer.ergoplatform.com/en/token/{}"


def cut(text, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def duration(seconds: float) -> str:
    s = max(int(seconds), 0)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s" if m else f"{s}s"


def field(name: str, value, inline: bool = True) -> dict:
    return {"name": cut(name, TITLE_MAX), "value": cut(value if value not in (None, "") else "—", FIELD_MAX),
            "inline": inline}


def embed(title: str, color: int, fields=(), description: Optional[str] = None,
          footer: Optional[str] = None) -> dict:
    e = {"title": cut(title, TITLE_MAX), "color": color, "timestamp": datetime.now(timezone.utc).isoformat()}
    if description:
        e["description"] = cut(description, DESC_MAX)
    if fields:
        e["fields"] = list(fields)[:FIELDS_MAX]
    if footer:
        e["footer"] = {"text": cut(footer, FOOTER_MAX)}
    return e


def _boxes_field() -> dict:
    pool = EXPLORER_TOKEN.format(config.SPECTRUM_SIGUSD_POOL_NFT)
    bank = EXPLORER_TOKEN.format(config.SIGMAUSD_BANK_NFT)
    return field("Boxes", f"[pool]({pool}) · [bank]({bank})", inline=False)


def episode_embed(ep, status: str, height: int = 0, data_age_s: Optional[float] = None,
                  oracle_usd: Optional[float] = None) -> dict:
    is_open = status == "open"
    end = ep.closed_at if ep.closed_at is not None else ep.last_seen_at
    usd = f" / ${ep.profit_erg * oracle_usd:+.2f}" if oracle_usd else ""
    fields = [
        field("Size", f"{ep.size_erg:.2f} ERG"),
        field("Profit", f"{ep.profit_erg:+.4f} ERG ({ep.profit_percent:+.2f}%){usd}"),
        field("Peak", f"{ep.peak_percent:+.2f}% ({ep.peak_erg:+.4f} ERG at {ep.peak_size_erg:.2f})"),
        field("Break-even", f"{ep.break_even_erg:.2f} ERG" if ep.break_even_erg else None),
        field("Duration", duration(end - ep.opened_at)),
    ]
    if ep.trade:
        fields.append(field("Live trade", ep.trade, inline=False))
    if is_open and ep.steps:
        fields.append(field("Steps", "\n".join(f"{i}. {s}" for i, s in enumerate(ep.steps, 1)), inline=False))
    fields.append(_boxes_field())
    if is_open:
        title = f"OPEN · {ep.label} {ep.profit_percent:+.2f}%"
    else:
        title = (f"Closed · {ep.label} after {duration(end - ep.opened_at)} · "
                 f"peak {ep.peak_percent:+.2f}% · last {ep.profit_percent:+.2f}%")
    footer = f"h{height}" + (f" · data {data_age_s:.0f}s" if data_age_s is not None else "")
    description = f"Closed: {ep.close_reason}" if not is_open and ep.close_reason else None
    return embed(title, GREEN if is_open else GREY, fields, description=description, footer=footer)


def stale_episode_embed(row: dict) -> dict:
    """Grey 'closed' version of a message left open by a bot that stopped without closing it."""
    try:
        lasted = duration((datetime.fromisoformat(row["closed_at"])
                           - datetime.fromisoformat(row["opened_at"])).total_seconds())
    except (KeyError, TypeError, ValueError):
        lasted = "?"
    fields = [field("Peak", f"{row.get('peak_profit_percent') or 0:+.2f}% ({row.get('peak_profit_erg') or 0:+.4f} ERG)"),
              field("Seen for", lasted)]
    return embed(f"Closed · {row.get('path', '?')} · bot restarted", GREY, fields,
                 description="Closed: bot restarted (the last update before it stopped is shown)")


def health_embed(event) -> dict:
    if event.kind == "recovered":
        return embed(f"✅ {event.text}", GREEN)
    return embed(f"⚠️ {event.text}", RED if event.ping else YELLOW)


def mint_gate_embed(event) -> dict:
    """Bank mint gate: green while open, edited to grey when it closes."""
    if event.kind == "opened":
        fields = [field("Room", f"~{event.room_erg:,.0f} ERG mintable"),
                  field("Oracle", f"${event.oracle_price:.4f}/ERG")]
        return embed(f"Bank mint OPEN · RR {event.reserve_ratio:.0f}%", GREEN, fields,
                     description="Bank mint -> pool sell is possible now; --live trades it when profitable.")
    if event.open_price and event.oracle_price and event.open_price > event.oracle_price:
        reopen = field("Reopens at", f"ERG ${event.open_price:.3f} "
                                     f"({(event.open_price / event.oracle_price - 1) * 100:+.1f}%)")
    elif event.open_price and event.oracle_price:   # RR >= 400% already; the room is what is missing
        reopen = field("Room", f"room only {event.room_erg:.2f} ERG mintable")
    else:
        reopen = field("Reopens at", None)
    fields = [field("RR", f"{event.reserve_ratio:.0f}%"), reopen]
    return embed(f"Bank mint closed · open for {duration(event.open_for_s or 0)}", GREY, fields)


def mint_gate_stopped_embed() -> dict:
    """An open mint message whose bot stopped: nobody is watching the gate any more."""
    return embed("Bank mint · bot stopped", GREY,
                 description="The bot stopped while minting was open, so the current state is unknown.")


def wallet_embed(wallet: dict, analysis: dict) -> dict:
    wallet, analysis = wallet or {}, analysis or {}
    description = (f"{wallet.get('erg') or 0:.4f} ERG · {wallet.get('sigusd') or 0:.2f} SigUSD · "
                   f"{wallet.get('use') or 0:.3f} USE")
    fields = []
    for key, label in (("erg", "ERG"), ("sigusd", "SigUSD"), ("use", "USE")):
        info = analysis.get(key) or {}
        options = [o for o in (info.get("options") or []) if not o.get("blocked")]
        if not options:
            continue
        lines = []
        for o in sorted(options, key=lambda o: o.get("profit_pct") or 0, reverse=True)[:3]:
            pct = o.get("profit_pct") or 0
            lines.append(f"{'>>' if pct > 0.5 else '--'} {o.get('name', '?')} ({pct:+.1f}%)")
        fields.append(field(f"{label} ({info.get('balance') or 0:g})", "\n".join(lines), inline=False))
    return embed("Wallet", BLUE, fields, description=description)


def digest_embed(d) -> dict:
    if d.paths:
        lines = [f"{label}: {p['count']} episode{'s' if p['count'] != 1 else ''}, longest "
                 f"{duration(p['longest_s'])}, best peak {p['best_peak_percent']:+.2f}%"
                 for label, p in d.paths.items()]
        opp = "\n".join(lines) + f"\nPotential: {d.potential_erg:+.4f} ERG"
    else:
        opp = "No opportunities"
    t = d.trades
    trades = (f"{t['count']} trade{'s' if t['count'] != 1 else ''}, net {t['net_erg']:+.4f} ERG, "
              f"{t['failed']} failed") if t["count"] else "No trades"
    outages = "\n".join(f"{subject}: {duration(seconds)}{' (ongoing)' if ongoing else ''}"
                        for subject, seconds, ongoing in d.outages) or "No outages"
    fields = [field("Opportunities", opp, inline=False), field("Live trades", trades, inline=False),
              field(f"Outages ({d.outage_since})", outages, inline=False)]
    if d.wallet:
        fields.append(field("Wallet", f"{d.wallet.get('erg', 0):.4f} ERG · {d.wallet.get('sigusd', 0):.2f} SigUSD"))
    if getattr(d, "mint", None):
        fields.append(field("Bank mint", d.mint))
    return embed(f"Daily digest · last {d.hours}h", BLUE, fields)


def startup_embed(mode: str) -> dict:
    fields = [
        field("Alerts", f">= {config.DISCORD_MIN_PROFIT_PERCENT}% and >= {config.DISCORD_MIN_PROFIT_ERG} ERG, "
                        f"held {config.DISCORD_CONFIRM_SECONDS:g}s"),
        field("Ping", f">= {config.DISCORD_TIER1_PROFIT_PERCENT}%, chain down, live paused"),
        field("Max trade", f"{config.MAX_TRADE_SIZE_ERG:g} ERG"),
        field("Digest", f"{config.DISCORD_DIGEST_HOUR}:00" if config.DISCORD_DIGEST_HOUR >= 0 else "off"),
    ]
    return embed(f"Ergo arbitrage started · {mode.upper()}", RED if mode == "live" else BLUE, fields)


def shutdown_embed(stats: dict) -> dict:
    stats = stats or {}
    fields = [field("Ran for", stats.get("session_duration", "—")),
              field("Opportunities", stats.get("opportunities_seen", 0))]
    return embed("Ergo arbitrage stopped", GREY, fields)
