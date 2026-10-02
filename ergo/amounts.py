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


def erg_spendable(boxes: list[dict], fee: int, keep_box: bool = False) -> int:
    """nanoERG that can leave the wallet: everything minus the fee, minus a minimum
    box for the change if the wallet also holds tokens (they need a box to stay in).
    keep_box: always keep one minimum box (a pool swap pays its output into a wallet box)."""
    total = sum(int(b["value"]) for b in boxes)
    holds_tokens = any(b.get("assets") for b in boxes)
    return max(total - fee - (config.ERG_MIN_BOX_NANO if holds_tokens or keep_box else 0), 0)
