"""Crux Finance /dex/quote response parsing."""
from dataclasses import dataclass


@dataclass
class CruxQuote:
    output: int            # raw amount the AMM route delivers (what /dex/swap builds)
    price_impact: float    # percent
    lp_fee_percent: float
    pool_id: str
    pool_type: str
    source: str            # Crux's headline source: "amm" or "limit_order"


def parse_quote(quote: dict) -> CruxQuote:
    """Normalise a /dex/quote response.

    Crux's headline (`requested_amount`, `price_impact`) may come from its own
    limit orders, with null impact/fee fields, while /dex/swap builds the swap
    through the AMM pool. Use the AMM route numbers when present, so the
    displayed output and the TX guard's minimum match the built transaction.
    """
    amm = (quote.get("details") or {}).get("amm") or {}
    output = amm.get("output_amount")
    if output is None:
        output = quote.get("requested_amount") or 0
    impact = amm.get("price_impact")
    if impact is None:
        impact = quote.get("price_impact")
    fee = amm.get("lp_fee_percent")
    if fee is None:
        fee = quote.get("lp_fee_percent")
    return CruxQuote(
        output=int(output),
        price_impact=float(impact or 0),
        lp_fee_percent=float(fee or 0),
        pool_id=amm.get("pool_id") or "?",
        pool_type=amm.get("pool_type") or "?",
        source=quote.get("source") or "amm",
    )
