"""The Figure 2 receipt and the smaller-count rule.

    (payer NYM, hop NYM, hop index, merkle root, bytes, t_first, t_last, prev receipt sig)  signed by payer

The tuple, the signing domain, the agreement thresholds and the two lines of
`bwkit.resolve` that compute a settlement are copied here exactly from
bwkit.py. They are copied rather than imported because bwkit.py imports the
`cryptography` package at module level and the simulator must run without
it. When bwkit does import, the copies are checked against it at import time
(and `tests.py` checks `settle_counts` against `bwkit.resolve` with real
keys).

The simulator does not sign anything. `sign` is a SHA-256 over the signer's
name and the canonical bytes: deterministic, and enough to chain receipts and
tell two parties' copies apart, but not a signature.
"""

import hashlib
import json

FIELDS = ("payer", "hop", "hop_index", "merkle_root", "bytes", "t_first", "t_last", "prev_sig")
DOMAIN = b"bandwidthswap/receipt/v1\n"   # prefixed to every signed message
AGREE_PCT = 1.0                         # two honest meters of one window differ by less than this
AGREE_MIN_BYTES = 65536                 # or by less than this, whichever is larger
DEFAULT_PRICE_USD_PER_GB = 0.10         # the lab's posted price; dollars, no token

SOURCE = "copied from bwkit.py"
try:  # pragma: no cover - depends on the machine
    import bwkit as _bwkit
    if (_bwkit.FIELDS, _bwkit.DOMAIN, _bwkit.AGREE_PCT, _bwkit.AGREE_MIN_BYTES,
            _bwkit.DEFAULT_PRICE_USD_PER_GB) != (FIELDS, DOMAIN, AGREE_PCT, AGREE_MIN_BYTES,
                                                 DEFAULT_PRICE_USD_PER_GB):
        raise ImportError("bwkit.py's constants have moved away from sim/receipts.py")
    SOURCE = "checked against bwkit %s" % _bwkit.VERSION
except ImportError as e:
    if "moved away" in str(e):
        raise
    _bwkit = None


def canon(receipt):
    """The signed bytes: the Figure 2 tuple as a JSON array, in field order (bwkit.canon)."""
    return DOMAIN + json.dumps([receipt[f] for f in FIELDS], separators=(",", ":"), ensure_ascii=True).encode()


def sign(signer, receipt):
    """Stand-in for the ed25519 signature: SHA-256 over the signer and the canonical bytes."""
    return hashlib.sha256(signer.encode() + b"\0" + canon(receipt)).hexdigest()


def merkle_root(payer, window, nbytes):
    """Stand-in for the Merkle root over the window's chunks (bwkit uses the meter readings)."""
    s = "sim-meter-v1|%s|%d|%d" % (payer, window, nbytes)
    return hashlib.sha256(s.encode()).hexdigest()


def make_receipt(payer, hop, hop_index, root, nbytes, t_first, t_last, prev_sig):
    r = dict(zip(FIELDS, (payer, hop, int(hop_index), root, int(nbytes), int(t_first), int(t_last), prev_sig)))
    return r


def settle_counts(x, y):
    """bwkit.resolve's rule: the smaller of the two signed counts, `settled` when they agree.

    Returns (settled_bytes, status). Neither party is bound beyond what it signed.
    """
    settled = min(x, y)
    agree = abs(x - y) <= max(AGREE_MIN_BYTES, AGREE_PCT / 100.0 * max(x, y))
    return settled, ("settled" if agree else "disputed")
