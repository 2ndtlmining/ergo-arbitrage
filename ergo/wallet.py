"""Wallet box selection for transactions we build ourselves."""


def sum_assets(boxes: list[dict]) -> dict[str, int]:
    total: dict[str, int] = {}
    for b in boxes:
        for a in b.get("assets", []):
            total[a["tokenId"]] = total.get(a["tokenId"], 0) + int(a["amount"])
    return total


def select_boxes(boxes: list[dict], token_id: str | None, token_amount: int, min_erg: int) -> list[dict]:
    """Pick P2PK wallet boxes until they hold `token_amount` of `token_id` and `min_erg` nanoERG.

    Token-holding boxes are taken first (largest first), then plain boxes by value.
    With no token target (`token_id` None or `token_amount` 0) any P2PK box counts as plain.
    Raises ValueError if the wallet can't cover either target.
    """
    p2pk = [b for b in boxes if b.get("ergoTree", "").startswith("0008cd")]
    if not token_id or token_amount <= 0:
        token_id, token_amount = "", 0
    holding = sorted(
        (b for b in p2pk if sum_assets([b]).get(token_id, 0) > 0),
        key=lambda b: sum_assets([b])[token_id], reverse=True,
    )
    plain = sorted((b for b in p2pk if b not in holding), key=lambda b: int(b["value"]), reverse=True)

    chosen: list[dict] = []
    tokens = erg = 0
    for b in holding:
        if tokens >= token_amount:
            break
        chosen.append(b)
        tokens += sum_assets([b])[token_id]
        erg += int(b["value"])
    if tokens < token_amount:
        raise ValueError(f"wallet holds {tokens} raw of token {token_id[:8]}, need {token_amount}")
    for b in plain:
        if erg >= min_erg:
            break
        chosen.append(b)
        erg += int(b["value"])
    if erg < min_erg:
        raise ValueError(f"selected boxes hold {erg} nanoERG, need {min_erg} ERG for fees")
    return chosen
