#!/usr/bin/env python3
"""Check a directory in the layout of spec/vectors/ against the bsp/1 vectors.

    python3 tools/conform.py <dir>

<dir> holds inputs.json (copied from spec/vectors/) and the files the
implementation under test produced from it (spec/CONFORMANCE.md lists
them). For each file the runner computes what it must contain and prints
`ok` or `FAIL` with the first difference; it also verifies every signature
and the proof on their own. `impl` members are informational and ignored.
Exit 0 only when every file passes.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import bwkit  # noqa: E402
import vectors  # noqa: E402

IGNORE = ("impl",)


def strip(obj):
    if isinstance(obj, dict):
        return {k: strip(v) for k, v in obj.items() if k not in IGNORE}
    if isinstance(obj, list):
        return [strip(v) for v in obj]
    return obj


def first_diff(a, b, path="$"):
    """Where two parsed JSON values first differ, or None."""
    if type(a) is not type(b) or (isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, bool)):
        return "%s: %s here, %s expected" % (path, json.dumps(a)[:80], json.dumps(b)[:80])
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                return "%s: missing %s" % (path, k)
            if k not in b:
                return "%s: unexpected %s" % (path, k)
            d = first_diff(a[k], b[k], path + "." + k)
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return "%s: %d items here, %d expected" % (path, len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_diff(x, y, "%s[%d]" % (path, i))
            if d:
                return d
        return None
    if a != b or json.dumps(a) != json.dumps(b):      # 1 and 1.0 compare equal in Python and encode differently
        return "%s: %s here, %s expected" % (path, json.dumps(a)[:80], json.dumps(b)[:80])
    return None


def load(path):
    with open(path) as f:
        if path.endswith(".jsonl"):
            return [json.loads(line) for line in f if line.strip()]
        return json.load(f)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__)
        return 2
    d = argv[0]
    try:
        with open(os.path.join(d, "inputs.json")) as f:
            inp = json.load(f)
    except (OSError, ValueError) as e:
        print("FAIL inputs.json: %s" % e)
        return 1
    expected = vectors.generate(inp)
    nym = {r: v["nym"] for r, v in expected["keys.json"].items()}
    ok = True
    got = {}
    for name in sorted(expected):
        p = os.path.join(d, name)
        try:
            got[name] = load(p)
        except (OSError, ValueError) as e:
            print("FAIL %s: %s" % (name, e))
            ok = False
            continue
        diff = first_diff(strip(got[name]), strip(expected[name]))
        print(("ok   %s" if not diff else "FAIL %s: %s") % ((name, diff) if diff else name))
        ok = ok and not diff
    # the independent checks: signatures and the proof, with nothing but the files
    checks = []
    if "purchase.json" in got:
        checks.append(("purchase.json signed by the payer, id the hash of the record", lambda: (bwkit.check_purchase(got["purchase.json"]), bwkit.verify_purchase(got["purchase.json"]))[1]
                       and got["purchase.json"]["poster"] == nym["payer"]))
    if "receipt.json" in got:
        checks.append(("receipt.json signed by the payer", lambda: bwkit.check_envelope(got["receipt.json"]) == nym["payer"]))
    if "ack.json" in got:
        checks.append(("ack.json signed by the hop, chained to receipt.json", lambda: bwkit.check_envelope(got["ack.json"]) == nym["hop"]
                       and got["ack.json"]["receipt"]["prev_sig"] == got["receipt.json"]["sig"]))
    if "link.json" in got:
        checks.append(("link.json signed by the hop", lambda: (bwkit.check_link(got["link.json"]), bwkit.verify_link(got["link.json"]))[1]))
    if "commitment.json" in got:
        checks.append(("commitment.json signed by the Mint", lambda: bwkit.verify_commitment(got["commitment.json"])
                       and got["commitment.json"]["mint"] == nym["mint"]))
    if "proof.json" in got:
        checks.append(("proof.json verifies against the Mint's NYM", lambda: bwkit.check_proof(got["proof.json"], nym["mint"])[0]))
    if "rebuild/ledger.jsonl" in got:
        def consistent():
            """Every commitment in the ledger is the root over the ledger's own settlements for its hour, in leaf order."""
            ss, ok = {}, True
            for e in got["rebuild/ledger.jsonl"]:
                if e.get("kind") == "settlement":
                    ss.setdefault(bwkit.commit_hour(e), []).append(e)
                elif e.get("kind") == "commitment":
                    _, leaves = bwkit.hour_leaves(ss.get(e["window"], []))
                    ok = ok and bool(leaves) and bwkit.merkle_root(leaves).hex() == e["root"] and len(leaves) == e["count"]
            return ok and bool(ss)
        checks.append(("rebuild/ledger.jsonl: each commitment is the root over its hour's settlements", consistent))
    for name, fn in checks:
        try:
            good = bool(fn())
        except Exception as e:   # noqa: BLE001 - any failure is a failed check, named
            good = False
            name += " (%s)" % e
        print(("ok   %s" if good else "FAIL %s") % name)
        ok = ok and good
    print("conformance: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
