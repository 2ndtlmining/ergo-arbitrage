# Archived one-off scripts

These scripts were written for specific past incidents (a Mew batcher swap and a stuck
swap-order box). They are kept for reference only:

- They hard-code transaction ids, box ids and public keys from those incidents.
- They rebuild contract ErgoTrees by string-replacing a public key, which silently
  produces a different contract if the template changes or the key appears twice.
- They do not go through `ergo/tx_guard.py`.

Do not run them against a live wallet. Use the wallet tool (`python arb.py ...`), which
verifies every transaction with the TX guard and only signs with `--check` / `--execute`.

## Retired execution scripts (2026-10)

`execute_*.py` and `verify_opportunity.py` were moved here when `arb.py` replaced them
(issue #13). They worked, and some were tested live, but each duplicated the quote/sign/monitor
code, and the Crux-based ones take their limits from the Crux API. See the README section
"Retired execution scripts" for the `arb.py` command that replaces each one.
