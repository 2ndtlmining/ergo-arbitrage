"""The venues the dashboard lists: one describe function per venue (at most MAX_VENUES).

Adding a venue = one function here + the scanner putting its result into `prices`.
"""
from dataclasses import dataclass
from typing import Callable, Optional

from arbitrage.dashboard_state import MAX_VENUES, VenueStatus


@dataclass
class VenueContext:
    prices: dict
    timestamps: dict          # scanner._price_timestamps: source -> time.time()
    now: float
    chain_error: Optional[str]
    pending: frozenset        # ChainSnapshot.pending
    read_ms: Optional[float]
    enable_cex: bool
    enable_use: bool
    cex_watch: bool


def _age(c: VenueContext, key: str) -> Optional[float]:
    t = c.timestamps.get(key)
    return c.now - t if t else None


def _chain_down(name: str, c: VenueContext) -> Optional[VenueStatus]:
    if c.chain_error:
        return VenueStatus(name, "on-chain", "down", error=c.chain_error)
    return None


def pool(c: VenueContext) -> VenueStatus:
    spot = c.prices.get("spectrum_erg_sigusd")
    down = _chain_down("ErgoDEX pool", c)
    if down or not spot:
        return down or VenueStatus("ErgoDEX pool", "on-chain", "down", error="no data")
    p = c.prices.get("spectrum_pool")
    reserve = f", {p.reserve_x:,.0f} ERG" if p else ""
    return VenueStatus("ErgoDEX pool", "on-chain", "pending" if "pool" in c.pending else "live",
                       f"{spot:.4f} SigUSD/ERG{reserve}", _age(c, "spectrum"), c.read_ms)


def bank(c: VenueContext) -> VenueStatus:
    b = c.prices.get("bank") or {}
    down = _chain_down("SigmaUSD bank", c)
    if down or b.get("reserve_ratio") is None:
        return down or VenueStatus("SigmaUSD bank", "on-chain", "down", error="no data")
    return VenueStatus("SigmaUSD bank", "on-chain", "pending" if "bank" in c.pending else "live",
                       f"RR {b['reserve_ratio']:.0f}%, mint {'✓' if b.get('can_mint_sigusd') else '✗'}",
                       _age(c, "bank"), c.read_ms)


def oracle(c: VenueContext) -> VenueStatus:
    b = c.prices.get("bank") or {}
    down = _chain_down("Oracle", c)
    if down or not b.get("oracle_erg_usd"):
        return down or VenueStatus("Oracle", "on-chain", "down", error="no data")
    return VenueStatus("Oracle", "on-chain", "pending" if "oracle" in c.pending else "live",
                       f"${b['oracle_erg_usd']:.4f}/ERG", _age(c, "bank"), c.read_ms)


def dexy_use(c: VenueContext) -> VenueStatus:
    if not c.enable_use:
        return VenueStatus("Dexy USE", "on-chain", "disabled", "ENABLE_USE=false")
    lp = c.prices.get("use_lp")
    if lp is None:
        return VenueStatus("Dexy USE", "on-chain", "down", error="no data")
    return VenueStatus("Dexy USE", "on-chain", "live", f"{lp.price_y_in_x:.4f} ERG/USE", _age(c, "use"))


def _cex(name: str, price_key: str, ts_key: str) -> Callable[[VenueContext], VenueStatus]:
    def describe(c: VenueContext) -> VenueStatus:
        if not (c.enable_cex or c.cex_watch):
            return VenueStatus(name, "CEX", "disabled", "ENABLE_CEX=false")
        if c.enable_cex:
            price = c.prices.get(price_key)
            if not price:
                return VenueStatus(name, "CEX", "down", error="no quote")
            return VenueStatus(name, "CEX", "live", f"${price:.4f}", _age(c, ts_key))
        full = (c.prices.get("cex") or {}).get(name)
        if full is not None and full.error and full.error.startswith("disabled"):
            return VenueStatus(name, "CEX", "disabled", full.error.split(": ", 1)[-1])
        q = (c.prices.get("cex_watch") or {}).get(name)
        if q is None:
            return VenueStatus(name, "CEX", "down", error=(full.error if full is not None and full.error
                                                            else "no quote"))
        ts = q.timestamp if isinstance(q.timestamp, (int, float)) else q.timestamp.timestamp()
        return VenueStatus(name, "CEX", "watch", f"${q.bid:.4f} / ${q.ask:.4f}", c.now - ts)
    return describe


VENUES: tuple = (pool, bank, oracle, dexy_use, _cex("Kucoin", "kucoin_erg_usdt", "kucoin"),
                 _cex("NonKYC", "nonkyc_erg_usdt", "nonkyc"), _cex("Gate", "gate_erg_usdt", "gate"),
                 _cex("MEXC", "mexc_erg_usdt", "mexc"), _cex("SafeTrade", "safetrade_erg_usdt", "safetrade"))


def describe_all(c: VenueContext) -> list[VenueStatus]:
    """One row per venue; a venue whose description fails shows as down instead of breaking the panel."""
    rows = []
    for describe in VENUES[:MAX_VENUES]:
        try:
            rows.append(describe(c))
        except Exception as e:
            name = getattr(describe, "__name__", "venue")
            rows.append(VenueStatus(name, "?", "down", error=f"{e.__class__.__name__}: {e}"))
    return rows
