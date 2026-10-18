#!/usr/bin/env python3
"""Write spec/vectors/ from fixed test-only keys and fixed timestamps.

    python3 tools/vectors.py            # rewrites spec/vectors/
    python3 tools/vectors.py --check    # says whether spec/vectors/ is what the kit writes now

The keys are derived from published seeds and are for vectors only: never
use them on a node. The inputs (inputs.json) are what another
implementation starts from; everything else is what it must produce, and
tools/conform.py checks a directory in this layout. Deterministic: Ed25519
signatures are deterministic, every clock is fixed, and every window's
meter readings and the hop's count are given.
"""

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bwkit  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

VECTORS_DIR = os.path.join(os.path.dirname(HERE), "spec", "vectors")
T0 = 1_800_000_000          # 2027-01-15 08:00:00 UTC, an hour boundary
ROLES = ("payer", "hop", "mint")


def seed(label):
    return hashlib.sha256(("bsp/1 vectors, test only: " + label).encode()).hexdigest()


def meters(windows):
    """Cumulative meter readings for the payer, so every window's bytes are the deltas (spec, Section 4)."""
    rx, tx = 1_000_000, 2_000_000
    out = []
    for w in windows:
        drx, dtx = w["payer_bytes"] // 2, w["payer_bytes"] - w["payer_bytes"] // 2
        out.append({"t0": w["t_first"], "rx0": rx, "tx0": tx, "t1": w["t_last"], "rx1": rx + drx, "tx1": tx + dtx, "reset": False})
        rx, tx = rx + drx, tx + dtx
    return out


def inputs():
    """The fixed inputs. Chosen so the settlements cover every number encoding (0.0, 5e-05, 2.5e-06, 0.01)."""
    windows = [
        # payer_bytes, hop_bytes (None: the hop declined), t_first, t_last; settled_at is the Mint's clock
        {"t_first": T0, "t_last": T0 + 3600, "payer_bytes": 100_000_000, "hop_bytes": 100_500_000},         # defaults: settled
        {"t_first": T0 + 3600, "t_last": T0 + 7200, "payer_bytes": 50_000_000, "hop_bytes": 60_000_000},    # link A: disputed
        {"t_first": T0 + 7200, "t_last": T0 + 9000, "payer_bytes": 1_000_000, "hop_bytes": None},           # declined: no ack
        {"t_first": T0 + 9000, "t_last": T0 + 10800, "payer_bytes": 500_000, "hop_bytes": 1_600_000},       # link A: disputed, 5e-05
        {"t_first": T0 + 10800, "t_last": T0 + 11400, "payer_bytes": 1712, "hop_bytes": 1712},              # link B: settled, 0.0
        {"t_first": T0 + 11400, "t_last": T0 + 12600, "payer_bytes": 20_000_000, "hop_bytes": 20_100_000},  # link B: settled
        {"t_first": T0 + 12600, "t_last": T0 + 14400, "payer_bytes": 30_000_000, "hop_bytes": 31_000_000},  # link B: settled
    ]
    for w, m in zip(windows, meters(windows)):
        w["meter"] = m
        w["settled_at"] = w["t_last"] + 2
    return {
        "proto": 1,
        "note": "Test-only seeds; never use these keys on a node. Timestamps are fixed; T0 is an hour boundary.",
        "seeds": {r: seed(r) for r in ROLES},
        "names": {"payer": "vec-payer", "hop": "vec-hop", "mint": "vec-mint"},
        "price_usd_per_gb": 0.1, "steward_fee": 0.05,
        "links": [
            {"payer": "*", "price": 0.1, "margin": 0.1, "min_bytes": 65536, "patience_windows": 2, "clock_tolerance_s": 5,
             "medium": "wifi", "range_class": "building", "since": T0 + 3600},
            {"payer": "payer", "price": 0.2, "margin": 0.05, "min_bytes": 65536, "patience_windows": 0, "clock_tolerance_s": 5,
             "medium": "wire", "range_class": "room", "since": T0 + 10800},
        ],
        "windows": windows,
        "purchase": {"min_bytes": 20_000_000, "premium_per_gb": 0.02, "windows": 1, "escrow": 0.01, "deadline": T0 + 86400,
                     "posted_at": T0 + 12700, "steward_margin": 0.01},    # governs the seventh window, the one ending after it
        "standalone": {"receipt": 1, "settlement-default": 0, "settlement-disputed": 1, "settlement-settled": 5, "link": 0,
                       "settlement-purchased": 6, "commitment_hour": T0 + 10800},
        "rebuild_at": T0 + 20000,
    }


def keys(inp):
    return {r: Ed25519PrivateKey.from_private_bytes(bytes.fromhex(inp["seeds"][r])) for r in ROLES}


def generate(inp):
    """Everything the vectors hold, from the inputs: {file name: object or list of records}."""
    priv = keys(inp)
    nym = {r: bwkit.nym_of_pub(priv[r].public_key()) for r in ROLES}
    names = inp["names"]
    links = []
    for spec in inp["links"]:
        body = dict(bwkit.PILOT_LINK)
        body.update({k: v for k, v in spec.items() if k in bwkit.LINK_PARAMS})
        body.update({"kind": "link", "proto": 1, "relay": nym["hop"], "domain": nym["mint"], "since": spec["since"],
                     "payer": nym["payer"] if spec["payer"] == "payer" else spec["payer"]})
        links.append(dict(body, sig=bwkit.sign_link(priv["hop"], body)))
    pu = inp["purchase"]
    body = {"kind": "purchase", "proto": 1, "poster": nym["payer"], "program": "payer",
            "route": {"from": nym["payer"], "to": nym["hop"], "not_via": []}, "min_bytes": pu["min_bytes"],
            "premium_per_gb": pu["premium_per_gb"], "windows": pu["windows"], "deadline": pu["deadline"], "proving": None,
            "escrow": pu["escrow"], "unit": "USD", "posted_at": pu["posted_at"], "matches": None}
    purchase = dict(body, id=bwkit.purchase_id(body), sig=bwkit.sign_purchase(priv["payer"], body))
    book = bwkit.PurchaseBook(None, inp["steward_fee"], pu["steward_margin"])
    book.add(purchase)
    receipts, acks, settlements = [], [], []
    for w in inp["windows"]:
        r = {"payer": nym["payer"], "hop": nym["hop"], "hop_index": 1, "merkle_root": bwkit.meter_root(nym["payer"], w["meter"]),
             "bytes": w["payer_bytes"], "t_first": w["t_first"], "t_last": w["t_last"], "prev_sig": ""}
        env = {"kind": "receipt", "proto": 1, "from": names["payer"], "impl": bwkit.IMPL, "receipt": r,
               "sig": bwkit.sign_receipt(priv["payer"], r), "meter": w["meter"]}
        receipts.append(env)
        if w["hop_bytes"] is None:
            acks.append(None)
            settlements.append(None)
            continue
        a = dict(r, bytes=w["hop_bytes"], prev_sig=env["sig"])
        ack = {"kind": "ack", "proto": 1, "from": names["hop"], "impl": bwkit.IMPL, "receipt": a,
               "sig": bwkit.sign_receipt(priv["hop"], a), "meter": None}
        acks.append(ack)
        link = bwkit.link_in_force(links, r["payer"], r["hop"], r["t_first"])
        st = bwkit.resolve(env, ack, inp["price_usd_per_gb"], w["settled_at"], link, inp["steward_fee"], book.context(r, r["t_last"]))
        book.note(st)
        settlements.append(st)
    sa = inp["standalone"]
    hour = sa["commitment_hour"]
    in_hour = [s for s in settlements if s and bwkit.commit_hour(s) == hour]
    commitment = bwkit.make_commitment(priv["mint"], in_hour, hour, max(s["t"] for s in in_hour))
    proof = bwkit.make_proof(commitment, in_hour)
    ledger, _ = bwkit.rebuild([(i + 1, e) for i, e in enumerate(receipts)],
                              [(i + 1, a) for i, a in enumerate(x for x in acks if x)],
                              [(i + 1, l) for i, l in enumerate(links)],
                              inp["price_usd_per_gb"], inp["steward_fee"], inp["rebuild_at"],
                              purchases=[(1, purchase)], steward_margin=pu["steward_margin"])
    return {
        "keys.json": {r: {"seed": inp["seeds"][r], "nym": nym[r], "name": names[r]} for r in ROLES},
        "receipt.json": receipts[sa["receipt"]],
        "ack.json": acks[sa["receipt"]],
        "link.json": links[sa["link"]],
        "settlement-default.json": settlements[sa["settlement-default"]],
        "settlement-disputed.json": settlements[sa["settlement-disputed"]],
        "settlement-settled.json": settlements[sa["settlement-settled"]],
        "purchase.json": purchase,
        "settlement-purchased.json": settlements[sa["settlement-purchased"]],
        "commitment.json": commitment,
        "proof.json": proof,
        "rebuild/receipts.jsonl": receipts,
        "rebuild/acks.jsonl": [a for a in acks if a],
        "rebuild/links.jsonl": links,
        "rebuild/purchases.jsonl": [purchase],
        "rebuild/ledger.jsonl": ledger,
    }


def render(name, obj):
    if name.endswith(".jsonl"):
        return "".join(json.dumps(e, sort_keys=True) + "\n" for e in obj)
    return json.dumps(obj, indent=1, sort_keys=True) + "\n"


README = """# `bsp/1` test vectors

**Test-only keys.** `inputs.json` and `keys.json` carry the private seeds of the three keys. They are published so that
anyone can reproduce every signature here. Never use them on a node; a Mint should refuse their NYMs.

Written by `tools/vectors.py` from `inputs.json`: fixed seeds, fixed timestamps (T0 = 1800000000, an hour boundary), seven
windows' meter readings and counts, two announcements, a purchase, the price and the fee. `spec/CONFORMANCE.md` says what each file
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
| `purchase.json` | the payer's purchase on the link, posted at T0+12700: 20 MB a window, 2 cents a GB, one window, a cent escrowed |
| `settlement-purchased.json` | the seventh window, settled against it: the premium on 30 MB from the poster, the fee on both |
| `commitment.json`, `proof.json` | the hour T0+10800, three settlements |
| `rebuild/` | the four inputs of a rebuild and the ledger it must make |
"""


def write(directory=VECTORS_DIR):
    inp = inputs()
    files = generate(inp)
    os.makedirs(os.path.join(directory, "rebuild"), exist_ok=True)
    with open(os.path.join(directory, "inputs.json"), "w") as f:
        f.write(render("inputs.json", inp))
    with open(os.path.join(directory, "README.md"), "w") as f:
        f.write(README)
    for name, obj in files.items():
        with open(os.path.join(directory, name), "w") as f:
            f.write(render(name, obj))
    return files


def check(directory=VECTORS_DIR):
    """The names of the files on disk that differ from what the kit writes now (missing files included)."""
    with open(os.path.join(directory, "inputs.json")) as f:
        inp = json.load(f)
    differ = []
    for name, obj in generate(inp).items():
        p = os.path.join(directory, name)
        if not os.path.exists(p) or open(p).read() != render(name, obj):
            differ.append(name)
    return differ


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        d = check()
        print("spec/vectors/ is what the kit writes" if not d else "spec/vectors/ differs: " + ", ".join(d))
        sys.exit(1 if d else 0)
    files = write()
    print("wrote %d files to %s" % (len(files) + 2, VECTORS_DIR))
