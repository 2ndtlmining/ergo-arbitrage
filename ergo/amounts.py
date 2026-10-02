"""Amount parsing and 'all' calculations for wallet actions."""
import config


def parse_amount(text: str):
    """A positive number, or the string "all"."""
    if str(text).strip().lower() == "all":
        return "all"
    value = float(text)
    if value <= 0:
        raise ValueError("amount must be positive (or 'all')")
    return value


def token_total(boxes: list[dict], token_id: str) -> int:
    return sum(int(a["amount"]) for b in boxes for a in b.get("assets", []) if a["tokenId"] == token_id)


def erg_spendable(boxes: list[dict], fee: int) -> int:
    """nanoERG that can leave the wallet: everything minus the fee, minus a minimum
    box for the change if the wallet also holds tokens (they need a box to stay in)."""
    total = sum(int(b["value"]) for b in boxes)
    holds_tokens = any(b.get("assets") for b in boxes)
    return max(total - fee - (config.ERG_MIN_BOX_NANO if holds_tokens else 0), 0)
