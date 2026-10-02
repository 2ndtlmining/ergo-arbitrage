"""Wallet box selection for transactions we build ourselves."""

# Distinct tokens one change box may carry. A box is at most 4096 bytes and each token takes ~34+;
# beyond this the node would reject the transaction, so selection refuses it with a clear message.
MAX_CHANGE_TOKENS = 64


def sum_assets(boxes: list[dict]) -> dict[str, int]:
    total: dict[str, int] = {}
    for b in boxes:
        for a in b.get("assets", []):
            total[a["tokenId"]] = total.get(a["tokenId"], 0) + int(a["amount"])
    return total


def select_boxes(boxes: list[dict], token_id: str | None, token_amount: int, min_erg: int) -> list[dict]:
    """Pick P2PK wallet boxes until they hold `token_amount` of `token_id` and `min_erg` nanoERG.

    Boxes holding the target token are taken first (largest first). The ERG then comes from
    token-free boxes first (largest first), and only then from boxes holding other tokens (fewest
    tokens first), so plain ERG spends don't drag airdropped tokens into the change.
    Raises ValueError if the wallet can't cover either target, or if the selected boxes hold more
    distinct tokens than one change box can carry (MAX_CHANGE_TOKENS).
    """
    p2pk = [b for b in boxes if b.get("ergoTree", "").startswith("0008cd")]
    if not token_id or token_amount <= 0:
        token_id, token_amount = "", 0
    holding = sorted(
        (b for b in p2pk if sum_assets([b]).get(token_id, 0) > 0),
        key=lambda b: sum_assets([b])[token_id], reverse=True,
    )
    plain = sorted((b for b in p2pk if b not in holding),
                   key=lambda b: (len(b.get("assets", [])), -int(b["value"])))

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
    distinct = len(sum_assets(chosen))
    if distinct > MAX_CHANGE_TOKENS:
        raise ValueError(f"the selected boxes hold {distinct} distinct tokens, more than one change box can "
                         f"carry ({MAX_CHANGE_TOKENS}); consolidate them first (send some tokens to yourself "
                         f"in smaller batches)")
    return chosen
