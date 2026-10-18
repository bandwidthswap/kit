# `bsp/1` test vectors

**Test-only keys.** `inputs.json` and `keys.json` carry the private seeds of the three keys. They are published so that
anyone can reproduce every signature here. Never use them on a node; a Mint should refuse their NYMs.

Written by `tools/vectors.py` from `inputs.json`: fixed seeds, fixed timestamps (T0 = 1800000000, an hour boundary), seven
windows' meter readings and counts, two announcements, the price and the fee. `spec/CONFORMANCE.md` says what each file
is and how `tools/conform.py` checks a directory in this layout. `test_bwkit.py` fails when the kit no longer writes
exactly these files.

| File | What |
|---|---|
| `inputs.json` | the inputs; copy it into your own output directory |
| `keys.json` | the three NYMs with their seeds and names |
| `receipt.json`, `ack.json` | the second window's receipt and acknowledgement |
| `link.json` | the first announcement (any payer, 10% margin, since T0+3600) |
| `settlement-default.json` | the first window, no announcement in force |
| `settlement-disputed.json` | the second window, under the first announcement |
| `settlement-settled.json` | the sixth window, under the second announcement (price 0.2, margin 5%) |
| `commitment.json`, `proof.json` | the hour T0+10800, three settlements |
| `rebuild/` | the three inputs of a rebuild and the ledger it must make |
