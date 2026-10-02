"""Crux /dex/quote parsing: the swap builder routes through the AMM, so use the AMM numbers."""
from exchanges.crux import parse_quote

# Live shape 2026-10-02: headline from a limit order, nullable impact/fee; /dex/swap builds via AMM (31)
LIMIT_ORDER_QUOTE = {
    "source": "limit_order", "requested_amount": 32, "price_impact": None, "lp_fee_percent": None,
    "details": {
        "amm": {"pool_id": "9916d751", "pool_type": "Spectrum", "output_amount": 31,
                "price_impact": 0.55, "lp_fee_percent": 0.5},
        "limit_orders": [{"order_id": "7f5d", "fillable_output": 32}],
    },
}
AMM_QUOTE = {
    "source": "amm", "requested_amount": 31808475717, "price_impact": 0.52, "lp_fee_percent": 0.5,
    "details": {"amm": {"pool_id": "9916d751", "pool_type": "Spectrum", "output_amount": 31808475717,
                        "price_impact": 0.52, "lp_fee_percent": 0.5}, "limit_orders": None},
}


def test_limit_order_headline_uses_amm_output():
    q = parse_quote(LIMIT_ORDER_QUOTE)
    assert q.output == 31
    assert q.price_impact == 0.55
    assert q.lp_fee_percent == 0.5
    assert q.pool_type == "Spectrum"
    assert q.source == "limit_order"


def test_amm_quote():
    q = parse_quote(AMM_QUOTE)
    assert q.output == 31808475717
    assert q.price_impact == 0.52


def test_missing_fields_default_to_zero():
    q = parse_quote({"requested_amount": 5, "price_impact": None, "lp_fee_percent": None, "details": {}})
    assert q.output == 5
    assert q.price_impact == 0.0
    assert q.lp_fee_percent == 0.0
    assert q.pool_id == "?"
