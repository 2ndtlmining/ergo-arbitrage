"""Discord embed builders: pure, within Discord's limits (spec: discord episodes)."""
from types import SimpleNamespace

from notifications import embeds


def ep(**kw):
    base = dict(label="pool→redeem", size_erg=42.67, profit_erg=1.43, profit_percent=3.36, break_even_erg=0.06,
                peak_erg=1.51, peak_percent=3.5, peak_size_erg=45.0, opened_at=1000.0, last_seen_at=1380.0,
                closed_at=None, close_reason=None, trade=None, steps=["Swap 42.67 ERG -> SigUSD", "Redeem"])
    base.update(kw)
    return SimpleNamespace(**base)


def values(e):
    return " ".join([e["title"]] + [f["name"] + " " + f["value"] for f in e.get("fields", [])] +
                    [e.get("description", ""), e.get("footer", {}).get("text", "")])


def test_cut_and_duration():
    assert embeds.cut("abcdef", 4) == "abc…" and embeds.cut("abc", 4) == "abc"
    assert embeds.duration(45) == "45s" and embeds.duration(380) == "6m 20s" and embeds.duration(7300) == "2h 1m"


def test_open_episode_embed():
    e = embeds.episode_embed(ep(), "open", height=1885700, data_age_s=1.2, oracle_usd=0.3238)
    text = values(e)
    assert e["color"] == embeds.GREEN and e["title"].startswith("OPEN · pool→redeem +3.36%")
    for needle in ("42.67 ERG", "+1.4300 ERG", "+3.36%", "$+0.46", "+3.50%", "0.06 ERG", "6m 20s",
                   "1. Swap 42.67 ERG -> SigUSD", "explorer.ergoplatform.com/en/token/", "h1885700", "data 1s"):
        assert needle in text, needle


def test_closed_episode_embed():
    e = embeds.episode_embed(ep(closed_at=1380.0, profit_percent=0.3, trade="executed +0.42 ERG"), "closed")
    assert e["color"] == embeds.GREY
    assert e["title"] == "Closed · pool→redeem after 6m 20s · peak +3.50% · last +0.30%"
    assert "executed +0.42 ERG" in values(e) and "Steps" not in values(e)


def test_closed_by_shutdown_says_so():
    e = embeds.episode_embed(ep(closed_at=1100.0, close_reason="bot stopped"), "closed")
    assert "bot stopped" in values(e)


def test_limits_are_respected():
    long = "x" * 5000
    e = embeds.episode_embed(ep(label=long, steps=[long] * 5, trade=long), "open")
    assert len(e["title"]) <= 256 and all(len(f["value"]) <= 1024 for f in e["fields"])
    assert len(e["fields"]) <= 25


def test_health_embed_colours():
    alert = embeds.health_embed(SimpleNamespace(kind="alert", text="Chain state unreadable", ping=True))
    quiet = embeds.health_embed(SimpleNamespace(kind="alert", text="Kucoin down", ping=False))
    ok = embeds.health_embed(SimpleNamespace(kind="recovered", text="Kucoin recovered after 6m", ping=False))
    assert (alert["color"], quiet["color"], ok["color"]) == (embeds.RED, embeds.YELLOW, embeds.GREEN)
    assert ok["title"].startswith("✅") and alert["title"].startswith("⚠️")


def test_wallet_embed_survives_missing_keys():
    wallet = {"erg": 20.7119, "sigusd": 0.0}
    analysis = {"erg": {"balance": 20.7, "options": [{"name": "Spectrum buy -> Bank redeem", "profit_pct": 2.9,
                                                      "blocked": False}]},
                "sigusd": {"balance": 0.0}}
    e = embeds.wallet_embed(wallet, analysis)
    text = values(e)
    assert "20.7119 ERG" in text and "Spectrum buy -> Bank redeem" in text and "+2.9%" in text
    assert embeds.wallet_embed({}, {})["title"] == "Wallet"


def test_digest_embed_with_nothing_happening():
    d = SimpleNamespace(hours=24, paths={}, potential_erg=0.0, trades={"count": 0, "net_erg": 0.0, "failed": 0},
                        outages=[], outage_since="since 08:00", wallet={"erg": 20.7, "sigusd": 0.0})
    text = values(embeds.digest_embed(d))
    assert "No opportunities" in text and "No trades" in text and "No outages" in text and "20.7000 ERG" in text


def test_digest_embed_with_data():
    d = SimpleNamespace(hours=24, paths={"pool→redeem": {"count": 3, "longest_s": 400, "best_peak_percent": 3.5}},
                        potential_erg=2.75, trades={"count": 1, "net_erg": 0.42, "failed": 0},
                        outages=[("chain", 250.0, False)], outage_since="since 08:00", wallet=None)
    text = values(embeds.digest_embed(d))
    for needle in ("pool→redeem", "3 episodes", "6m 40s", "+3.50%", "+2.7500 ERG", "1 trade", "+0.4200 ERG",
                   "chain", "4m 10s"):
        assert needle in text, needle


def test_startup_and_shutdown():
    assert "LIVE" in embeds.startup_embed("live")["title"]
    s = embeds.shutdown_embed({"session_duration": "1:02:03", "opportunities_seen": 4})
    assert "1:02:03" in values(s)


def test_wallet_embed_survives_none_values():
    analysis = {"erg": {"balance": None, "options": [{"name": "x", "profit_pct": None}]}}
    e = embeds.wallet_embed({"erg": None, "sigusd": None, "use": None}, analysis)
    assert e["title"] == "Wallet" and "0.0000 ERG" in values(e)


def test_stale_episode_embed_after_a_restart():
    row = {"path": "pool→redeem", "peak_profit_percent": 3.5, "peak_profit_erg": 1.5,
           "opened_at": "2026-10-03T08:00:00", "closed_at": "2026-10-03T08:06:40"}
    e = embeds.stale_episode_embed(row)
    assert e["color"] == embeds.GREY and e["title"].startswith("Closed · pool→redeem")
    assert "bot restarted" in values(e) and "+3.50%" in values(e) and "6m 40s" in values(e)


from notifications.mint_gate import MintGateEvent, MintOpening


def test_mint_gate_embeds():
    opened = embeds.mint_gate_embed(MintGateEvent("opened", 403.2, 85.4, 0.31, 0.3125, MintOpening(0.0), None, True))
    assert opened["color"] == embeds.GREEN and opened["title"] == "Bank mint OPEN · RR 403%"
    assert "~85 ERG" in values(opened) and "--live" in values(opened)
    closed = embeds.mint_gate_embed(MintGateEvent("closed", 398.0, 0.0, 0.3141, 0.3125, MintOpening(0.0), 750.0,
                                                  False))
    assert closed["color"] == embeds.GREY and closed["title"] == "Bank mint closed · open for 12m 30s"
    assert "$0.314" in values(closed) and "+0.5%" in values(closed)


def test_digest_embed_shows_the_mint_line_only_when_known():
    d = SimpleNamespace(hours=24, paths={}, potential_erg=0.0, trades={"count": 0, "net_erg": 0.0, "failed": 0},
                        outages=[], outage_since="", wallet=None, mint="✗ needs ERG $0.398 (+24.2%)")
    assert "Bank mint" in values(embeds.digest_embed(d)) and "$0.398" in values(embeds.digest_embed(d))
    d.mint = None
    assert "Bank mint" not in values(embeds.digest_embed(d))
