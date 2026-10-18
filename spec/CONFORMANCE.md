# Conformance to `bsp/1`

An implementation may call itself a Bandwidth Swap Protocol pilot profile 1 implementation when it reproduces the vectors in `spec/vectors/` byte for byte in every signed and hashed form, and `tools/conform.py` says so.

## What must be reproduced

From the inputs in `spec/vectors/inputs.json` (test-only key seeds, the windows' meter readings and counts, the announcements' parameters, fixed timestamps, the price and the fee fraction) an implementation produces:

| File | What is checked |
|---|---|
| `keys.json` | the three NYMs derived from the seeds (Section 2 of the spec) |
| `receipt.json` | the payer's envelope for the second window: the meter root, the eight fields, and the signature, which is byte for byte the vector's (Ed25519 is deterministic) |
| `ack.json` | the hop's acknowledgement of it, likewise |
| `link.json` | the first announcement, its signature and its hash (Section 7) |
| `settlement-default.json` | the first window, settled with no announcement in force: `link` null, the pilot defaults, the fee |
| `settlement-disputed.json` | the second window, under the first announcement, disputed |
| `settlement-settled.json` | a window under the second announcement, settled at its price and margin |
| `purchase.json` | the payer's purchase on the link (Section 15): its id, which is the hash of the record, and its signature |
| `settlement-purchased.json` | the seventh window, settled against that purchase: the premium on the bytes moved, from the poster's pool, and the fee on both |
| `commitment.json` | the Mint's commitment over the hour with three settlements: the root over the leaves in leaf order with the odd split, and the signature |
| `proof.json` | the proof document for that hour: every index, leaf and path |
| `rebuild/receipts.jsonl`, `acks.jsonl`, `links.jsonl`, `purchases.jsonl` | the four inputs of a rebuild, as the parties would hold them (every envelope, announcement and purchase signed by the test keys) |
| `rebuild/ledger.jsonl` | the ledger a rebuild makes from them (Section 11): every receipt, acknowledgement, settlement and unsigned commitment, in order, with the rebuild's fixed clock |

Settlement records are compared whole, including `t`, which the inputs fix. Numbers must be encoded as Section 3 of the spec says (`0.0`, `5e-05` and `3e-06`, the fee on `5e-05` rounded from 2.5e-06, all occur in the vectors, on purpose). The rebuilt ledger is compared line by line.

## Running a conformance check

```
python3 tools/conform.py spec/vectors          # the reference implementation against its own vectors
python3 tools/conform.py /path/to/your/output  # another implementation's output, in the same layout
```

The directory must hold `inputs.json` (copy it from `spec/vectors/`) and the files above that the implementation produced. The runner computes what each file must contain, compares, verifies every signature and the proof on its own as well, and prints one line per file, `ok` or `FAIL` with the first difference; it exits 0 only when every file passes. A file that is missing is a failure.

An implementation that produces only part of the profile (a verifier, say, that checks commitments and proofs but makes no receipts) may say which parts it reproduces; the runner reports per file, so the claim can be checked.

## Regenerating the vectors

```
python3 tools/vectors.py            # rewrites spec/vectors/ from the reference implementation
```

`test_bwkit.py` checks that the kit still writes exactly the committed vectors and that `conform.py` passes over them, so a change to any encoding fails the tests before it reaches a node. A change that is meant to alter an encoding is a new profile (Section 14 of the spec), not a new set of vectors.

## The keys

The seeds in `inputs.json` and `keys.json` are published. They are for vectors only and MUST NOT be used on any node; a Mint SHOULD refuse their NYMs in its peer list.
