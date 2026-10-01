# Archived one-off scripts

These scripts were written for specific past incidents (a Mew batcher swap and a stuck
swap-order box). They are kept for reference only:

- They hard-code transaction ids, box ids and public keys from those incidents.
- They rebuild contract ErgoTrees by string-replacing a public key, which silently
  produces a different contract if the template changes or the key appears twice.
- They do not go through `ergo/tx_guard.py`.

Do not run them against a live wallet. Use the `execute_*.py` scripts in the repo root,
which verify every transaction with the TX guard and only sign with `--execute`.
