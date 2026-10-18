#!/usr/bin/env python3
"""bwkit: the Bandwidth Swap pilot receipt daemon and Mint, in one file.

The reference implementation of the Bandwidth Swap Protocol, pilot profile 1
(spec/PROTOCOL.md, `bsp/1`). Needs Python 3.9+ and the `cryptography`
package (Debian: python3-cryptography).

Three roles, chosen by "role" in the config file:

  payer  (bw-house)  Every hour: read the tunnel's byte counters, build the
                     Figure 2 receipt for the window, sign it, ask the hop
                     to acknowledge it, send receipt and ack to the Mint.
  hop    (bw-forest, bw-neighbor)
                     Serve /ack: check the payer's signature, its clock and
                     the hop's patience with it, read its own counters for
                     the same window, sign its own count with prev_sig = the
                     payer's signature, send that ack to the Mint too, and
                     return it. `bwkit announce` signs the link's parameters
                     (price, margin, patience, ...) and registers them with
                     the Mint.
  mint   (bw-mint)   Serve /receipt and /link: verify every signature,
                     append each receipt and ack to an append-only ledger,
                     keep the link announcements, and settle a window once
                     both halves are there, at the smaller of the two signed
                     counts, under the announcement in force at the window's
                     start, with the Steward's fee on every settlement.

The Mint also commits: after every settlement it signs the Merkle root over
that hour's settlements (paper, Section 4) and appends the commitment to the
ledger; GET /commitments lists the current one for every hour, GET /proof
and `bwkit prove` give the inclusion proofs for an hour and `bwkit
verify-proof` checks them with no node state. `bwkit rebuild` makes the
ledger again from the payer's receipts.jsonl, the hop's acks.jsonl and the
announcements alone and, with --compare, says whether the Mint's ledger and
its commitments agree with it.

The Mint also reports: `bwkit report` (the bwkit-status timer, every two
minutes) summarises the ledger per node, names and roles, with the latest
commitment, the Mint's NYM and the proofs of that hour, and posts it to the
status page's Worker, so bandwidthswap.com can say which nodes are up and a
browser can check the commitment while nothing reaches the LAN.

The receipt is the paper's Figure 2:

  (payer NYM, hop NYM, hop index, merkle root, bytes, t_first, t_last, prev receipt sig)

signed by the payer. The hop's ack is the same tuple with the hop's own byte
count and prev_sig set to the payer's signature, signed by the hop: when the
counts agree it is the countersignature that closes the chain, when they
don't it is the dispute, and either way the Mint resolves it from the two
signed receipts alone.

Phase 0 has no chunked payload, so there is nothing to build a Merkle tree
over. merkle_root carries the payer's commitment to its meter readings for
the window (a SHA-256 over them) until a chunked data plane exists.

Files: config in /etc/bwkit/config.json (BWKIT_CONFIG), everything else in
/var/lib/bwkit (BWKIT_DIR): key.priv, peers.json, state.json, ledger.jsonl,
links.jsonl, outbox/, sent/. Every node keeps its own signed copies: the
payer appends receipts.jsonl and acks.jsonl, the hop acks.jsonl and
links.jsonl, and both append sent.jsonl with the Mint's answer to each
envelope, which carries the Mint's signed commitment when one was made.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

try:
    import steward_rules as rules   # the Steward's sizes and cap, one file shared with sim/; installed beside bwkit
except ImportError:                 # an older install without it: everything but purchases and the Steward still runs
    rules = None

VERSION = "0.4.0"
IMPL = "bwkit/" + VERSION               # `impl` in every envelope: informational, never the profile
PROTO = 1                               # the profile every record this kit writes carries (spec, Section 14)
PROTOS = (1,)                           # the profiles it accepts; a record without `proto` is profile 1
FIELDS = ("payer", "hop", "hop_index", "merkle_root", "bytes", "t_first", "t_last", "prev_sig")
DOMAIN = b"bandwidthswap/receipt/v1\n"   # prefixed to every signed receipt and ack
COMMIT_DOMAIN = b"bandwidthswap/commitment/v1\n"   # prefixed to every signed commitment
LINK_DOMAIN = b"bandwidthswap/link/v1\n"           # prefixed to every signed link announcement
LEAF_PREFIX, NODE_PREFIX = b"\x00", b"\x01"       # RFC 6962: a leaf and an inner node hash differently
LEAF_SKIP = ("t",)                      # the settlement's own clock stays out of its leaf (see leaf_of)
CLOCK_TOLERANCE_S = 5                   # the pilot's clock rule: 5 seconds, decline; also the Mint's slack on `since`
AGREE_PCT = 1.0                         # the pilot's agreement margin: two honest meters of one wired window differ by less
AGREE_MIN_BYTES = 65536                 # or by less than this, whichever is larger
DEFAULT_PORT = 8710
DEFAULT_PRICE_USD_PER_GB = 0.10         # the lab's posted price; dollars, no token
DEFAULT_STEWARD_FEE = 0.05              # f: the fraction of every fee that is the Network Steward's (paper, 3.3)
PILOT_LINK = {                          # what governs a window with no announcement in force (spec, Appendix B)
    "price": DEFAULT_PRICE_USD_PER_GB, "unit": "USD/GB", "carry": 0, "clock_tolerance_s": CLOCK_TOLERANCE_S,
    "margin": AGREE_PCT / 100.0, "min_bytes": AGREE_MIN_BYTES, "patience_windows": 0, "window": "hour",
    "medium": "wire", "range_class": "room", "sig_scheme": "ed25519", "receipt_lifetime_s": None,
    "light_time_s": 0, "contact_plan": None,
}
LINK_PARAMS = tuple(PILOT_LINK)         # the parameters a hop's config may set under "link"
LINK_FIELDS = ("kind", "proto", "relay", "payer") + LINK_PARAMS + ("domain", "since", "sig")
PROFILE_1_CONSTANTS = {"unit": "USD/GB", "carry": 0, "window": "hour", "sig_scheme": "ed25519",
                       "light_time_s": 0, "contact_plan": None}   # what profile 1 fixes; the paper has the rest
SETTLEMENT_PROFILE_KEYS = ("proto", "steward_fee", "fee_usd", "margin", "min_bytes", "link")   # what 0.2.0 settlements lack
PURCHASE_DOMAIN = b"bandwidthswap/purchase/v1\n"   # prefixed to every signed purchase (spec, Section 15)
PURCHASE_FIELDS = ("kind", "proto", "id", "poster", "program", "route", "min_bytes", "premium_per_gb", "windows", "deadline",
                   "proving", "escrow", "unit", "posted_at", "matches", "sig")
PROGRAMS = ("payer", "match", "second_bridge", "first_link")   # who posted a purchase and under which rule (paper, Section 7)
TOPUP_DOMAIN = b"bandwidthswap/top-up/v1\n"   # a poster adds escrow to its own purchase (spec, Section 15)
CLOSE_DOMAIN = b"bandwidthswap/close/v1\n"    # a poster withdraws its own purchase; the unspent escrow returns
TOPUP_FIELDS = ("kind", "proto", "id", "poster", "purchase", "escrow", "posted_at", "sig")
CLOSE_FIELDS = ("kind", "proto", "id", "poster", "purchase", "posted_at", "sig")
PURCHASE_KEYS = ("purchase", "match", "premium_usd", "refund_usd", "from_poster_usd", "from_steward_usd", "steward_held_usd")
DEFAULT_STEWARD = {                     # the Steward's parameters (paper, Section 7; the mockup's values), under "steward" in the Mint's config
    "m": 0.04, "margin": 0.01, "program_window_s": 7 * 86400, "first_link_usd": 1.0, "first_link_premium": 0.05,
    "min_payer_age_s": 86400, "bounty_deadline_s": 7 * 86400,
}
DEFAULT_MAP_PATH = "/etc/bwkit/map.json"   # BWKIT_MAP: the membership's map of domains, places and which node is where
DECLINE_WORDS = ("clock", "patience", "since", "domain", "profile", "replay", "route")   # 409 reasons, the word before the colon
TOKEN_RE = re.compile(r"^[a-z0-9-]{1,32}$")   # a medium or range class
HTTP_TIMEOUT_S = 10
HOP_FLUSH_DELAY_S = 5                   # the hop sends its own copy of an ack this long after answering the payer
FLUSH_EVERY_S = 300                     # and retries whatever is left in its outbox this often
MAX_BODY = 1 << 20
STATUS_TIMEOUT_S = 10                   # posting the status report
HISTORY_HOURS = 12                      # the status report carries each node's windows of the last twelve hours
HISTORY_MAX = 36                        # at most this many per node (the forced tests made several in one hour)
REPORT_MAX = 64 * 1024                  # the status Worker refuses a larger body
PRIVATE_PATTERNS = (                    # what a status report may never carry
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),               # an IPv4 address
    re.compile(r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", re.I),  # a MAC address
)


# ---------------------------------------------------------------- encoding

def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64u_dec(s):
    if not isinstance(s, str):
        raise ValueError("not a string")
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def canon_json(obj):
    """Canonical JSON (spec, Section 3): sorted keys, no whitespace, ASCII only."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def nym_of_pub(pub):
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "ed25519:" + b64u(raw)


def pub_of_nym(nym):
    if not isinstance(nym, str) or not nym.startswith("ed25519:"):
        raise ValueError("not an ed25519 NYM")
    raw = b64u_dec(nym[len("ed25519:"):])
    if len(raw) != 32:
        raise ValueError("NYM is not 32 bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def check_proto(rec):
    """Raise ValueError when rec names a profile this kit does not accept. No `proto` is profile 1."""
    p = rec.get("proto")
    if p is not None and (not is_int(p) or p not in PROTOS):
        raise ValueError("profile %r is not accepted (this implementation accepts %s)" % (p, ", ".join(str(x) for x in PROTOS)))


def canon(receipt):
    """The signed bytes: the Figure 2 tuple as a JSON array, in field order."""
    return DOMAIN + json.dumps([receipt[f] for f in FIELDS], separators=(",", ":"), ensure_ascii=True).encode()


def sign_receipt(priv, receipt):
    return b64u(priv.sign(canon(receipt)))


def verify_receipt(nym, receipt, sig):
    """True when sig is nym's signature over receipt."""
    try:
        pub_of_nym(nym).verify(b64u_dec(sig), canon(receipt))
        return True
    except (InvalidSignature, ValueError):
        return False


def check_receipt(r):
    """Raise ValueError unless r has exactly the Figure 2 fields with sane types."""
    if not isinstance(r, dict) or set(r) != set(FIELDS):
        raise ValueError("receipt must have exactly the fields %s" % ", ".join(FIELDS))
    pub_of_nym(r["payer"])
    pub_of_nym(r["hop"])
    for k in ("hop_index", "bytes", "t_first", "t_last"):
        if not is_int(r[k]) or r[k] < 0:
            raise ValueError("%s must be a non-negative integer" % k)
    if r["hop_index"] < 1:
        raise ValueError("hop_index starts at 1")
    if r["t_first"] > r["t_last"]:
        raise ValueError("t_first is after t_last")
    m = r["merkle_root"]
    if not isinstance(m, str) or len(m) != 64 or any(c not in "0123456789abcdef" for c in m):
        raise ValueError("merkle_root must be 64 hex characters")
    if not isinstance(r["prev_sig"], str):
        raise ValueError("prev_sig must be a string")
    if r["prev_sig"]:
        if len(b64u_dec(r["prev_sig"])) != 64:
            raise ValueError("prev_sig is not an ed25519 signature")


def check_envelope(env):
    """Validate an envelope's shape and signature. Returns the signer's NYM."""
    if not isinstance(env, dict):
        raise ValueError("envelope must be an object")
    kind = env.get("kind")
    if kind not in ("receipt", "ack"):
        raise ValueError("kind must be receipt or ack")
    check_proto(env)
    r = env.get("receipt")
    check_receipt(r)
    signer = r["payer"] if kind == "receipt" else r["hop"]
    if kind == "receipt" and r["prev_sig"] != "":
        raise ValueError("a payer's receipt has no previous signature in Phase 0")
    if kind == "ack" and r["prev_sig"] == "":
        raise ValueError("an ack must carry the payer's signature as prev_sig")
    sig = env.get("sig")
    if not isinstance(sig, str) or not verify_receipt(signer, r, sig):
        raise ValueError("bad signature")
    return signer


def window_key(r):
    return "%s|%s|%d|%d|%d" % (r["payer"], r["hop"], r["hop_index"], r["t_first"], r["t_last"])


def meter_root(payer_nym, meter):
    """Phase 0 stand-in for the Merkle root: the payer's commitment to its meter readings."""
    s = "bwkit-meter-v1|%s|%d|%d|%d|%d|%d|%d" % (
        payer_nym, meter["t0"], meter["rx0"], meter["tx0"], meter["t1"], meter["rx1"], meter["tx1"])
    return hashlib.sha256(s.encode()).hexdigest()


def assert_public(text):
    """Raise ValueError when text carries an address. A status report is names, roles and signed records only."""
    for pat in PRIVATE_PATTERNS:
        m = pat.search(text)
        if m:
            raise ValueError("a status report may not carry an address (%s)" % m.group(0))


# ---------------------------------------------------------------- link announcements (spec, Section 7)

def link_body(rec):
    return {k: v for k, v in rec.items() if k != "sig"}


def link_canon(rec):
    """The signed bytes of an announcement: the domain string and the canonical JSON of the record without sig."""
    return LINK_DOMAIN + canon_json(link_body(rec)).encode()


def sign_link(priv, body):
    return b64u(priv.sign(LINK_DOMAIN + canon_json(body).encode()))


def verify_link(rec):
    """True when rec carries its relay's signature."""
    try:
        pub_of_nym(rec["relay"]).verify(b64u_dec(rec["sig"]), link_canon(rec))
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


def link_hash(rec):
    """An announcement's name: the SHA-256 of its canonical JSON, signature included."""
    return hashlib.sha256(canon_json(rec).encode()).hexdigest()


def check_link(rec):
    """Raise ValueError unless rec has exactly an announcement's fields with sane types. Signature not checked here."""
    if not isinstance(rec, dict) or set(rec) != set(LINK_FIELDS):
        raise ValueError("an announcement has exactly the fields %s" % ", ".join(LINK_FIELDS))
    if rec["kind"] != "link":
        raise ValueError("kind must be link")
    check_proto(rec)
    pub_of_nym(rec["relay"])
    pub_of_nym(rec["domain"])
    if rec["payer"] != "*":
        pub_of_nym(rec["payer"])
    for k in ("price", "margin", "carry"):
        v = rec[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0 or v in (float("inf"),):
            raise ValueError("%s must be a non-negative number" % k)
    if rec["margin"] > 1:
        raise ValueError("margin is a fraction, at most 1")
    for k in ("min_bytes", "clock_tolerance_s", "patience_windows", "light_time_s", "since"):
        if not is_int(rec[k]) or rec[k] < 0:
            raise ValueError("%s must be a non-negative integer" % k)
    if rec["receipt_lifetime_s"] is not None and (not is_int(rec["receipt_lifetime_s"]) or rec["receipt_lifetime_s"] < 0):
        raise ValueError("receipt_lifetime_s must be null or a non-negative integer")
    for k in ("unit", "window", "sig_scheme"):
        if not isinstance(rec[k], str):
            raise ValueError("%s must be a string" % k)
    for k in ("medium", "range_class"):
        if not isinstance(rec[k], str) or not TOKEN_RE.match(rec[k]):
            raise ValueError("%s is 1 to 32 characters of a-z, 0-9 and -" % k)
    if not isinstance(rec["sig"], str) or len(b64u_dec(rec["sig"])) != 64:
        raise ValueError("sig is not an ed25519 signature")


def profile_1_link(rec):
    """Why rec is not a profile-1 announcement, or None. Carry, light time, contact plans and other units are the paper's."""
    for k, v in PROFILE_1_CONSTANTS.items():
        if rec[k] != v:
            return "%s must be %s in profile 1, not %s" % (k, json.dumps(v), json.dumps(rec[k]))
    return None


def link_params(link):
    """The parameters that govern a window: the pilot's defaults, or the announcement's."""
    p = dict(PILOT_LINK)
    if link:
        for k in LINK_PARAMS:
            p[k] = link[k]
    return p


def link_in_force(links, payer, hop, t_first):
    """The announcement in force for a window (spec, Section 7), or None for the pilot defaults.

    Of the hop's announcements for this payer or for `*` with since at or
    before t_first: the newest since; of two with the same since, the one
    for this payer; of two still tied, the lexically greater hash.
    """
    best = None
    for rec in links:
        if rec["relay"] != hop or rec["payer"] not in (payer, "*") or rec["since"] > t_first:
            continue
        key = (rec["since"], 1 if rec["payer"] == payer else 0, link_hash(rec))
        if best is None or key > best[0]:
            best = (key, rec)
    return best[1] if best else None


# ---------------------------------------------------------------- purchases (spec, Section 15)

def is_place(x):
    return isinstance(x, str) and not x.startswith("ed25519:") and bool(TOKEN_RE.match(x))


def purchase_body(rec):
    return {k: v for k, v in rec.items() if k not in ("id", "sig")}


def purchase_id(body):
    """A purchase's id: the SHA-256 of the canonical JSON of the record without id and sig."""
    return hashlib.sha256(canon_json(body).encode()).hexdigest()


def sign_purchase(priv, body):
    return b64u(priv.sign(PURCHASE_DOMAIN + canon_json(body).encode()))


def verify_purchase(rec):
    """True when rec carries its poster's signature."""
    try:
        pub_of_nym(rec["poster"]).verify(b64u_dec(rec["sig"]), PURCHASE_DOMAIN + canon_json(purchase_body(rec)).encode())
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


def check_purchase(rec):
    """Raise ValueError unless rec has exactly a purchase's fields with sane types and its own id. Signature not checked."""
    if not isinstance(rec, dict) or set(rec) != set(PURCHASE_FIELDS):
        raise ValueError("a purchase has exactly the fields %s" % ", ".join(PURCHASE_FIELDS))
    if rec["kind"] != "purchase":
        raise ValueError("kind must be purchase")
    check_proto(rec)
    pub_of_nym(rec["poster"])
    if rec["program"] not in PROGRAMS:
        raise ValueError("program must be one of %s" % ", ".join(PROGRAMS))
    route = rec["route"]
    if not isinstance(route, dict) or set(route) != {"from", "to", "not_via"}:
        raise ValueError("route has exactly from, to and not_via")
    for k in ("from", "to"):
        v = route[k]
        if v is None and k == "from":
            continue
        if not isinstance(v, str) or not (v.startswith("ed25519:") or is_place(v)):
            raise ValueError("route.%s is a NYM or a place id%s" % (k, ", or null" if k == "from" else ""))
        if v.startswith("ed25519:"):
            pub_of_nym(v)
    if not isinstance(route["not_via"], list) or not all(is_place(x) for x in route["not_via"]):
        raise ValueError("route.not_via is a list of place ids")
    for k in ("min_bytes", "deadline", "posted_at"):
        if not is_int(rec[k]) or rec[k] < 0:
            raise ValueError("%s must be a non-negative integer" % k)
    if not is_int(rec["windows"]) or rec["windows"] < 1:
        raise ValueError("windows must be a positive integer")
    for k in ("premium_per_gb", "escrow"):
        v = rec[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0 or v == float("inf"):
            raise ValueError("%s must be a non-negative number" % k)
    pv = rec["proving"]
    if pv is not None:
        if not isinstance(pv, dict) or set(pv) != {"windows_per_period", "period_s"} or \
                not all(is_int(pv[k]) and pv[k] >= 0 for k in pv):
            raise ValueError("proving is null or {windows_per_period, period_s}")
    if rec["unit"] != "USD":
        raise ValueError("unit must be USD in profile 1")
    m = rec["matches"]
    if m is not None and not (isinstance(m, str) and len(m) == 64 and all(c in "0123456789abcdef" for c in m)):
        raise ValueError("matches is null or a purchase id")
    if (rec["program"] == "match") != (m is not None):
        raise ValueError("a match names the purchase it matches, and only a match does")
    if rec["id"] != purchase_id(purchase_body(rec)):
        raise ValueError("id is not the hash of the record")
    if not isinstance(rec["sig"], str) or len(b64u_dec(rec["sig"])) != 64:
        raise ValueError("sig is not an ed25519 signature")


def adjust_body(rec):
    return {k: v for k, v in rec.items() if k not in ("id", "sig")}


def adjust_domain(kind):
    return TOPUP_DOMAIN if kind == "top-up" else CLOSE_DOMAIN


def sign_adjust(priv, body):
    return b64u(priv.sign(adjust_domain(body["kind"]) + canon_json(body).encode()))


def verify_adjust(rec):
    """True when a top-up or close carries its poster's signature."""
    try:
        pub_of_nym(rec["poster"]).verify(b64u_dec(rec["sig"]), adjust_domain(rec["kind"]) + canon_json(adjust_body(rec)).encode())
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


def check_adjust(rec):
    """Raise ValueError unless rec is a well-formed top-up or close (spec, Section 15). Signature not checked here."""
    if not isinstance(rec, dict) or rec.get("kind") not in ("top-up", "close"):
        raise ValueError("kind must be top-up or close")
    fields = TOPUP_FIELDS if rec["kind"] == "top-up" else CLOSE_FIELDS
    if set(rec) != set(fields):
        raise ValueError("a %s has exactly the fields %s" % (rec["kind"], ", ".join(fields)))
    check_proto(rec)
    pub_of_nym(rec["poster"])
    pid = rec["purchase"]
    if not (isinstance(pid, str) and len(pid) == 64 and all(c in "0123456789abcdef" for c in pid)):
        raise ValueError("purchase is a purchase id")
    if not is_int(rec["posted_at"]) or rec["posted_at"] < 0:
        raise ValueError("posted_at must be a non-negative integer")
    if rec["kind"] == "top-up":
        v = rec["escrow"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v <= 0 or v == float("inf"):
            raise ValueError("escrow must be a positive number")
    if rec["id"] != purchase_id(adjust_body(rec)):
        raise ValueError("id is not the hash of the record")
    if not isinstance(rec["sig"], str) or len(b64u_dec(rec["sig"])) != 64:
        raise ValueError("sig is not an ed25519 signature")


class PurchaseBook:
    """The purchases a Mint holds and what the settlements have drawn from them, kept in settlement order.

    Pure bookkeeping shared by the Mint and the rebuild: which purchase a
    window settles against, the pools it draws from, the per-payer cap.
    `place_of` maps a NYM to its place in the map (or None); f and margin
    are the domain's.
    """

    def __init__(self, place_of=None, f=DEFAULT_STEWARD_FEE, margin=DEFAULT_STEWARD["margin"]):
        self.place_of = place_of or (lambda nym: None)
        self.f, self.margin = f, margin
        self.purchases, self.order = {}, []
        self.topups, self.closed = {}, {}     # purchase id -> [(posted_at, escrow)] of its top-ups; purchase id -> when its poster closed it
        self.records = []                     # every record in registration order: purchases, top-ups and closes
        self.ids = set()
        self.use = {}                         # purchase id -> {"windows", "own", "steward", "refund", "held"}
        self.fees_in, self.paid_on = {}, {}   # payer NYM -> gross amounts paid for carriage; what the Steward paid out on it

    def add(self, rec):
        """A purchase, a top-up or a close, in registration order. Returns False when its id is already held."""
        if rec["id"] in self.ids:
            return False
        self.ids.add(rec["id"])
        self.records.append(rec)
        if rec["kind"] == "purchase":
            self.purchases[rec["id"]] = rec
            self.order.append(rec["id"])
        elif rec["kind"] == "top-up":
            self.topups.setdefault(rec["purchase"], []).append((rec["posted_at"], rec["escrow"]))
        elif rec["kind"] == "close":
            self.closed.setdefault(rec["purchase"], rec["posted_at"])
        return True

    def total_escrow(self, rec, at=None):
        """A purchase's escrow with its top-ups: all of them, or, for a window ending at `at`, those posted by then.

        A top-up counts for the windows ending at or after it was posted,
        as a purchase and a close do, so the Mint and a rebuild draw the same
        pool for every window.
        """
        return round(rec["escrow"] + sum(e for t, e in self.topups.get(rec["id"], ()) if at is None or t <= at), 6)

    def topups_total(self, rec):
        return round(float(sum(e for _, e in self.topups.get(rec["id"], ()))), 6)

    def note(self, s):
        """A settlement happened: count the payer's fees in and what it drew from a purchase."""
        self.fees_in[s["payer"]] = round(self.fees_in.get(s["payer"], 0.0) + s["amount_usd"], 6)
        pid = s.get("purchase")
        if not pid:
            return
        u = self.use.setdefault(pid, {"windows": 0, "own": 0.0, "steward": 0.0, "refund": 0.0, "held": 0.0})
        u["windows"] += 1
        u["own"] = round(u["own"] + s["from_poster_usd"], 6)
        u["refund"] = round(u["refund"] + s["refund_usd"], 6)
        target = s.get("match") or pid
        if s["from_steward_usd"] or s["steward_held_usd"]:
            mu = self.use.setdefault(target, {"windows": 0, "own": 0.0, "steward": 0.0, "refund": 0.0, "held": 0.0})
            mu["steward"] = round(mu["steward"] + s["from_steward_usd"], 6)
            mu["held"] = round(mu["held"] + s["steward_held_usd"], 6)
        self.paid_on[s["payer"]] = round(self.paid_on.get(s["payer"], 0.0) + s["from_steward_usd"], 6)

    def pool(self, rec, at=None):
        """What is left of a purchase's escrow (top-ups posted by `at` included): the poster's own pool, or the Steward's."""
        u = self.use.get(rec["id"], {})
        return round(self.total_escrow(rec, at) - u.get("own", 0.0) - u.get("steward", 0.0), 6)

    def closed_for(self, rec, t_last):
        """Has the poster closed this purchase before this window ended? A close governs the windows ending at or after it."""
        c = self.closed.get(rec["id"])
        return c is not None and t_last >= c

    def match_for(self, pid):
        for mid in self.order:
            m = self.purchases[mid]
            if m["program"] == "match" and m["matches"] == pid:
                return m
        return None

    def applies(self, rec, r, t_last):
        """Does this purchase govern a window (payer, hop, t_first, t_last)? Programs other than match only.

        A purchase is open from posted_at: it governs the windows that end at
        or after it was posted (the window running when it was posted
        included) and at or before its deadline.
        """
        if rec["program"] == "match" or rec["posted_at"] > t_last or t_last > rec["deadline"] or self.closed_for(rec, t_last):
            return False
        if self.use.get(rec["id"], {}).get("windows", 0) >= rec["windows"]:
            return False
        route = rec["route"]
        hop_place, payer_place = self.place_of(r["hop"]), self.place_of(r["payer"])
        if rec["program"] == "payer":
            return rec["poster"] == r["payer"] and route["from"] in (None, r["payer"]) and \
                (route["to"] == r["hop"] or (is_place(route["to"]) and hop_place == route["to"]))
        if rec["program"] == "first_link":
            return route["from"] is None and hop_place is not None and hop_place == route["to"]
        if rec["program"] == "second_bridge":
            return payer_place is not None and payer_place == route["from"] and \
                hop_place is not None and hop_place not in route["not_via"] and hop_place != route["from"]
        return False

    def context(self, r, t_last):
        """The purchase a window settles against and the pools it draws from, for resolve(); None when none applies.

        The payer's own purchases come first, then the Steward's, each by
        posting time; a purchase whose pools are empty is passed over.
        """
        cands = [self.purchases[pid] for pid in self.order if self.applies(self.purchases[pid], r, t_last)]
        cands.sort(key=lambda rec: (0 if rec["poster"] == r["payer"] else 1, rec["posted_at"], rec["id"]))
        for rec in cands:
            match = self.match_for(rec["id"]) if rec["program"] == "payer" else None
            if rec["program"] == "payer":
                own, steward = self.pool(rec, t_last), (self.pool(match, t_last) if match else 0.0)
            else:
                own, steward = 0.0, self.pool(rec, t_last)
            if own <= 0 and steward <= 0:
                continue
            allowed = rules.allowance(self.f, self.margin, self.fees_in.get(r["payer"], 0.0), self.paid_on.get(r["payer"], 0.0)) if rules else 0.0
            return {"rec": rec, "match": match, "pool_own": max(0.0, own), "pool_steward": max(0.0, steward), "allowance": allowed}
        return None


def resolve(receipt_env, ack_env, price_usd_per_gb=DEFAULT_PRICE_USD_PER_GB, now=None, link=None, fee=DEFAULT_STEWARD_FEE, purchase=None):
    """Settle one window from a payer's receipt and a hop's ack (spec, Section 8). Pure: needs nothing else.

    link is the announcement in force at the window's start, or None for the
    pilot defaults with price_usd_per_gb; fee is the Steward's fraction f;
    purchase, when a purchase applies, is PurchaseBook.context(): the record
    and the pools the premium is drawn from (spec, Section 15). Raises
    ValueError when the two don't describe the same window or a signature
    fails. Returns the settlement record.
    """
    if check_envelope(receipt_env) is None or check_envelope(ack_env) is None:
        raise ValueError("unreachable")
    if receipt_env["kind"] != "receipt" or ack_env["kind"] != "ack":
        raise ValueError("need a receipt and an ack")
    r, a = receipt_env["receipt"], ack_env["receipt"]
    if a["prev_sig"] != receipt_env["sig"]:
        raise ValueError("the ack does not chain to this receipt")
    for f in FIELDS:
        if f not in ("bytes", "prev_sig") and a[f] != r[f]:
            raise ValueError("the ack changed %s" % f)
    p = link_params(link)
    price = p["price"] if link else price_usd_per_gb
    x, y = r["bytes"], a["bytes"]
    settled = min(x, y)
    agree = abs(x - y) <= max(p["min_bytes"], p["margin"] * max(x, y))
    amount = round(settled / 1e9 * price, 6)
    extra, premium = {}, 0.0
    if purchase:
        if rules is None:
            raise ValueError("steward_rules.py is not installed beside bwkit; a purchase cannot be settled")
        rec = purchase["rec"]
        due, refund = rules.premium_split(settled, rec["min_bytes"], rec["premium_per_gb"])
        own, steward, held = rules.draw(due, purchase["pool_own"], purchase["pool_steward"], purchase["allowance"])
        own, steward, held = round(float(own), 6), round(float(steward), 6), round(float(held), 6)   # floats, never the integer 0
        premium = round(own + steward, 6)
        extra = {"purchase": rec["id"], "match": purchase["match"]["id"] if purchase.get("match") else None,
                 "premium_usd": premium, "refund_usd": round(float(refund), 6),
                 "from_poster_usd": own, "from_steward_usd": steward, "steward_held_usd": held}
    out = {
        "kind": "settlement", "proto": PROTO,
        "t": int(now if now is not None else time.time()),
        "payer": r["payer"], "hop": r["hop"], "hop_index": r["hop_index"],
        "t_first": r["t_first"], "t_last": r["t_last"],
        "payer_bytes": x, "hop_bytes": y, "settled_bytes": settled,
        "status": "settled" if agree else "disputed",
        "rule": "the smaller of the two signed counts; neither party is bound beyond what it signed",
        "receipt_sig": receipt_env["sig"], "ack_sig": ack_env["sig"],
        "price_usd_per_gb": price,
        "amount_usd": amount,
        "steward_fee": fee, "fee_usd": round((amount + premium) * fee, 6),   # f is taken from the premium as from any fee
        "margin": p["margin"], "min_bytes": p["min_bytes"],
        "link": link_hash(link) if link else None,
    }
    out.update(extra)
    return out


def legacy_shape(s):
    """A settlement as 0.2.0 wrote it: without the profile-1 members. Such a record is profile 1 under the pilot defaults."""
    return {k: v for k, v in s.items() if k not in SETTLEMENT_PROFILE_KEYS}


# ---------------------------------------------------------------- commitments

def commit_hour(settlement):
    """The hour a settlement is committed under: the Unix hour its window started in."""
    return settlement["t_first"] - settlement["t_first"] % 3600


def leaf_of(settlement):
    """A leaf: SHA-256 over the canonical JSON of the settlement entry, without `t`.

    `t` is the Mint's own clock, which nobody else can know. Everything else
    in the entry follows from the two signed receipts and the announcement,
    so a ledger rebuilt from the parties' copies makes the same leaf.
    """
    body = {k: v for k, v in settlement.items() if k not in LEAF_SKIP}
    return hashlib.sha256(LEAF_PREFIX + canon_json(body).encode()).digest()


def node_of(left, right):
    return hashlib.sha256(NODE_PREFIX + left + right).digest()


def _split(n):
    """RFC 6962's split: the largest power of two below n."""
    k = 1
    while k * 2 < n:
        k *= 2
    return k


def merkle_root(leaves):
    """The root over leaves (bytes), in the order given; one leaf's root is that leaf."""
    if not leaves:
        raise ValueError("no leaves")
    if len(leaves) == 1:
        return leaves[0]
    k = _split(len(leaves))
    return node_of(merkle_root(leaves[:k]), merkle_root(leaves[k:]))


def merkle_path(leaves, i):
    """The siblings from leaf i up to the root, [[side, hex], ...]: 'L' when the sibling is on the left."""
    if len(leaves) == 1:
        return []
    k = _split(len(leaves))
    if i < k:
        return merkle_path(leaves[:k], i) + [["R", merkle_root(leaves[k:]).hex()]]
    return merkle_path(leaves[k:], i - k) + [["L", merkle_root(leaves[:k]).hex()]]


def merkle_check(leaf, path, root_hex):
    """True when leaf (bytes) climbs this path to the root."""
    h = leaf
    try:
        for side, sib in path:
            sib = bytes.fromhex(sib)
            if side == "L":
                h = node_of(sib, h)
            elif side == "R":
                h = node_of(h, sib)
            else:
                return False
    except (ValueError, TypeError):
        return False
    return h.hex() == root_hex


def settlement_order(s):
    """Leaves go by window start, then the rest of the window, so every party builds the same tree."""
    return (s["t_first"], s["t_last"], s["payer"], s["hop"], s["hop_index"], s["receipt_sig"])


def hour_leaves(settlements):
    """An hour's settlements in leaf order, and their leaves."""
    ss = sorted(settlements, key=settlement_order)
    return ss, [leaf_of(s) for s in ss]


def commitment_canon(mint, window, root, count):
    """The signed bytes of a commitment: the Mint's NYM, the hour, the root and the count."""
    return COMMIT_DOMAIN + json.dumps([mint, window, root, count], separators=(",", ":"), ensure_ascii=True).encode()


def verify_commitment(c):
    """True when c carries its Mint's signature over (window, root, count)."""
    try:
        pub_of_nym(c["mint"]).verify(b64u_dec(c["signature"]), commitment_canon(c["mint"], c["window"], c["root"], c["count"]))
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


def make_commitment(priv, settlements, window, now):
    """The commitment entry for an hour: the Merkle root over its settlements, signed when priv is given."""
    ss, leaves = hour_leaves(settlements)
    root = merkle_root(leaves).hex()
    mint = nym_of_pub(priv.public_key()) if priv else None
    return {"kind": "commitment", "proto": PROTO, "t": int(now), "mint": mint, "window": window, "root": root,
            "count": len(ss), "signature": b64u(priv.sign(commitment_canon(mint, window, root, len(ss)))) if priv else None}


def make_proof(commitment, settlements):
    """Inclusion proofs for every settlement of the commitment's hour. Raises ValueError when they don't give its root."""
    ss, leaves = hour_leaves(settlements)
    root = merkle_root(leaves).hex()
    if root != commitment["root"] or len(ss) != commitment["count"]:
        raise ValueError("the hour's %d settlements give root %s; the commitment is over %d with root %s" % (
            len(ss), root, commitment["count"], commitment["root"]))
    return {"kind": "proof", "proto": PROTO, "impl": IMPL, "mint": commitment["mint"], "window": commitment["window"],
            "root": root, "count": len(ss), "signature": commitment["signature"],
            "settlements": [{"index": i, "leaf": leaves[i].hex(), "path": merkle_path(leaves, i), "settlement": s}
                            for i, s in enumerate(ss)]}


def check_proof(doc, mint=None):
    """Check a proof with nothing but what it carries. Returns (ok, lines): the commitment's line, then one per settlement.

    mint, when given, is the NYM the commitment must be signed by.
    """
    try:
        if doc.get("kind") != "proof":
            raise ValueError("not a proof")
        check_proto(doc)
        c = {k: doc[k] for k in ("mint", "window", "root", "count", "signature")}
        items = doc["settlements"]
        if not isinstance(items, list):
            raise ValueError("settlements must be a list")
    except (KeyError, ValueError, AttributeError, TypeError) as e:
        return False, ["BAD: %s" % (e or "malformed proof")]
    ok, lines = True, []
    if mint and c["mint"] != mint:
        ok = False
        lines.append("BAD: the commitment is signed by %s, not %s" % (c["mint"], mint))
    signed = verify_commitment(c)
    ok = ok and signed
    lines.append("commitment for hour %s by %s: root %s over %s settlements, signature %s" % (
        c["window"], c["mint"], c["root"], c["count"], "ok" if signed else "BAD"))
    for item in items:
        try:
            s = item["settlement"]
            if s.get("kind") != "settlement":
                raise ValueError("not a settlement")
            where = "window %d..%d, %d bytes %s" % (s["t_first"], s["t_last"], s["settled_bytes"], s["status"])
            if commit_hour(s) != c["window"]:
                raise ValueError("%s: belongs to hour %d, not %s" % (where, commit_hour(s), c["window"]))
            if not is_int(item["index"]) or not 0 <= item["index"] < c["count"]:
                raise ValueError("%s: index %r is outside the commitment's %s leaves" % (where, item.get("index"), c["count"]))
            if not merkle_check(leaf_of(s), item["path"], c["root"]):
                raise ValueError("%s: NOT in the commitment (its leaf does not reach the root)" % where)
            lines.append("%s: included, leaf %d of %d" % (where, item["index"], c["count"]))
        except (KeyError, ValueError, TypeError, AttributeError) as e:
            ok = False
            lines.append("BAD: %s" % e)
    return ok, lines


def parse_hour(text):
    """An hour from the command line: Unix seconds (any moment in it) or YYYY-MM-DDTHH[:MM] in local time."""
    if text.isdigit():
        t = int(text)
    else:
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H", "%Y-%m-%d %H:%M", "%Y-%m-%d %H"):
            try:
                t = int(time.mktime(time.strptime(text, fmt)))
                break
            except ValueError:
                continue
        else:
            raise SystemExit("an hour is Unix seconds or YYYY-MM-DDTHH (local time)")
    return t - t % 3600


# ---------------------------------------------------------------- the rebuild

def read_jsonl(path):
    """The entries of a .jsonl file as (line number, entry), with the lines that aren't JSON named apart."""
    entries, bad = [], []
    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append((i, json.loads(line)))
            except ValueError:
                bad.append("%s line %d: not JSON" % (os.path.basename(path), i))
    return entries, bad


def place_lookup(map_obj, peers):
    """A function NYM -> place id from the map's `nodes` (name -> place) through the peer list (NYM -> name)."""
    nodes = (map_obj or {}).get("nodes") or {}
    return lambda nym: nodes.get(peers.get(nym)) if peers else None


def rebuild(receipts, acks, links=(), price_usd_per_gb=DEFAULT_PRICE_USD_PER_GB, fee=DEFAULT_STEWARD_FEE, now=None,
            names=("receipts.jsonl", "acks.jsonl", "links.jsonl", "purchases.jsonl"), purchases=(), place_of=None,
            steward_margin=DEFAULT_STEWARD["margin"]):
    """A Mint's ledger from the payer's receipts, the hop's acks and the announcements alone (spec, Section 11).

    receipts, acks and links are lists of (line number, record), as
    read_jsonl gives them. Every signature is checked; what fails is left
    out and named in the report. Each receipt is paired with the ack that
    chains to it (prev_sig = the receipt's signature) and settled by
    resolve(), the Mint's own rule, under the announcement in force at the
    window's start, so a receipt the hop declined stays without an ack.
    Each hour's settlements get a commitment, unsigned, since only the Mint
    holds its key.

    purchases are the purchase records (spec, Section 15); a window that one
    governs settles against it, in the order the windows are rebuilt, with
    place_of (NYM -> place) for the Steward's place-routed purchases.

    Returns (entries, report): the ledger lines in the order a Mint would
    have written them, and the counts, with `bad` naming what was left out.
    """
    now = int(now if now is not None else time.time())
    bad, seen, good = [], set(), {"receipt": [], "ack": [], "link": []}
    book = PurchaseBook(place_of, fee, steward_margin)
    for i, rec in purchases:
        try:
            if isinstance(rec, dict) and rec.get("kind") in ("top-up", "close"):
                check_adjust(rec)
                if not verify_adjust(rec):
                    raise ValueError("bad signature")
                target = book.purchases.get(rec["purchase"])
                if target is None:
                    raise ValueError("%s names purchase %s, which is not held" % (rec["kind"], rec["purchase"][:16]))
                if target["poster"] != rec["poster"]:
                    raise ValueError("%s by %s on a purchase posted by another" % (rec["kind"], rec["poster"][:20]))
            else:
                check_purchase(rec)
                if not verify_purchase(rec):
                    raise ValueError("bad signature")
        except ValueError as e:
            bad.append("%s line %d: %s" % (names[3] if len(names) > 3 else "purchases.jsonl", i, e))
            continue
        book.add(rec)
    for i, rec in links:
        try:
            check_link(rec)
            if not verify_link(rec):
                raise ValueError("bad signature")
            why = profile_1_link(rec)
            if why:
                raise ValueError(why)
        except ValueError as e:
            bad.append("%s line %d: %s" % (names[2], i, e))
            continue
        if rec["sig"] not in seen:
            seen.add(rec["sig"])
            good["link"].append(rec)
    keys, acks_by_prev = {}, {}
    for name, rows, kind in ((names[0], receipts, "receipt"), (names[1], acks, "ack")):
        for i, env in rows:
            try:
                check_envelope(env)
                if env["kind"] != kind:
                    raise ValueError("a %s, not a %s" % (env["kind"], kind))
            except ValueError as e:
                bad.append("%s line %d: %s" % (name, i, e))
                continue
            if env["sig"] in seen:
                continue
            if kind == "receipt":
                k = window_key(env["receipt"])
                if k in keys:
                    bad.append("%s line %d: a second receipt for window %d..%d (replay); the first is kept" % (
                        name, i, env["receipt"]["t_first"], env["receipt"]["t_last"]))
                    continue
                keys[k] = env
            else:
                if env["receipt"]["prev_sig"] in acks_by_prev:
                    bad.append("%s line %d: a second ack for receipt %s (replay); the first is kept" % (
                        name, i, env["receipt"]["prev_sig"][:12]))
                    continue
                acks_by_prev[env["receipt"]["prev_sig"]] = env
            seen.add(env["sig"])
            good[kind].append(env)
    rep = {"receipts": len(good["receipt"]), "acks": len(good["ack"]), "links": len(good["link"]), "purchases": len(book.order),
           "settlements": 0, "disputed": 0, "hours": 0, "receipts_without_ack": 0, "acks_without_receipt": 0, "bad": bad}

    def entry(env):
        return {"kind": env["kind"], "received_at": now, "from": env.get("from") or env["kind"],
                "receipt": env["receipt"], "sig": env["sig"], "meter": env.get("meter")}

    entries, by_hour = [], {}
    for env in sorted(good["receipt"], key=lambda e: (e["receipt"]["t_last"], e["receipt"]["t_first"], e["sig"])):
        entries.append(entry(env))
        ack = acks_by_prev.pop(env["sig"], None)
        if ack is None:
            rep["receipts_without_ack"] += 1
            continue
        entries.append(entry(ack))
        r = env["receipt"]
        try:
            s = resolve(env, ack, price_usd_per_gb, now, link_in_force(good["link"], r["payer"], r["hop"], r["t_first"]), fee,
                        book.context(r, r["t_last"]))
        except ValueError as e:
            bad.append("%s %s does not settle receipt %s: %s" % (names[1], ack["sig"][:12], env["sig"][:12], e))
            continue
        book.note(s)
        entries.append(s)
        rep["settlements"] += 1
        rep["disputed"] += 1 if s["status"] == "disputed" else 0
        by_hour.setdefault(commit_hour(s), []).append(s)
    last = {}
    for i, e in enumerate(entries):
        if e["kind"] == "settlement":
            last[commit_hour(e)] = i
    out = []
    for i, e in enumerate(entries):
        out.append(e)
        if e["kind"] == "settlement" and last[commit_hour(e)] == i:
            out.append(make_commitment(None, by_hour[commit_hour(e)], commit_hour(e), now))
    for a in sorted(acks_by_prev.values(), key=lambda e: (e["receipt"]["t_last"], e["receipt"]["t_first"], e["sig"])):
        out.append(entry(a))
        rep["acks_without_receipt"] += 1
    rep["hours"] = len(by_hour)
    return out, rep


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def compare(rebuilt, mint_entries):
    """The rebuilt ledger against the Mint's. Returns (agree, verdict, lines).

    The verdict is one line: AGREE, or DIFFER with the windows and hours that
    do. The lines say how, one each, and note the hours the Mint has not
    committed to, which is not a difference. A settlement the Mint wrote
    before profile 1 (no `proto`) is compared in that form: the rebuilt one
    must then carry the pilot defaults and no announcement, and its leaf
    for the hour's root is taken over the pre-profile members.
    """
    def settlements(entries):
        return {e["receipt_sig"]: e for e in entries if e.get("kind") == "settlement"}

    def commitments(entries):
        out = {}
        for e in entries:
            if e.get("kind") == "commitment":
                out[e["window"]] = e           # the last one for an hour is the Mint's current word
        return out

    def where(s):
        return "%d..%d" % (s["t_first"], s["t_last"])

    mine, theirs = settlements(rebuilt), settlements(mint_entries)
    their_c = commitments(mint_entries)
    windows, hours, lines, notes, pre, my_leaf = [], set(), [], [], 0, {}
    for sig, s in sorted(mine.items(), key=lambda kv: settlement_order(kv[1])):
        t = theirs.get(sig)
        if t is None:
            windows.append(where(s))
            lines.append("window %s: settled from the parties' copies, not on the Mint's ledger" % where(s))
            my_leaf[sig] = leaf_of(s)
            continue
        a = s
        if "proto" not in t:                   # written before profile 1: profile 1 under the defaults, no fee
            pre += 1
            a = legacy_shape(s)
            if s.get("link") is not None or s.get("margin") != PILOT_LINK["margin"] or s.get("min_bytes") != PILOT_LINK["min_bytes"]:
                windows.append(where(s))
                lines.append("window %s: settled under announcement %s here; the Mint settled it before profile 1, under the defaults" % (
                    where(s), (s.get("link") or "?")[:16]))
        my_leaf[sig] = leaf_of(a)
        if leaf_of(a) != leaf_of(t):
            windows.append(where(s))
            diffs = ["%s %r here, %r on the Mint" % (k, a.get(k), t.get(k))
                     for k in sorted(set(a) | set(t)) if k not in LEAF_SKIP and a.get(k) != t.get(k)]
            lines.append("window %s: %s" % (where(s), "; ".join(diffs)))
    for sig, t in sorted(theirs.items(), key=lambda kv: settlement_order(kv[1])):
        if sig not in mine:
            windows.append(where(t))
            lines.append("window %s: on the Mint's ledger, not in the parties' copies" % where(t))
    my_hours, their_hours = {}, {}
    for s in mine.values():
        my_hours.setdefault(commit_hour(s), []).append(s)
    for t in theirs.values():
        their_hours.setdefault(commit_hour(t), []).append(t)
    committed = 0
    for h in sorted(set(my_hours) | set(their_c)):
        ss = sorted(my_hours.get(h, []), key=settlement_order)
        m = (merkle_root([my_leaf[s["receipt_sig"]] for s in ss]).hex(), len(ss)) if ss else None
        c = their_c.get(h)
        if c is None:
            notes.append("hour %d: no commitment on the Mint's ledger (%s rebuilt)" % (h, plural(m[1], "settlement")))
            continue
        committed += 1 if m else 0
        if not verify_commitment(c):
            hours.add(h)
            lines.append("hour %d: the Mint's commitment is not signed by %s" % (h, c.get("mint")))
        if m is None:
            hours.add(h)
            lines.append("hour %d: the Mint committed to %s; the parties' copies have none in that hour" % (h, plural(c["count"], "settlement")))
        elif m != (c["root"], c["count"]):
            hours.add(h)
            lines.append("hour %d: root %s over %d here, %s over %d on the Mint" % (h, m[0][:16], m[1], c["root"][:16], c["count"]))
        own = their_hours.get(h, [])
        if not own or merkle_root(hour_leaves(own)[1]).hex() != c["root"] or len(own) != c["count"]:
            hours.add(h)
            lines.append("hour %d: the Mint's own ledger no longer matches the commitment it signed" % h)
    agree = not windows and not hours
    if agree:
        verdict = "AGREE: %d settlements in %d hours rebuilt from the parties' copies match the Mint's ledger and its %d commitments" % (
            len(mine), len(my_hours), committed)
        if pre:
            verdict += " (%d settled before profile 1, compared in that form)" % pre
        if notes:
            verdict += " (%d hours have no commitment yet)" % len(notes)
    else:
        what = []
        if windows:
            what.append("window%s %s" % ("s" if len(windows) != 1 else "", ", ".join(dict.fromkeys(windows))))
        if hours:
            what.append("hour%s %s" % ("s" if len(hours) != 1 else "", ", ".join(str(h) for h in sorted(hours))))
        verdict = "DIFFER in " + " and ".join(what)
    return agree, verdict, lines + notes


# ---------------------------------------------------------------- http

USER_AGENT = IMPL   # Cloudflare's edge in front of the status page refuses Python's default (error 1010)


def http_post(url, body, timeout=HTTP_TIMEOUT_S, headers=None):
    data = json.dumps(body).encode()
    h = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    h.update(headers or {})
    req = urlrequest.Request(url, data=data, headers=h, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urlerror.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "null")
        except ValueError:
            return e.code, {"error": "non-JSON error body"}


def http_get(url, timeout=HTTP_TIMEOUT_S):
    req = urlrequest.Request(url, headers={"User-Agent": USER_AGENT})
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode()


class Decline(Exception):
    """A request refused for a protocol reason (HTTP 409; 403 when the message starts with `unknown`)."""


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


# ---------------------------------------------------------------- node

class NoBaseline(Exception):
    pass


class Node:
    def __init__(self, directory, config):
        self.dir = directory
        self.config = config
        self.role = config.get("role")
        if self.role not in ("payer", "hop", "mint"):
            raise ValueError("config role must be payer, hop or mint")
        self.name = config.get("name") or socket.gethostname()
        os.makedirs(directory, exist_ok=True)
        os.makedirs(os.path.join(directory, "outbox"), exist_ok=True)
        os.makedirs(os.path.join(directory, "sent"), exist_ok=True)
        self.lock = threading.Lock()
        self.priv = None
        kp = self.path("key.priv")
        if os.path.exists(kp):
            with open(kp, "rb") as f:
                self.priv = Ed25519PrivateKey.from_private_bytes(f.read())
        self.index = None

    @classmethod
    def from_env(cls):
        directory = os.environ.get("BWKIT_DIR", "/var/lib/bwkit")
        cfg = os.environ.get("BWKIT_CONFIG", "/etc/bwkit/config.json")
        with open(cfg) as f:
            return cls(directory, json.load(f))

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    # keys and peers

    def keygen(self):
        kp = self.path("key.priv")
        if os.path.exists(kp):
            raise SystemExit("%s exists; not overwriting a node's identity" % kp)
        priv = Ed25519PrivateKey.generate()
        raw = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())
        fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        self.priv = priv
        return self.nym

    @property
    def nym(self):
        if self.priv is None:
            raise SystemExit("no key yet: run `bwkit keygen`")
        return nym_of_pub(self.priv.public_key())

    def peers(self):
        p = self.path("peers.json")
        if not os.path.exists(p):
            return {}
        with open(p) as f:
            return json.load(f)

    def peer_add(self, name, nym):
        pub_of_nym(nym)
        peers = self.peers()
        peers[nym] = name
        self.write_json("peers.json", peers)

    def nym_by_name(self, name):
        for nym, n in self.peers().items():
            if n == name:
                return nym
        raise SystemExit("no peer named %s in peers.json; run `bwkit peer add %s <nym>`" % (name, name))

    # small files

    def write_json(self, name, obj):
        tmp = self.path(name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path(name))

    def state(self):
        p = self.path("state.json")
        if not os.path.exists(p):
            return {}
        with open(p) as f:
            return json.load(f)

    def append(self, name, obj):
        with open(self.path(name), "a") as f:
            f.write(json.dumps(obj, sort_keys=True) + "\n")

    def read_lines(self, name, skip_partial=False):
        """The records of one of this node's .jsonl files; with skip_partial, a half-written last line is skipped."""
        p = self.path(name)
        if not os.path.exists(p):
            return []
        with open(p) as f:
            lines = f.read().split("\n")
        out = []
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                if skip_partial and i == len(lines) - 1:
                    break
                raise
        return out

    # the meter

    def read_counters(self):
        iface = self.config.get("iface", "wg0")
        if iface.startswith("file:"):
            with open(iface[5:]) as f:
                d = json.load(f)
            return int(d["rx"]), int(d["tx"])
        base = "/sys/class/net/%s/statistics/" % iface
        with open(base + "rx_bytes") as f:
            rx = int(f.read())
        with open(base + "tx_bytes") as f:
            tx = int(f.read())
        return rx, tx

    def sample(self, now=None):
        """Take the baseline: the window after this starts here."""
        now = int(now if now is not None else time.time())
        rx, tx = self.read_counters()
        st = self.state()
        st["sample"] = {"t": now, "rx": rx, "tx": tx}
        self.write_json("state.json", st)
        return st["sample"]

    def window(self, now):
        """Bytes through the interface since the last sample. Does not move the sample."""
        rx, tx = self.read_counters()
        prev = self.state().get("sample")
        if prev is None:
            raise NoBaseline()
        reset = rx < prev["rx"] or tx < prev["tx"]
        drx = rx if reset else rx - prev["rx"]
        dtx = tx if reset else tx - prev["tx"]
        meter = {"t0": prev["t"], "rx0": prev["rx"], "tx0": prev["tx"],
                 "t1": now, "rx1": rx, "tx1": tx, "reset": reset}
        return prev["t"], drx + dtx, meter

    def commit_sample(self, t, meter):
        st = self.state()
        st["sample"] = {"t": t, "rx": meter["rx1"], "tx": meter["tx1"]}
        self.write_json("state.json", st)

    # outbox: everything meant for the Mint is written down before it is sent

    def outbox_put(self, env):
        # receipts sort before acks of the same window, so the Mint sees them in that order
        name = "%d-%s-%s-%s.json" % (env["receipt"]["t_last"], "0" if env["kind"] == "receipt" else "1",
                                     env["kind"], env["sig"][:12])
        with open(self.path("outbox", name), "w") as f:
            json.dump(env, f, sort_keys=True)
        return name

    def flush(self):
        """Send everything in the outbox to the Mint. Returns (sent, left)."""
        mint = self.config.get("mint")
        if not mint:
            raise SystemExit("config has no mint URL")
        with self.lock:
            return self._flush(mint)

    def _flush(self, mint):
        names = sorted(os.listdir(self.path("outbox")))
        sent = 0
        for name in names:
            p = self.path("outbox", name)
            try:
                with open(p) as f:
                    env = json.load(f)
            except FileNotFoundError:
                continue
            try:
                status, body = http_post(mint.rstrip("/") + "/receipt", env)
            except (urlerror.URLError, socket.timeout, OSError) as e:
                log("mint unreachable (%s); %d left in outbox" % (e, len(names) - sent))
                break
            if 200 <= status < 300:
                os.replace(p, self.path("sent", name))
                self.append("sent.jsonl", {"t": int(time.time()), "file": name, "mint": body})
                sent += 1
            elif status == 409 and isinstance(body, dict) and body.get("error") == "duplicate":
                os.replace(p, self.path("sent", name))
                sent += 1
            else:
                log("mint refused %s: %s %s" % (name, status, body))
                os.replace(p, self.path("sent", name + ".refused"))
        return sent, len(os.listdir(self.path("outbox")))

    def flush_quietly(self):
        """A background flush: never raises, logs what it did."""
        try:
            if os.listdir(self.path("outbox")):
                sent, left = self.flush()
                log("outbox: sent %d, %d left" % (sent, left))
        except (OSError, SystemExit) as e:
            log("outbox flush failed: %s" % e)

    def flush_later(self, delay):
        t = threading.Timer(delay, self.flush_quietly)
        t.daemon = True
        t.start()

    # payer

    def make_receipt(self, now=None):
        now = int(now if now is not None else time.time())
        hop_nym = self.nym_by_name(self.config["hop_name"])
        t_first, nbytes, meter = self.window(now)
        r = {"payer": self.nym, "hop": hop_nym, "hop_index": 1,
             "merkle_root": meter_root(self.nym, meter), "bytes": nbytes,
             "t_first": t_first, "t_last": now, "prev_sig": ""}
        return {"kind": "receipt", "proto": PROTO, "from": self.name, "impl": IMPL,
                "receipt": r, "sig": sign_receipt(self.priv, r), "meter": meter}

    def receipt_run(self, now=None):
        """The hourly job: receipt, ack, both to the Mint. Returns a summary dict."""
        if self.role != "payer":
            raise SystemExit("only the payer makes receipts")
        try:
            env = self.make_receipt(now)
        except NoBaseline:
            s = self.sample(now)
            log("no baseline yet; took one at %d (rx %d, tx %d). The first receipt comes next hour." % (s["t"], s["rx"], s["tx"]))
            return {"baseline": s}
        r = env["receipt"]
        log("receipt for %d..%d: %d bytes, sig %s" % (r["t_first"], r["t_last"], r["bytes"], env["sig"][:12]))
        self.commit_sample(r["t_last"], env["meter"])
        self.append("receipts.jsonl", env)
        self.outbox_put(env)
        ack = None
        hop_url = self.config.get("hop_url")
        if hop_url:
            try:
                status, body = http_post(hop_url.rstrip("/") + "/ack", env)
            except (urlerror.URLError, socket.timeout, OSError) as e:
                log("hop unreachable: %s" % e)
                status, body = None, None
            if status == 200:
                try:
                    signer = check_envelope(body)
                    if body["kind"] != "ack" or signer != r["hop"] or body["receipt"]["prev_sig"] != env["sig"]:
                        raise ValueError("ack is not the hop's ack of this receipt")
                    ack = body
                    log("ack from %s: %d bytes%s" % (body.get("from"), ack["receipt"]["bytes"],
                        "" if ack["receipt"]["bytes"] == r["bytes"] else " (differs from ours: %d)" % r["bytes"]))
                    self.append("acks.jsonl", ack)
                    self.outbox_put(ack)
                except ValueError as e:
                    log("hop returned a bad ack: %s" % e)
            elif status is not None:
                log("hop declined: %s %s" % (status, body))
        sent, left = self.flush()
        log("sent %d to the Mint, %d left in outbox" % (sent, left))
        return {"receipt": env, "ack": ack, "sent": sent, "left": left}

    def purchase_post(self, to, min_bytes, premium_per_gb, windows, escrow, deadline, now=None, post=True, poster_priv=None,
                      program="payer", route_from="self", not_via=(), matches=None, mint_url=None, keep=True):
        """Sign a purchase (spec, Section 15) and register it with the Mint. Returns (record, id).

        to is a peer name, a NYM or a place id; route_from "self" is this
        node's NYM, None is anywhere, else a place id. The Steward passes its
        own key and program.
        """
        now = int(now if now is not None else time.time())
        priv = poster_priv or self.priv
        poster = nym_of_pub(priv.public_key())
        if isinstance(to, str) and not to.startswith("ed25519:") and not is_place(to):
            to = self.nym_by_name(to)
        elif isinstance(to, str) and is_place(to) and to in {n for n in self.peers().values()}:
            to = self.nym_by_name(to)
        body = {"kind": "purchase", "proto": PROTO, "poster": poster, "program": program,
                "route": {"from": poster if route_from == "self" else route_from, "to": to, "not_via": list(not_via)},
                "min_bytes": int(min_bytes), "premium_per_gb": float(premium_per_gb), "windows": int(windows), "deadline": int(deadline),
                "proving": None, "escrow": float(escrow), "unit": "USD", "posted_at": now, "matches": matches}
        rec = dict(body, id=purchase_id(body), sig=sign_purchase(priv, body))
        check_purchase(rec)
        if post:
            mint = (mint_url or self.config.get("mint") or "").rstrip("/")
            if not mint:
                raise SystemExit("config has no mint URL")
            try:
                status, ans = http_post(mint + "/purchase", rec)
            except (urlerror.URLError, socket.timeout, OSError) as e:
                raise SystemExit("mint unreachable: %s" % e)
            if not 200 <= status < 300:
                raise SystemExit("the Mint refused the purchase: %s %s" % (status, json.dumps(ans)))
            if keep:
                self.append("purchases.jsonl", rec)
            log("posted purchase %s to the Mint: %s -> %s, >= %d bytes a window, premium %s/GB, %d windows, escrow %s" % (
                rec["id"][:16], rec["route"]["from"], rec["route"]["to"], rec["min_bytes"], rec["premium_per_gb"], rec["windows"], rec["escrow"]))
        return rec, rec["id"]

    def purchase_adjust(self, kind, pid, escrow=None, now=None, post=True, poster_priv=None, mint_url=None, keep=True):
        """Sign a top-up (escrow added) or a close on a purchase and register it with the Mint. Returns (record, id)."""
        now = int(now if now is not None else time.time())
        priv = poster_priv or self.priv
        body = {"kind": kind, "proto": PROTO, "poster": nym_of_pub(priv.public_key()), "purchase": pid, "posted_at": now}
        if kind == "top-up":
            body["escrow"] = float(escrow)
        rec = dict(body, id=purchase_id(body), sig=sign_adjust(priv, body))
        check_adjust(rec)
        if post:
            mint = (mint_url or self.config.get("mint") or "").rstrip("/")
            if not mint:
                raise SystemExit("config has no mint URL")
            try:
                status, ans = http_post(mint + "/purchase", rec)
            except (urlerror.URLError, socket.timeout, OSError) as e:
                raise SystemExit("mint unreachable: %s" % e)
            if not 200 <= status < 300:
                raise SystemExit("the Mint refused the %s: %s %s" % (kind, status, json.dumps(ans)))
            if keep:
                self.append("purchases.jsonl", rec)
            log("posted %s %s on purchase %s%s" % (kind, rec["id"][:16], pid[:16], ": +%s" % rec["escrow"] if kind == "top-up" else ""))
        return rec, rec["id"]

    # hop

    def my_link(self):
        """The link parameters this hop runs with: the pilot defaults under its config's `link` object."""
        p = dict(PILOT_LINK)
        cfg = self.config.get("link") or {}
        for k in LINK_PARAMS:
            if k in cfg:
                p[k] = cfg[k]
        return p

    def patience_state(self):
        """Per payer: how many windows fell short of our count beyond the margin, and whether we have stopped serving it."""
        return self.state().get("patience", {})

    def note_short(self, payer, short, now):
        """Count a window where the payer's count fell short beyond the margin (spec, Section 12)."""
        st = self.state()
        ps = st.setdefault("patience", {}).setdefault(payer, {"short": 0, "declined": False, "declined_at": None})
        if short:
            ps["short"] += 1
        self.write_json("state.json", st)
        return ps

    def decline_payer(self, payer, now):
        """Mark a payer declined and run the on_decline hook once, with the payer's NYM as its argument."""
        st = self.state()
        ps = st.setdefault("patience", {}).setdefault(payer, {"short": 0, "declined": False, "declined_at": None})
        if ps["declined"]:
            return
        ps["declined"], ps["declined_at"] = True, int(now)
        self.write_json("state.json", st)
        cmd = self.config.get("on_decline")
        if cmd:
            try:
                subprocess.Popen(shlex.split(cmd) + [payer], stdin=subprocess.DEVNULL)
                log("on_decline: ran %s for %s" % (cmd, payer))
            except OSError as e:
                log("on_decline: could not run %s: %s" % (cmd, e))

    def patience_reset(self, payer):
        st = self.state()
        st.get("patience", {}).pop(payer, None)
        self.write_json("state.json", st)

    def ack(self, env, now=None):
        """Acknowledge a payer's receipt with our own count (spec, Section 5). Raises Decline or ValueError."""
        now = int(now if now is not None else time.time())
        signer = check_envelope(env)
        if env["kind"] != "receipt":
            raise ValueError("expected a payer's receipt")
        r = env["receipt"]
        if r["hop"] != self.nym:
            raise ValueError("this receipt names another hop")
        if signer not in self.peers():
            raise Decline("unknown payer %s" % signer)
        link = self.my_link()
        tol = link["clock_tolerance_s"]
        if abs(now - r["t_last"]) > tol:
            raise Decline("clock: payer's t_last is %d s from ours (tolerance %d s)" % (now - r["t_last"], tol))
        ps = self.patience_state().get(signer)
        if link["patience_windows"] > 0 and ps and ps["short"] >= link["patience_windows"]:
            self.decline_payer(signer, now)
            raise Decline("patience: payer %s fell short of our count beyond the margin in %d windows (patience %d); declined" % (
                signer, ps["short"], link["patience_windows"]))
        try:
            _, nbytes, meter = self.window(now)
        except NoBaseline:
            self.sample(now)
            raise Decline("no baseline here; took one now, acks start with the next window")
        a = dict(r)
        a["bytes"] = nbytes
        a["prev_sig"] = env["sig"]
        ack = {"kind": "ack", "proto": PROTO, "from": self.name, "impl": IMPL,
               "receipt": a, "sig": sign_receipt(self.priv, a), "meter": meter}
        short = nbytes - r["bytes"] > max(link["min_bytes"], link["margin"] * nbytes)
        with self.lock:
            self.commit_sample(r["t_last"], meter)
            self.append("acks.jsonl", ack)
            self.outbox_put(ack)
            ps = self.note_short(signer, short, now)
        log("ack for %s %d..%d: payer says %d, we say %d%s" % (env.get("from"), r["t_first"], r["t_last"], r["bytes"], nbytes,
            " (short beyond the margin, %d so far)" % ps["short"] if short else ""))
        if self.config.get("mint"):
            self.flush_later(HOP_FLUSH_DELAY_S)   # our own copy, in case the payer drops an ack it dislikes
        return ack

    def announce(self, now=None, since=None, post=True):
        """Hop: sign this link's parameters and register them with the Mint (spec, Section 7). Returns (record, hash)."""
        if self.role != "hop":
            raise SystemExit("only a hop announces a link")
        now = int(now if now is not None else time.time())
        mint = (self.config.get("mint") or "").rstrip("/")
        domain = self.config.get("mint_nym")
        if not domain:
            if not mint:
                raise SystemExit("config has no mint URL (or mint_nym)")
            try:
                status, text = http_get(mint + "/status")
                domain = json.loads(text)["nym"]
            except (urlerror.URLError, socket.timeout, OSError, ValueError, KeyError) as e:
                raise SystemExit("could not read the Mint's NYM from %s/status: %s" % (mint, e))
        cfg = self.config.get("link") or {}
        body = {"kind": "link", "proto": PROTO, "relay": self.nym, "payer": cfg.get("payer", "*"),
                "domain": domain, "since": int(since if since is not None else now)}
        body.update({k: v for k, v in self.my_link().items()})
        rec = dict(body, sig=sign_link(self.priv, body))
        check_link(rec)
        why = profile_1_link(rec)
        if why:
            raise SystemExit("the link's config is outside profile 1: %s" % why)
        h = link_hash(rec)
        if post:
            if not mint:
                raise SystemExit("config has no mint URL")
            try:
                status, ans = http_post(mint + "/link", rec)
            except (urlerror.URLError, socket.timeout, OSError) as e:
                raise SystemExit("mint unreachable: %s" % e)
            if not 200 <= status < 300:
                raise SystemExit("the Mint refused the announcement: %s %s" % (status, json.dumps(ans)))
            self.append("links.jsonl", rec)
            log("announced link %s to the Mint: price %s %s, margin %s, min_bytes %d, patience %d, since %d" % (
                h[:16], rec["price"], rec["unit"], rec["margin"], rec["min_bytes"], rec["patience_windows"], rec["since"]))
        return rec, h

    # mint

    def load_links(self, skip_partial=False):
        return self.read_lines("links.jsonl", skip_partial)

    def load_purchases(self, skip_partial=False):
        return self.read_lines("purchases.jsonl", skip_partial)

    def load_map(self):
        """The membership's map: domains, places and which node is at which place (BWKIT_MAP, or `map` in the config)."""
        path = os.environ.get("BWKIT_MAP") or self.config.get("map") or DEFAULT_MAP_PATH
        if not os.path.exists(path):
            return {"domains": {}, "places": {}, "nodes": {}}
        with open(path) as f:
            m = json.load(f)
        for k in ("domains", "places", "nodes"):
            m.setdefault(k, {})
        return m

    def steward_params(self):
        p = dict(DEFAULT_STEWARD)
        p.update(self.config.get("steward") or {})
        return p

    def steward_nym(self):
        """The Steward's NYM: from its key beside the Mint's (steward.priv), else `steward_nym` in the config, else None."""
        kp = self.path("steward.priv")
        if os.path.exists(kp):
            with open(kp, "rb") as f:
                return nym_of_pub(Ed25519PrivateKey.from_private_bytes(f.read()).public_key())
        return self.config.get("steward_nym")

    def load_index(self, skip_partial=False):
        """Rebuild the Mint's view from the ledger, the announcements and the purchases.

        skip_partial: a reader beside the running Mint (the status reporter)
        may catch the last line half written; skip it rather than fail.
        """
        peers = self.peers()
        self.map = self.load_map()
        sp = self.steward_params()
        ix = {"by_sig": {}, "receipt_by_window": {}, "acks_by_prev": {}, "settled": {}, "first_seen": {},
              "commitments": {}, "last_commitment": None, "lines": 0, "links": self.load_links(skip_partial),
              "book": PurchaseBook(place_lookup(self.map, peers), self.config.get("steward_fee", DEFAULT_STEWARD_FEE), sp["margin"]),
              "steward_nym": self.steward_nym()}
        for rec in self.load_purchases(skip_partial):
            ix["book"].add(rec)
        for entry in self.read_lines("ledger.jsonl", skip_partial):
            self._index_line(ix, entry)
        self.index = ix
        return ix

    @staticmethod
    def _index_line(ix, entry):
        k = entry.get("kind")
        ix["lines"] += 1
        if k == "receipt":
            ix["by_sig"][entry["sig"]] = entry
            ix["receipt_by_window"][window_key(entry["receipt"])] = entry
            ix["first_seen"].setdefault(entry["receipt"]["payer"], entry["received_at"])
        elif k == "ack":
            ix["by_sig"][entry["sig"]] = entry
            ix["acks_by_prev"][entry["receipt"]["prev_sig"]] = entry
            ix["first_seen"].setdefault(entry["receipt"]["hop"], entry["received_at"])
        elif k == "settlement":
            ix["settled"][entry["receipt_sig"]] = entry
            ix["book"].note(entry)
        elif k == "commitment":
            ix["commitments"][entry["window"]] = entry     # the newest for an hour is the Mint's current word
            ix["last_commitment"] = entry

    def ledger_append(self, entry):
        self.append("ledger.jsonl", entry)
        self._index_line(self.index, entry)

    def receive(self, body, now=None):
        """The Mint's /receipt. Stores what is new and well signed, settles what it can."""
        now = int(now if now is not None else time.time())
        if self.index is None:
            self.load_index()
        envs = body if isinstance(body, list) else [body]
        if len(envs) > 16:
            raise ValueError("at most 16 envelopes per request")
        peers = self.peers()
        stored, duplicates, settlements, commitments = [], [], [], []
        with self.lock:
            for env in envs:
                signer = check_envelope(env)
                if signer not in peers:
                    raise Decline("unknown signer %s" % signer)
                if env["kind"] == "ack" and env["receipt"]["payer"] not in peers:
                    raise Decline("ack for an unknown payer")
                if env["sig"] in self.index["by_sig"]:
                    duplicates.append(env["sig"])
                    continue
                r = env["receipt"]
                if env["kind"] == "receipt":
                    held = self.index["receipt_by_window"].get(window_key(r))
                    if held is not None:
                        raise Decline("replay: another receipt for window %d..%d is already held (sig %s)" % (
                            r["t_first"], r["t_last"], held["sig"][:12]))
                else:
                    held = self.index["acks_by_prev"].get(r["prev_sig"])
                    if held is not None:
                        raise Decline("replay: another ack for receipt %s is already held (sig %s)" % (
                            r["prev_sig"][:12], held["sig"][:12]))
                entry = {"kind": env["kind"], "received_at": now, "from": peers[signer],
                         "receipt": env["receipt"], "sig": env["sig"], "meter": env.get("meter")}
                self.ledger_append(entry)
                stored.append(env["sig"])
            for env in envs:
                s = self.try_settle(env, now)
                if s:
                    settlements.append(s)
                    c = self.commit(commit_hour(s), now)
                    if c:
                        commitments.append(c)
        if not stored and duplicates and not settlements:
            raise Decline("duplicate")
        return {"stored": stored, "duplicates": duplicates, "settlements": settlements, "commitments": commitments}

    def register_link(self, rec, now=None):
        """The Mint's /link (spec, Section 7). Checks the announcement, appends it to links.jsonl, returns its hash."""
        now = int(now if now is not None else time.time())
        if self.index is None:
            self.load_index()
        check_link(rec)
        if not verify_link(rec):
            raise ValueError("bad signature")
        peers = self.peers()
        if rec["relay"] not in peers:
            raise Decline("unknown relay %s" % rec["relay"])
        if rec["payer"] != "*" and rec["payer"] not in peers:
            raise Decline("unknown payer %s" % rec["payer"])
        if rec["domain"] != self.nym:
            raise Decline("domain: the announcement names Mint %s, not this one" % rec["domain"])
        why = profile_1_link(rec)
        if why:
            raise Decline("profile: " + why)
        h = link_hash(rec)
        with self.lock:
            if any(link_hash(l) == h for l in self.index["links"]):
                raise Decline("duplicate")
            if rec["since"] < now - CLOCK_TOLERANCE_S:
                raise Decline("since: %d is in the past (the Mint's clock is %d); an announcement cannot be backdated" % (rec["since"], now))
            for e in self.index["by_sig"].values():
                r = e["receipt"]
                if r["hop"] == rec["relay"] and rec["payer"] in ("*", r["payer"]) and r["t_first"] >= rec["since"]:
                    raise Decline("since: a window from %d is already held; an announcement cannot change it" % r["t_first"])
            self.append("links.jsonl", rec)
            self.index["links"].append(rec)
        log("link %s registered by %s for %s: price %s %s, margin %s, min_bytes %d, patience %d, since %d" % (
            h[:16], peers[rec["relay"]], rec["payer"] if rec["payer"] == "*" else peers.get(rec["payer"], rec["payer"]),
            rec["price"], rec["unit"], rec["margin"], rec["min_bytes"], rec["patience_windows"], rec["since"]))
        return h

    def register_adjust(self, rec, now):
        """A top-up or a close on a purchase this Mint holds, by its poster (spec, Section 15). Returns the record's id."""
        check_adjust(rec)
        if not verify_adjust(rec):
            raise ValueError("bad signature")
        book = self.index["book"]
        target = book.purchases.get(rec["purchase"])
        if target is None:
            raise Decline("route: %s names no purchase this Mint holds" % rec["kind"])
        if rec["poster"] != target["poster"]:
            raise Decline("unknown poster %s: only the poster may %s its purchase" % (rec["poster"], rec["kind"]))
        with self.lock:
            if rec["id"] in book.ids:
                raise Decline("duplicate")
            if rec["posted_at"] < now - CLOCK_TOLERANCE_S:
                raise Decline("since: posted_at %d is in the past (the Mint's clock is %d); a %s cannot be backdated" % (rec["posted_at"], now, rec["kind"]))
            if target["id"] in book.closed:
                raise Decline("route: purchase %s is closed" % target["id"][:16])
            if rec["kind"] == "close":
                for e in self.index["by_sig"].values():
                    r = e["receipt"]
                    if r["t_last"] >= rec["posted_at"] and book.applies(target, r, r["t_last"]):
                        raise Decline("since: a window ending at %d that the purchase governs is already held" % r["t_last"])
            self.append("purchases.jsonl", rec)
            book.add(rec)
        log("%s %s on purchase %s by its poster%s" % (rec["kind"], rec["id"][:16], target["id"][:16],
                                                      ": +%s escrow" % rec["escrow"] if rec["kind"] == "top-up" else ""))
        return rec["id"]

    def register_purchase(self, rec, now=None):
        """The Mint's /purchase (spec, Section 15): a purchase, a top-up or a close. Appends it to purchases.jsonl, returns its id."""
        now = int(now if now is not None else time.time())
        if self.index is None:
            self.load_index()
        if isinstance(rec, dict) and rec.get("kind") in ("top-up", "close"):
            return self.register_adjust(rec, now)
        check_purchase(rec)
        if not verify_purchase(rec):
            raise ValueError("bad signature")
        peers = self.peers()
        steward = self.index["steward_nym"]
        if rec["program"] == "payer":
            if rec["poster"] not in peers:
                raise Decline("unknown poster %s" % rec["poster"])
            if rec["route"]["from"] not in (None, rec["poster"]):
                raise Decline("route: a payer's purchase starts at the payer (or anywhere, null)")
            to = rec["route"]["to"]
            if to.startswith("ed25519:"):
                if to not in peers:
                    raise Decline("unknown route.to %s" % to)
            elif to not in self.map["places"]:
                raise Decline("route: %s is not a place on the map" % to)
        else:
            if not steward or rec["poster"] != steward:
                raise Decline("unknown poster %s: only the Steward posts a %s" % (rec["poster"], rec["program"]))
            if rec["program"] == "match" and rec["matches"] not in self.index["book"].purchases:
                raise Decline("route: a match names a purchase this Mint holds")
        book = self.index["book"]
        with self.lock:
            if rec["id"] in book.ids:
                raise Decline("duplicate")
            if rec["posted_at"] < now - CLOCK_TOLERANCE_S:
                raise Decline("since: posted_at %d is in the past (the Mint's clock is %d); a purchase cannot be backdated" % (rec["posted_at"], now))
            if rec["program"] != "match":
                for e in self.index["by_sig"].values():
                    r = e["receipt"]
                    if r["t_last"] >= rec["posted_at"] and book.applies(rec, r, r["t_last"]):
                        raise Decline("since: a window ending at %d that this purchase would govern is already held" % r["t_last"])
            self.append("purchases.jsonl", rec)
            book.add(rec)
        log("purchase %s registered: %s by %s, %s -> %s, >= %d bytes a window, premium %s/GB, %d windows, escrow %s, until %d" % (
            rec["id"][:16], rec["program"], peers.get(rec["poster"], "the Steward" if rec["poster"] == steward else rec["poster"]),
            rec["route"]["from"], rec["route"]["to"], rec["min_bytes"], rec["premium_per_gb"], rec["windows"], rec["escrow"], rec["deadline"]))
        return rec["id"]

    def try_settle(self, env, now):
        ix = self.index
        if env["kind"] == "receipt":
            rec = ix["by_sig"].get(env["sig"])
            ack = ix["acks_by_prev"].get(env["sig"])
        else:
            rec = ix["by_sig"].get(env["receipt"]["prev_sig"])
            ack = ix["by_sig"].get(env["sig"])
        if not rec or not ack or rec["sig"] in ix["settled"]:
            return None
        r = rec["receipt"]
        link = link_in_force(ix["links"], r["payer"], r["hop"], r["t_first"])
        try:
            s = resolve(rec, ack, self.config.get("price_usd_per_gb", DEFAULT_PRICE_USD_PER_GB), now, link,
                        self.config.get("steward_fee", DEFAULT_STEWARD_FEE), ix["book"].context(r, r["t_last"]))
        except ValueError as e:
            log("receipt %s and ack %s do not settle: %s" % (rec["sig"][:12], ack["sig"][:12], e))
            return None
        self.ledger_append(s)
        log("%s %s..%d: payer %d, hop %d, settled %d bytes ($%.6f, fee $%.6f, margin %s%s%s)" % (
            s["status"], s["t_first"], s["t_last"], s["payer_bytes"], s["hop_bytes"], s["settled_bytes"], s["amount_usd"],
            s["fee_usd"], s["margin"], " from link %s" % s["link"][:16] if s["link"] else ", the default",
            "; purchase %s: premium $%.6f (poster $%.6f, Steward $%.6f), $%.6f returned" % (
                s["purchase"][:16], s["premium_usd"], s["from_poster_usd"], s["from_steward_usd"], s["refund_usd"]) if s.get("purchase") else ""))
        return s

    def commit(self, window, now):
        """Mint: sign the Merkle root over an hour's settlements and append the commitment.

        Called after every settlement, so an hour with several windows (the
        forced tests, a late window from an outbox) gets a new commitment each
        time; the newest one for an hour is the Mint's current word on it. A
        ledger from before commitments gets its first one for an hour with
        the next window settled in it.
        """
        if self.priv is None:
            log("no key, so no commitment for hour %d" % window)
            return None
        c = make_commitment(self.priv, [s for s in self.index["settled"].values() if commit_hour(s) == window], window, now)
        self.ledger_append(c)
        log("commitment for hour %d: root %s over %d settlement%s" % (window, c["root"][:16], c["count"], "" if c["count"] == 1 else "s"))
        return c

    def commitments(self):
        """Mint: the current commitment of every hour, oldest first."""
        if self.index is None:
            self.load_index()
        return sorted(self.index["commitments"].values(), key=lambda c: c["window"])

    def proof(self, window, reload=True):
        """Mint: the inclusion proofs for an hour's settlements, against its current commitment."""
        if reload or self.index is None:
            self.load_index(skip_partial=True)
        ss = [s for s in self.index["settled"].values() if commit_hour(s) == window]
        c = self.index["commitments"].get(window)
        if c is None:
            if ss:
                raise SystemExit("hour %d: %d settlement%s and no commitment (settled before this Mint made commitments, or one is being written)" % (
                    window, len(ss), "" if len(ss) == 1 else "s"))
            raise SystemExit("no settlements in hour %d" % window)
        return make_proof(c, ss)

    def ledger_lines(self):
        return self.read_lines("ledger.jsonl")

    def pending(self):
        """Receipts with no ack yet, and acks with no receipt."""
        if self.index is None:
            self.load_index()
        ix = self.index
        no_ack = [e for s, e in ix["by_sig"].items() if e["kind"] == "receipt" and s not in ix["acks_by_prev"]]
        no_receipt = [e for e in ix["acks_by_prev"].values() if e["receipt"]["prev_sig"] not in ix["by_sig"]]
        return no_ack, no_receipt

    def invoice(self, month):
        """Sum the settlements of a month (YYYY-MM) per payer and hop, in local time, with the Steward's share apart.

        A settlement from before profile 1 carries no fee, so none is taken from it.
        """
        out = {}
        for e in self.ledger_lines():
            if e.get("kind") != "settlement":
                continue
            if time.strftime("%Y-%m", time.localtime(e["t_last"])) != month:
                continue
            k = (e["payer"], e["hop"])
            o = out.setdefault(k, {"windows": 0, "disputed": 0, "settled_bytes": 0, "amount_usd": 0.0, "fee_usd": 0.0, "relay_usd": 0.0,
                                   "premium_usd": 0.0, "refund_usd": 0.0, "from_steward_usd": 0.0, "purchases": 0})
            o["windows"] += 1
            o["disputed"] += 1 if e["status"] == "disputed" else 0
            o["settled_bytes"] += e["settled_bytes"]
            fee = e.get("fee_usd", 0.0)
            o["amount_usd"] = round(o["amount_usd"] + e["amount_usd"], 6)
            o["fee_usd"] = round(o["fee_usd"] + fee, 6)
            if e.get("purchase"):
                o["purchases"] += 1
                o["premium_usd"] = round(o["premium_usd"] + e["premium_usd"], 6)
                o["refund_usd"] = round(o["refund_usd"] + e["refund_usd"], 6)
                o["from_steward_usd"] = round(o["from_steward_usd"] + e["from_steward_usd"], 6)
            o["relay_usd"] = round(o["amount_usd"] + o["premium_usd"] - o["fee_usd"], 6)
        return out

    # the status page

    def service_up(self):
        """Does this node's own server answer on the loopback? None when it has none."""
        if self.role not in ("hop", "mint"):
            return None
        port = int(self.config.get("port", DEFAULT_PORT))
        try:
            status, _ = http_get("http://127.0.0.1:%d/status" % port, timeout=3)
            return status == 200
        except (urlerror.URLError, socket.timeout, OSError, ValueError):
            return False

    def status_report(self, now=None, service=None):
        """Mint: the lab's public summary for the status page, from the ledger alone.

        Names and roles, never an address or a hostname beyond the nodes'
        names. Per node: when its last receipt (for the hop, its last ack)
        reached the Mint, the bytes it signed for its latest window, how
        many it has signed, how many of its windows settled and how many of
        those were disputed, its running total of bytes (signed, counted, or
        settled for the Mint), and `history`: its windows of the last twelve
        hours (HISTORY_HOURS, at most HISTORY_MAX), each with the window's
        bounds, the bytes, the settled bytes and the disputed flag (None while
        unsettled). The Mint's own line counts what it recorded. `since` is
        when the first entry reached the ledger. `service` is whether the
        Mint's server answered (service_up), None when not checked.
        `commitment` is the Mint's latest: the hour, the Merkle root over
        that hour's settlements, how many, the Mint's signature over them and
        the Mint's NYM (None until the first); `proof` is the proof document
        for that hour, so a browser can check the commitment against the
        Mint's key with nothing else; `mint` is the Mint's NYM; `steward` is
        the Steward's book (Steward.feed: names and places only, None until
        the Steward has a key). NYMs are public keys and may appear;
        addresses may not (assert_public).
        """
        now = int(now if now is not None else time.time())
        self.load_index(skip_partial=True)
        ix = self.index
        peers = self.peers()
        order = {"payer": 0, "hop": 1, "mint": 2, "peer": 3}
        cut = (now // 3600 - (HISTORY_HOURS - 1)) * 3600      # the start of the oldest of the last twelve hours
        nodes = {}

        def node(name, role):
            n = nodes.get(name)
            if n is None:
                n = nodes[name] = {"name": name, "role": role, "last_receipt": None, "window_bytes": None,
                                   "window_t_first": None, "window_t_last": None,
                                   "receipts": 0, "windows": 0, "disputed": 0, "total_bytes": 0, "history": []}
            elif order[role] < order[n["role"]]:
                n["role"] = role
            return n

        def window_of(r, nbytes, st):
            return {"t_first": r["t_first"], "t_last": r["t_last"], "bytes": nbytes,
                    "settled_bytes": st["settled_bytes"] if st else None,
                    "disputed": (st["status"] == "disputed") if st else None}

        for name in peers.values():
            node(name, "peer")                      # a peer with nothing on the ledger yet is still listed
        entries = list(ix["by_sig"].values())
        for e in entries:
            n = node(e["from"], "payer" if e["kind"] == "receipt" else "hop")
            r = e["receipt"]
            n["receipts"] += 1
            n["total_bytes"] += r["bytes"]
            if n["last_receipt"] is None or e["received_at"] > n["last_receipt"]:
                n["last_receipt"] = e["received_at"]
            if n["window_t_last"] is None or r["t_last"] > n["window_t_last"]:
                n["window_bytes"], n["window_t_first"], n["window_t_last"] = r["bytes"], r["t_first"], r["t_last"]
            if r["t_last"] >= cut:
                st = ix["settled"].get(e["sig"] if e["kind"] == "receipt" else r["prev_sig"])
                n["history"].append(window_of(r, r["bytes"], st))
        for st in ix["settled"].values():
            for nym, role in ((st["payer"], "payer"), (st["hop"], "hop")):
                if nym in peers:
                    n = node(peers[nym], role)
                    n["windows"] += 1
                    n["disputed"] += 1 if st["status"] == "disputed" else 0
        me = node(self.name, "mint")
        me["role"] = "mint"
        me["receipts"] = len(entries)
        me["last_receipt"] = max((e["received_at"] for e in entries), default=None)
        me["windows"] = len(ix["settled"])
        me["disputed"] = sum(1 for st in ix["settled"].values() if st["status"] == "disputed")
        me["total_bytes"] = sum(st["settled_bytes"] for st in ix["settled"].values())
        me["history"] = [window_of(st, st["settled_bytes"], st) for st in ix["settled"].values() if st["t_last"] >= cut]
        latest = max(ix["settled"].values(), key=lambda st: (st["t_last"], st["t"]), default=None)
        if latest:
            me["window_bytes"], me["window_t_first"], me["window_t_last"] = latest["settled_bytes"], latest["t_first"], latest["t_last"]
        for n in nodes.values():
            n["history"].sort(key=lambda w: (w["t_last"], w["t_first"]))
            del n["history"][:-HISTORY_MAX]
        no_ack, no_receipt = self.pending()
        last = ix["last_commitment"]
        proof = None
        if last:
            try:
                proof = make_proof(last, [s for s in ix["settled"].values() if commit_hour(s) == last["window"]])
            except ValueError:
                proof = None                         # a settlement landed after this commitment; the next report has both
        steward = None
        if ix["steward_nym"]:
            try:
                steward = Steward(self).feed(now)
            except (SystemExit, ValueError, OSError) as e:
                log("steward: no book for the report: %s" % e)
        return {
            "v": 1, "generated_at": now, "bwkit": VERSION, "proto": PROTO, "phase": 0, "window_s": 3600,
            "since": min((e["received_at"] for e in entries), default=None),
            "service": service,
            "mint": self.nym if self.priv else None,
            "commitment": {k: last[k] for k in ("t", "window", "root", "count", "signature", "mint")} if last else None,
            "proof": proof,
            "steward": steward,
            "ledger": {"receipts": sum(1 for e in entries if e["kind"] == "receipt"),
                       "acks": sum(1 for e in entries if e["kind"] == "ack"),
                       "settlements": len(ix["settled"]), "disputed": me["disputed"],
                       "receipts_without_ack": len(no_ack), "acks_without_receipt": len(no_receipt)},
            "nodes": sorted(nodes.values(), key=lambda n: (order[n["role"]], n["name"])),
        }

    def report(self, url=None, token=None, now=None):
        """Mint: post status_report() to the status page's Worker. Returns (HTTP status, body).

        The URL and the token come from the arguments, else from the
        environment (BWKIT_STATUS_URL, BWKIT_STATUS_TOKEN: the systemd unit
        reads them from the root-only /etc/bwkit/status.env), the URL also from
        `status_url` in the config. The token is never in the config.
        """
        if self.role != "mint":
            raise SystemExit("only the Mint reports")
        url = url or os.environ.get("BWKIT_STATUS_URL") or self.config.get("status_url")
        token = token or os.environ.get("BWKIT_STATUS_TOKEN")
        if not url:
            raise SystemExit("no status URL: set BWKIT_STATUS_URL in /etc/bwkit/status.env (or status_url in the config)")
        if not token:
            raise SystemExit("no token: set BWKIT_STATUS_TOKEN in /etc/bwkit/status.env")
        rep = self.status_report(now, service=self.service_up())
        text = json.dumps(rep, sort_keys=True)
        assert_public(text)
        if len(text) > REPORT_MAX:
            raise ValueError("the report is %d bytes; the status Worker takes at most %d" % (len(text), REPORT_MAX))
        return http_post(url, rep, timeout=STATUS_TIMEOUT_S, headers={"Authorization": "Bearer " + token})

    # the server

    def serve(self, host="0.0.0.0", port=None, block=True):
        if self.role not in ("hop", "mint"):
            raise SystemExit("only the hop and the mint serve")
        if self.role == "mint":
            self.load_index()
        node = self
        port = port if port is not None else int(self.config.get("port", DEFAULT_PORT))

        class Handler(BaseHTTPRequestHandler):
            server_version = IMPL

            def log_message(self, fmt, *args):
                pass

            def reply(self, status, obj):
                data = json.dumps(obj, sort_keys=True).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlparse.urlsplit(self.path)
                if url.path == "/status":
                    st = {"name": node.name, "role": node.role, "nym": node.nym, "bwkit": VERSION, "impl": IMPL,
                          "proto": list(PROTOS), "time": int(time.time())}
                    if node.role == "mint":
                        no_ack, no_receipt = node.pending()
                        st["ledger_lines"] = node.index["lines"]
                        st["settlements"] = len(node.index["settled"])
                        st["commitments"] = len(node.index["commitments"])
                        st["disputed"] = sum(1 for s in node.index["settled"].values() if s["status"] == "disputed")
                        st["pending"] = {"receipts_without_ack": len(no_ack), "acks_without_receipt": len(no_receipt)}
                        st["links"] = node.index["links"]
                        st["purchases"] = len(node.index["book"].order)
                        st["steward"] = node.index["steward_nym"]
                    else:
                        st["sample"] = node.state().get("sample")
                        st["outbox"] = len(os.listdir(node.path("outbox")))
                        st["link"] = node.my_link()
                        st["patience"] = node.patience_state()
                    return self.reply(200, st)
                if url.path == "/ledger" and node.role == "mint":
                    p = node.path("ledger.jsonl")
                    data = open(p, "rb").read() if os.path.exists(p) else b""
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if url.path == "/commitments" and node.role == "mint":
                    return self.reply(200, node.commitments())
                if url.path == "/links" and node.role == "mint":
                    return self.reply(200, node.index["links"])
                if url.path == "/purchases" and node.role == "mint":
                    book = node.index["book"]
                    return self.reply(200, [book.purchases[pid] for pid in book.order])
                if url.path == "/proof" and node.role == "mint":
                    q = urlparse.parse_qs(url.query).get("window", [""])[0]
                    if not q.isdigit():
                        return self.reply(400, {"error": "window must be Unix seconds"})
                    try:
                        with node.lock:
                            return self.reply(200, node.proof(int(q) - int(q) % 3600, reload=False))
                    except SystemExit as e:
                        return self.reply(404, {"error": str(e)})
                self.reply(404, {"error": "no such path"})

            def do_POST(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > MAX_BODY:
                        return self.reply(413, {"error": "too large"})
                    body = json.loads(self.rfile.read(n).decode())
                except ValueError:
                    return self.reply(400, {"error": "bad JSON"})
                try:
                    if self.path == "/ack" and node.role == "hop":
                        return self.reply(200, node.ack(body))
                    if self.path == "/receipt" and node.role == "mint":
                        return self.reply(200, node.receive(body))
                    if self.path == "/link" and node.role == "mint":
                        return self.reply(200, {"stored": node.register_link(body)})
                    if self.path == "/purchase" and node.role == "mint":
                        return self.reply(200, {"stored": node.register_purchase(body)})
                    return self.reply(404, {"error": "no such path"})
                except Decline as e:
                    code = 409
                    msg = str(e)
                    if msg.startswith("unknown"):
                        code = 403
                    word = msg.split(":")[0]
                    log("declined %s from %s: %s" % (self.path, self.client_address[0], msg))
                    return self.reply(code, {"error": word if word in DECLINE_WORDS else msg, "detail": msg})
                except ValueError as e:
                    log("refused %s from %s: %s" % (self.path, self.client_address[0], e))
                    return self.reply(400, {"error": str(e)})

        srv = ThreadingHTTPServer((host, port), Handler)
        srv.daemon_threads = True
        if self.role == "hop" and self.config.get("mint"):
            def retry():
                while True:
                    time.sleep(FLUSH_EVERY_S)
                    self.flush_quietly()
            threading.Thread(target=retry, daemon=True).start()
        log("%s %s serving on %s:%d as %s" % (self.role, self.name, host, srv.server_address[1], self.nym))
        if block:
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
            srv.server_close()
            return None
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv


# ---------------------------------------------------------------- the Steward (paper, Section 7; PLAN.md 2.4)

def usd(x):
    """Five places: at the lab's scale the Steward's whole income is a fifth of a cent."""
    return "%.5f" % x


class Steward:
    """The Network Steward of a one-Mint domain: a rule run on the Mint's host after each hour's commitment.

    Its income is every settlement's fee_usd. Its only act is to post
    purchases (spec, Section 15) through the Mint's own /purchase, signed
    with its own key (steward.priv beside the Mint's): a match on a payer's
    purchase (program 1), a bounty for a second path around a link that
    carried fees between two places (program 2), and a standing offer for the
    first link to every mapped place that has none (program 3), topped up
    toward its size on every run. Every balance here is read from the ledger
    and the purchases; steward.jsonl is the log of its runs.
    """

    def __init__(self, node):
        if node.role != "mint":
            raise SystemExit("the Steward runs on the Mint's host, under the Mint's config")
        self.node = node
        self.params = node.steward_params()
        self.priv = None
        kp = node.path("steward.priv")
        if os.path.exists(kp):
            with open(kp, "rb") as f:
                self.priv = Ed25519PrivateKey.from_private_bytes(f.read())

    def keygen(self):
        kp = self.node.path("steward.priv")
        if os.path.exists(kp):
            raise SystemExit("%s exists; not overwriting the Steward's identity" % kp)
        priv = Ed25519PrivateKey.generate()
        raw = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        self.priv = priv
        return self.nym

    @property
    def nym(self):
        if self.priv is None:
            raise SystemExit("the Steward has no key yet: run `bwkit steward keygen`")
        return nym_of_pub(self.priv.public_key())

    def mint_url(self):
        return (self.node.config.get("steward_mint") or "http://127.0.0.1:%d" % int(self.node.config.get("port", DEFAULT_PORT))).rstrip("/")

    # ---- what the ledger says

    def tally(self, now=None):
        """Every balance, from the Mint's ledger, purchases and map (reloaded). Nothing here is stored."""
        now = int(now if now is not None else time.time())
        node = self.node
        node.load_index(skip_partial=True)
        ix, book, peers = node.index, node.index["book"], node.peers()
        nym = self.nym if self.priv else ix["steward_nym"]
        f, m = node.config.get("steward_fee", DEFAULT_STEWARD_FEE), self.params["m"]
        settled = sorted(ix["settled"].values(), key=lambda st: (st["t"], settlement_order(st)))
        income = round(float(sum(st.get("fee_usd", 0.0) for st in settled)), 6)
        place_of = book.place_of
        recs = [book.purchases[pid] for pid in book.order]
        rows = []
        for rec in recs:
            u = book.use.get(rec["id"], {})
            used = u.get("windows", 0)
            target = book.purchases.get(rec["matches"]) if rec["program"] == "match" else None
            if target is not None:
                used = book.use.get(target["id"], {}).get("windows", 0)
            pool = book.pool(rec)
            total = book.total_escrow(rec)
            if rec["id"] in book.closed and now >= book.closed[rec["id"]]:
                status = "closed"
            elif now > rec["deadline"]:
                status = "expired"
            elif used >= rec["windows"]:
                status = "complete"
            elif pool <= 0:
                status = "spent"
            else:
                status = "open"
            rows.append({"rec": rec, "status": status, "used": used, "pool": pool, "paid": round(total - pool, 6), "total": total,
                         "topups": book.topups_total(rec), "mine": rec["poster"] == nym, "refund": u.get("refund", 0.0),
                         "match": book.match_for(rec["id"]) if rec["program"] == "payer" else None})
        mine = [r for r in rows if r["mine"]]
        posted = round(float(sum(r["total"] for r in mine)), 6)
        paid = round(float(sum(r["paid"] for r in mine)), 6)
        returned = round(float(sum(r["pool"] for r in mine if r["status"] != "open")), 6)
        in_offers = round(float(sum(r["pool"] for r in mine if r["status"] == "open")), 6)
        held = round(float(sum(st.get("steward_held_usd", 0.0) for st in settled)), 6)
        matched = round(float(sum(r["total"] for r in mine if r["rec"]["program"] == "match")), 6)
        escrow = round(income - posted + returned, 6)
        reserve = round(max(0.0, m * income - matched), 6)
        # the links of the program window: (payer's place, hop's place) with the fees they carried
        since = now - self.params["program_window_s"]
        links = {}
        for st in settled:
            if st["t_last"] < since:
                continue
            key = (place_of(st["payer"]), place_of(st["hop"]), peers.get(st["payer"], "?"), peers.get(st["hop"], "?"))
            L = links.setdefault(key, {"windows": 0, "bytes": 0, "fees": 0.0, "take": 0.0})
            L["windows"] += 1
            L["bytes"] += st["settled_bytes"]
            L["fees"] = round(L["fees"] + st["amount_usd"], 6)
            L["take"] = round(L["take"] + st.get("fee_usd", 0.0), 6)
        # a place is linked once a settled receipt has either end there: a link has two ends, and the paper's standing offer is
        # for a place no receipt has crossed a link to
        linked = ({place_of(st["hop"]) for st in settled} | {place_of(st["payer"]) for st in settled}) - {None}
        return {"now": now, "nym": nym, "f": f, "m": m, "income": income, "escrow": escrow, "reserve": reserve, "posted": posted,
                "paid": paid, "returned": returned, "in_offers": in_offers, "held": held, "matched": matched, "rows": rows,
                "links": links, "linked": linked, "settled": settled, "peers": peers, "place_of": place_of, "book": book, "ix": ix}

    # ---- the programs

    def post(self, t, program, to, route_from, not_via, min_bytes, premium, windows, escrow, deadline, matches=None):
        rec, pid = self.node.purchase_post(to, min_bytes, premium, windows, escrow, deadline, now=t["now"], post=True,
                                           poster_priv=self.priv, program=program, route_from=route_from, not_via=not_via,
                                           matches=matches, mint_url=self.mint_url(), keep=False)
        t["escrow"] = round(t["escrow"] - escrow, 6)
        t["posted"] = round(t["posted"] + escrow, 6)
        return rec

    def adjust(self, t, kind, pid, escrow=None):
        rec, _ = self.node.purchase_adjust(kind, pid, escrow, now=t["now"], post=True, poster_priv=self.priv, mint_url=self.mint_url(), keep=False)
        if kind == "top-up":
            t["escrow"] = round(t["escrow"] - escrow, 6)
            t["posted"] = round(t["posted"] + escrow, 6)
        return rec

    def run(self, now=None):
        """One run of the three programs over the ledger as it stands. Returns the run record, also appended to steward.jsonl."""
        if rules is None:
            raise SystemExit("steward_rules.py is not installed beside bwkit")
        t = self.tally(now)
        now, p, f, m = t["now"], self.params, t["f"], t["m"]
        posted, skipped = [], []
        budget = lambda: rules.bounty_budget(t["escrow"], m, t["income"], t["matched"])   # noqa: E731
        # 1. matching: a payer's open purchase that has no match yet, from a payer the Mint has known long enough
        for r in t["rows"]:
            rec = r["rec"]
            if rec["program"] != "payer" or r["status"] != "open" or r["match"] is not None:
                continue
            age = now - t["ix"]["first_seen"].get(rec["poster"], now)
            if age < p["min_payer_age_s"]:
                skipped.append("match %s: %s has been on the ledger %d s, the program needs %d" % (rec["id"][:8], t["peers"].get(rec["poster"], "?"), age, p["min_payer_age_s"]))
                continue
            amt = round(rules.match_size(m, rec["escrow"], t["income"], t["matched"], t["escrow"]), 6)
            if amt <= 0:
                skipped.append("match %s: nothing to match with (m of income %s, matched %s, escrow %s)" % (rec["id"][:8], usd(m * t["income"]), usd(t["matched"]), usd(t["escrow"])))
                continue
            mrec = self.post(t, "match", rec["route"]["to"], rec["route"]["from"], rec["route"]["not_via"], rec["min_bytes"],
                             rec["premium_per_gb"], rec["windows"], amt, rec["deadline"], matches=rec["id"])
            t["matched"] = round(t["matched"] + amt, 6)
            posted.append(mrec)
            log("steward: matched %s with %s (m = %s of the poster's %s, within m of income %s)" % (rec["id"][:16], usd(amt), m, usd(rec["escrow"]), usd(t["income"])))
        # 2. the second bridge: every link between two places that carried fees this window and has no open bounty
        for (a, b, pn, hn), L in sorted(t["links"].items(), key=lambda kv: (-kv[1]["fees"], str(kv[0]))):
            if a is None or b is None:
                skipped.append("second bridge %s -> %s: a node is not on the map" % (pn, hn))
                continue
            if a == b:
                skipped.append("second bridge %s -> %s: both ends are at %s; no second place to go through" % (pn, hn, a))
                continue
            if any(r["status"] == "open" and r["rec"]["program"] == "second_bridge" and r["rec"]["route"]["from"] == a and r["rec"]["route"]["to"] == b
                   for r in t["rows"]):
                continue
            value = round(rules.second_bridge_size(f, L["fees"], budget()), 6)
            if value <= 0:
                skipped.append("second bridge %s -> %s: the escrow is spent (the link carried %s, the Steward's take %s)" % (a, b, usd(L["fees"]), usd(L["take"])))
                continue
            windows = max(1, p["program_window_s"] // 3600)
            premium = round(rules.second_bridge_premium(value, L["bytes"]), 6)
            rec = self.post(t, "second_bridge", b, a, [a, b], L["bytes"] // windows, premium, windows, value, now + p["bounty_deadline_s"])
            posted.append(rec)
            log("steward: posted %s for a second path from %s to %s (the sole link carried %s this window, the Steward's take %s), premium %s/GB" % (
                usd(value), a, b, usd(L["fees"]), usd(L["take"]), premium))
        # 3. the first link: one standing offer per mapped place no settled window has an end at, topped up toward its size
        for pid, place in sorted(self.node.map.get("places", {}).items()):
            if not place.get("mapped", True) or pid in t["linked"]:
                continue
            standing = sorted((r for r in t["rows"] if r["status"] == "open" and r["rec"]["program"] == "first_link" and r["rec"]["route"]["to"] == pid),
                              key=lambda r: (r["rec"]["posted_at"], r["rec"]["id"]))
            for extra in standing[1:]:                       # more than one open offer (an older rule posted one per run): keep the oldest
                self.adjust(t, "close", extra["rec"]["id"])
                t["escrow"] = round(t["escrow"] + extra["pool"], 6)
                t["returned"] = round(t["returned"] + extra["pool"], 6)
                posted.append({"id": extra["rec"]["id"], "kind": "close"})
                log("steward: closed the extra first-link offer %s for %s; %s returns to the escrow" % (extra["rec"]["id"][:16], pid, usd(extra["pool"])))
            standing = standing[:1]
            open_so_far = standing[0]["total"] if standing else 0.0
            value = round(rules.first_link_size(p["first_link_usd"], budget(), open_so_far), 6)
            if value <= 0:
                skipped.append("first link to %s: %s" % (pid, "the offer stands at its size" if open_so_far >= p["first_link_usd"] else "the escrow is spent"))
                continue
            if standing:
                rec = self.adjust(t, "top-up", standing[0]["rec"]["id"], value)
                posted.append({"id": rec["id"], "kind": "top-up"})
            else:
                windows = max(1, p["bounty_deadline_s"] // 3600)
                rec = self.post(t, "first_link", pid, None, [], 0, p["first_link_premium"], windows, value, now + p["bounty_deadline_s"])
                posted.append(rec)
            log("steward: %s the first-link offer for %s by %s (now %s toward %s)" % (
                "topped up" if standing else "posted", pid, usd(value), usd(open_so_far + value), usd(p["first_link_usd"])))
        run = {"kind": "steward-run", "proto": PROTO, "t": now, "steward": t["nym"], "income_usd": t["income"], "escrow_usd": t["escrow"],
               "posted_usd": t["posted"], "paid_usd": t["paid"], "refunded_usd": t["returned"], "held_by_cap_usd": t["held"],
               "posted": [r["id"] for r in posted if r.get("kind", "purchase") == "purchase"],
               "adjusted": [{"kind": r["kind"], "id": r["id"]} for r in posted if r.get("kind", "purchase") != "purchase"], "skipped": skipped}
        self.node.append("steward.jsonl", run)
        return run

    def last_run(self):
        runs = self.node.read_lines("steward.jsonl", skip_partial=True)
        return runs[-1] if runs else None

    # ---- the book, in text and as the feed's object (the same function)

    def feed(self, now=None):
        """The Steward's book as the status feed carries it (PLAN.md 2.5): names and places only, never a NYM but the Steward's own."""
        t = self.tally(now)
        peers, place_of = t["peers"], t["place_of"]
        places = self.node.map.get("places", {})

        def name(x):
            if x is None:
                return None
            if isinstance(x, str) and x.startswith("ed25519:"):
                return "steward" if x == t["nym"] else peers.get(x, "unknown")
            return x

        purchases = []
        for r in t["rows"]:
            rec = r["rec"]
            purchases.append({"id": rec["id"][:16], "program": rec["program"], "poster": name(rec["poster"]),
                              "route": {"from": name(rec["route"]["from"]), "to": name(rec["route"]["to"]), "not_via": list(rec["route"]["not_via"])},
                              "premium_per_gb": rec["premium_per_gb"], "min_bytes": rec["min_bytes"],
                              "windows": rec["windows"], "windows_left": max(0, rec["windows"] - r["used"]), "deadline": rec["deadline"],
                              "posted_at": rec["posted_at"], "escrow_usd": r["total"], "topups_usd": r["topups"], "paid_usd": r["paid"], "refund_usd": r["refund"],
                              "match_usd": r["match"]["escrow"] if r["match"] else None, "status": r["status"]})
        payouts = []
        for st in sorted((st for st in t["settled"] if st.get("purchase")), key=lambda st: (-st["t"], -st["t_last"]))[:24]:
            payouts.append({"t": st["t"], "window": {"t_first": st["t_first"], "t_last": st["t_last"]}, "payer": name(st["payer"]),
                            "hop": name(st["hop"]), "purchase": st["purchase"][:16], "settled_bytes": st["settled_bytes"],
                            "premium_usd": st["premium_usd"], "from_steward_usd": st["from_steward_usd"], "from_poster_usd": st["from_poster_usd"],
                            "refund_usd": st["refund_usd"]})
        last = self.last_run()
        return {"nym": t["nym"], "f": t["f"], "m": t["m"], "margin": self.params["margin"],
                "program_window_s": self.params["program_window_s"], "first_link_usd": self.params["first_link_usd"],
                "income_usd": t["income"], "escrow_usd": t["escrow"], "reserve_usd": t["reserve"], "posted_usd": t["posted"],
                "in_offers_usd": t["in_offers"], "paid_usd": t["paid"], "refunded_usd": t["returned"], "held_by_cap_usd": t["held"],
                "last_run": last["t"] if last else None,
                "purchases": purchases, "payouts": payouts,
                "links": [{"from": a, "to": b, "payer": pn, "hop": hn, "windows": L["windows"], "bytes": L["bytes"], "fees_usd": L["fees"], "take_usd": L["take"]}
                          for (a, b, pn, hn), L in sorted(t["links"].items(), key=lambda kv: str(kv[0]))],
                "map": {"domains": self.node.map.get("domains", {}),
                        "places": [{"id": pid, "name": pl.get("name", pid), "medium": pl.get("medium"), "range_class": pl.get("range_class"),
                                    "linked": pid in t["linked"]} for pid, pl in sorted(places.items())]}}

    def book(self, now=None):
        """The book in text: the page in the operator's terminal, built from the same feed."""
        fd = self.feed(now)
        dom = next(iter(fd["map"]["domains"].items()), (None, {}))
        out = ["Steward %s  domain %s (%s)  f %s  m %s  cap f - %s per payer" % (
            fd["nym"][:12] + "..." + fd["nym"][-4:] if fd["nym"] else "(no key)", dom[0] or "?", dom[1].get("unit", "USD"), pct(fd["f"]), pct(fd["m"]), pct(fd["margin"]))]
        out.append("income     %s  escrow %s  reserve %s" % (usd(fd["income_usd"]), usd(fd["escrow_usd"]), usd(fd["reserve_usd"])))
        out.append("posted     %s  paid   %s  returned %s  held %s  in open offers %s" % (
            usd(fd["posted_usd"]), usd(fd["paid_usd"]), usd(fd["refunded_usd"]), usd(fd["held_by_cap_usd"]), usd(fd["in_offers_usd"])))
        opened = [pu for pu in fd["purchases"] if pu["status"] == "open"]
        out.append("open" if opened else "open: nothing")
        short = {"payer": "payer", "match": "match", "second_bridge": "bridge", "first_link": "first"}
        for pu in opened:
            route = "%s -> %s%s" % (pu["route"]["from"] or "*", pu["route"]["to"], (" !via " + ",".join(pu["route"]["not_via"])) if pu["route"]["not_via"] else "")
            out.append("  %s  %-6s %-34s %s  %s/GB  %s  %s%s%s" % (
                pu["id"][:8], short[pu["program"]], route, (">=" + fmt_bytes(pu["min_bytes"])) if pu["min_bytes"] else "standing",
                pu["premium_per_gb"], ("%d left" % pu["windows_left"]) if pu["program"] != "first_link" else "until %s" % time.strftime("%b %d", time.localtime(pu["deadline"])),
                usd(pu["escrow_usd"]), (" (%s of it top-ups)" % usd(pu["topups_usd"])) if pu["topups_usd"] else "",
                (" + %s match" % usd(pu["match_usd"])) if pu["match_usd"] else ""))
        for L in fd["links"]:
            out.append("link  %s -> %s (%s -> %s): %d windows, %s, paid %s, the Steward's %s this program window" % (
                L["from"], L["to"], L["payer"], L["hop"], L["windows"], fmt_bytes(L["bytes"]), usd(L["fees_usd"]), usd(L["take_usd"])))
        for pl in fd["map"]["places"]:
            out.append("place %-12s %s/%s  %s" % (pl["id"], pl["medium"], pl["range_class"], "linked" if pl["linked"] else "no link yet"))
        last = self.last_run()
        if last:
            out.append("last run  %s  posted %d, adjusted %d, skipped %d%s" % (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(last["t"])), len(last["posted"]), len(last.get("adjusted", [])), len(last["skipped"]),
                (": " + "; ".join(last["skipped"][:3])) if last["skipped"] else ""))
        return "\n".join(out)


def pct(x):
    v = x * 100
    return ("%.4f" % v).rstrip("0").rstrip(".") + "%"


# ---------------------------------------------------------------- cli

def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.2f %s" % (n, unit)
        n /= 1000.0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bwkit", description="Bandwidth Swap pilot receipt daemon and Mint (bsp/1)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen", help="make this node's ed25519 key (once) and print its NYM")
    sub.add_parser("nym", help="print this node's NYM")
    p = sub.add_parser("peer", help="manage the peers this node accepts")
    ps = p.add_subparsers(dest="pcmd", required=True)
    pa = ps.add_parser("add")
    pa.add_argument("name")
    pa.add_argument("nym")
    ps.add_parser("list")
    sub.add_parser("sample", help="take the baseline counter sample; the first window starts now")
    sub.add_parser("receipt", help="payer: make this hour's receipt, get the ack, send both to the Mint")
    sub.add_parser("flush", help="send what is waiting in the outbox to the Mint")
    an = sub.add_parser("announce", help="hop: sign this link's parameters (the config's `link` object) and register them with the Mint")
    an.add_argument("--since", type=int, help="the first window start the announcement governs (Unix seconds; default now)")
    an.add_argument("--print", dest="print_only", action="store_true", help="print the signed announcement instead of posting it")
    pt = sub.add_parser("patience", help="hop: the payers it has counted short, and whether it has stopped serving one")
    pt.add_argument("--reset", metavar="NYM", help="clear the count for this payer and serve it again")
    sv = sub.add_parser("serve", help="hop or mint: serve HTTP")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=None)
    sub.add_parser("status", help="what this node knows")
    sub.add_parser("links", help="mint: the link announcements it holds")
    sw = sub.add_parser("steward", help="the Network Steward, run on the Mint's host (paper, Section 7)")
    sws = sw.add_subparsers(dest="swcmd", required=True)
    sws.add_parser("keygen", help="make the Steward's own key (once) and print its NYM")
    sws.add_parser("nym", help="print the Steward's NYM")
    sws.add_parser("status", help="the balances in one line")
    sws.add_parser("book", help="the Steward's book: balances, open offers, links, the map")
    swr = sws.add_parser("run", help="run the three programs over the ledger and post what they size (the hourly timer)")
    swr.add_argument("--now", type=int, help="the clock to run with (tests)")
    sws.add_parser("feed", help="print the `steward` object the status report carries")
    sws.add_parser("map", help="the map: domains, places and which node is where")
    pu = sub.add_parser("purchase", help="purchases (spec, Section 15): a payer posts one, the Mint lists them")
    pus = pu.add_subparsers(dest="pucmd", required=True)
    pp = pus.add_parser("post", help="payer: sign a purchase on a route and register it with the Mint")
    pp.add_argument("--to", required=True, help="the hop, by peer name or NYM, or a place id")
    pp.add_argument("--min-bytes", type=int, required=True, help="the minimum rate, in bytes per window")
    pp.add_argument("--premium", type=float, required=True, help="the premium above the posted price, USD per GB")
    pp.add_argument("--windows", type=int, required=True, help="how many windows it pays for")
    pp.add_argument("--escrow", type=float, required=True, help="the prepaid value in USD (a line on the invoice)")
    pp.add_argument("--deadline", required=True, help="Unix seconds, or +Nd / +Nh from now")
    pp.add_argument("--print", dest="print_only", action="store_true", help="print the signed record instead of posting it")
    pus.add_parser("list", help="the purchases this node holds (the Mint's registry, or a payer's own postings)")
    pt2 = pus.add_parser("topup", help="payer: add escrow to a purchase of yours")
    pt2.add_argument("id")
    pt2.add_argument("--escrow", type=float, required=True)
    pc = pus.add_parser("close", help="payer: withdraw a purchase of yours; what it did not spend comes back")
    pc.add_argument("id")
    lg = sub.add_parser("ledger", help="mint: print the ledger")
    lg.add_argument("--disputes", action="store_true", help="only disputed settlements")
    lg.add_argument("--json", action="store_true")
    iv = sub.add_parser("invoice", help="mint: sum a month's settlements (YYYY-MM), with the Steward's share apart")
    iv.add_argument("month")
    vf = sub.add_parser("verify", help="check the signature on receipt files (no node state needed)")
    vf.add_argument("files", nargs="+")
    st = sub.add_parser("settle", help="resolve one window offline from a receipt file and an ack file")
    st.add_argument("receipt")
    st.add_argument("ack")
    st.add_argument("--link", help="the announcement in force (a JSON file); without it, the pilot defaults")
    st.add_argument("--price", type=float, default=DEFAULT_PRICE_USD_PER_GB, help="the default price when there is no announcement")
    st.add_argument("--fee", type=float, default=DEFAULT_STEWARD_FEE, help="the Steward's fraction f")
    rp = sub.add_parser("report", help="mint: post the ledger's public summary to the status page "
                                       "(BWKIT_STATUS_URL and BWKIT_STATUS_TOKEN, from /etc/bwkit/status.env)")
    rp.add_argument("--print", dest="print_only", action="store_true", help="print the report instead of posting it")
    pv = sub.add_parser("prove", help="mint: print the inclusion proofs for every settlement of an hour, against its commitment")
    pv.add_argument("window", help="the hour: Unix seconds (any moment in it) or YYYY-MM-DDTHH in local time")
    vp = sub.add_parser("verify-proof", help="check a proof file against the commitment it carries (no node state needed)")
    vp.add_argument("file")
    vp.add_argument("--mint", help="the Mint's NYM, when you know it: refuse a proof signed by any other key")
    rb = sub.add_parser("rebuild", help="rebuild a Mint's ledger from the payer's receipts.jsonl, the hop's acks.jsonl and the "
                                        "announcements alone (no node state needed)")
    rb.add_argument("receipts", help="the payer's receipts.jsonl")
    rb.add_argument("acks", help="the hop's acks.jsonl")
    rb.add_argument("links", nargs="?", help="links.jsonl, the announcements (the Mint's or the hop's copy); none means the pilot defaults")
    rb.add_argument("purchases", nargs="?", help="purchases.jsonl, the purchases (the Mint's copy); none means no premiums")
    rb.add_argument("--map", help="the map (map.json), needed for the Steward's place-routed purchases")
    rb.add_argument("--peers", help="peers.json, to put nodes at their places on the map")
    rb.add_argument("--ledger", help="write the rebuilt ledger here instead of printing it")
    rb.add_argument("--compare", metavar="MINT_LEDGER", help="diff the rebuilt settlements and commitments against this ledger.jsonl")
    rb.add_argument("--price", type=float, default=DEFAULT_PRICE_USD_PER_GB, help="the default price when no announcement is in force")
    rb.add_argument("--fee", type=float, default=DEFAULT_STEWARD_FEE, help="the Steward's fraction f")
    args = ap.parse_args(argv)

    if args.cmd == "verify":
        ok = True
        for fn in args.files:
            with open(fn) as f:
                env = json.load(f)
            try:
                if isinstance(env, dict) and env.get("kind") == "link":
                    check_link(env)
                    if not verify_link(env):
                        raise ValueError("bad signature")
                    print("%s: link by %s, %s %s, margin %s, since %d, hash %s, ok" % (
                        fn, env["relay"], env["price"], env["unit"], env["margin"], env["since"], link_hash(env)[:16]))
                    continue
                if isinstance(env, dict) and env.get("kind") == "purchase":
                    check_purchase(env)
                    if not verify_purchase(env):
                        raise ValueError("bad signature")
                    print("%s: purchase %s by %s (%s), %s -> %s, >= %d bytes a window, premium %s/GB, %d windows, escrow %s, ok" % (
                        fn, env["id"][:16], env["poster"], env["program"], env["route"]["from"], env["route"]["to"], env["min_bytes"],
                        env["premium_per_gb"], env["windows"], env["escrow"]))
                    continue
                signer = check_envelope(env)
                r = env["receipt"]
                print("%s: %s by %s, %d bytes, %d..%d, ok" % (fn, env["kind"], signer, r["bytes"], r["t_first"], r["t_last"]))
            except ValueError as e:
                ok = False
                print("%s: BAD: %s" % (fn, e))
        return 0 if ok else 1
    if args.cmd == "settle":
        with open(args.receipt) as f:
            rec = json.load(f)
        with open(args.ack) as f:
            ack = json.load(f)
        link = None
        if args.link:
            with open(args.link) as f:
                link = json.load(f)
            check_link(link)
            if not verify_link(link):
                raise SystemExit("%s: bad signature" % args.link)
        s = resolve(rec, ack, args.price, link=link, fee=args.fee)
        print(json.dumps(s, indent=1, sort_keys=True))
        return 0
    if args.cmd == "verify-proof":
        with open(args.file) as f:
            doc = json.load(f)
        ok, lines = check_proof(doc, args.mint)
        for line in lines:
            print("%s: %s" % (args.file, line))
        return 0 if ok else 1
    if args.cmd == "rebuild":
        receipts, bad_r = read_jsonl(args.receipts)
        acks, bad_a = read_jsonl(args.acks)
        links, bad_l = read_jsonl(args.links) if args.links else ([], [])
        purchases, bad_p = read_jsonl(args.purchases) if args.purchases else ([], [])
        names = (os.path.basename(args.receipts), os.path.basename(args.acks), os.path.basename(args.links or "links.jsonl"),
                 os.path.basename(args.purchases or "purchases.jsonl"))
        place_of = None
        if args.map:
            with open(args.map) as f:
                map_obj = json.load(f)
            peers = {}
            if args.peers:
                with open(args.peers) as f:
                    peers = json.load(f)
            place_of = place_lookup(map_obj, peers)
        entries, rep = rebuild(receipts, acks, links, args.price, args.fee, names=names, purchases=purchases, place_of=place_of)
        rep["bad"] = bad_r + bad_a + bad_l + bad_p + rep["bad"]
        for line in rep["bad"]:
            print("left out: " + line, file=sys.stderr)
        summary = ("rebuilt %d receipts and %d acks under %d announcements and %d purchases into %d settlements (%d disputed) in %d hours; "
                   "%d receipts without ack, %d acks without receipt, %d lines left out" % (
                       rep["receipts"], rep["acks"], rep["links"], rep["purchases"], rep["settlements"], rep["disputed"], rep["hours"],
                       rep["receipts_without_ack"], rep["acks_without_receipt"], len(rep["bad"])))
        if args.ledger:
            with open(args.ledger, "w") as f:
                for e in entries:
                    f.write(json.dumps(e, sort_keys=True) + "\n")
            print("%s: %s" % (args.ledger, summary))
        elif args.compare:
            print(summary)
        else:
            for e in entries:
                print(json.dumps(e, sort_keys=True))
            print(summary, file=sys.stderr)
        rc = 1 if rep["bad"] else 0
        if args.compare:
            mint_entries, bad_m = read_jsonl(args.compare)
            for line in bad_m:
                print("skipped: " + line, file=sys.stderr)
            agree, verdict, lines = compare(entries, [e for _, e in mint_entries])
            print(verdict)
            for line in lines:
                print("  " + line)
            rc = rc or (0 if agree else 1)
        return rc

    node = Node.from_env()
    if args.cmd == "keygen":
        print(node.keygen())
    elif args.cmd == "nym":
        print(node.nym)
    elif args.cmd == "peer":
        if args.pcmd == "add":
            node.peer_add(args.name, args.nym)
        for nym, name in node.peers().items():
            print("%s\t%s" % (name, nym))
    elif args.cmd == "sample":
        s = node.sample()
        print("baseline at %d: rx %d, tx %d" % (s["t"], s["rx"], s["tx"]))
    elif args.cmd == "receipt":
        node.receipt_run()
    elif args.cmd == "flush":
        sent, left = node.flush()
        print("sent %d, %d left" % (sent, left))
    elif args.cmd == "announce":
        rec, h = node.announce(since=args.since, post=not args.print_only)
        if args.print_only:
            print(json.dumps(rec, indent=1, sort_keys=True))
        else:
            print("announced %s: %s %s, margin %s, min_bytes %d, patience %d, since %d" % (
                h, rec["price"], rec["unit"], rec["margin"], rec["min_bytes"], rec["patience_windows"], rec["since"]))
    elif args.cmd == "patience":
        if node.role != "hop":
            raise SystemExit("only a hop keeps patience")
        if args.reset:
            node.patience_reset(args.reset)
            print("cleared %s" % args.reset)
        link = node.my_link()
        peers = node.peers()
        pat = node.patience_state()
        print("patience %d window%s (0 never declines), margin %s, min_bytes %d" % (
            link["patience_windows"], "" if link["patience_windows"] == 1 else "s", link["margin"], link["min_bytes"]))
        for nym, ps in sorted(pat.items()):
            print("%s (%s): %d short%s" % (peers.get(nym, "?"), nym, ps["short"],
                                           ", declined since %d" % ps["declined_at"] if ps.get("declined") else ""))
    elif args.cmd == "serve":
        node.serve(args.host, args.port)
    elif args.cmd == "status":
        print("%s (%s) %s, bwkit %s, profile %d" % (node.name, node.role, node.nym if node.priv else "no key", VERSION, PROTO))
        if node.role == "mint":
            node.load_index()
            no_ack, no_receipt = node.pending()
            print("ledger: %d receipts and acks, %d settlements (%d disputed), %d receipts without ack, %d acks without receipt" % (
                len(node.index["by_sig"]), len(node.index["settled"]),
                sum(1 for s in node.index["settled"].values() if s["status"] == "disputed"), len(no_ack), len(no_receipt)))
            last = node.index["last_commitment"]
            print("commitments: %d hours%s" % (len(node.index["commitments"]),
                  (", latest for hour %d: root %s over %d" % (last["window"], last["root"][:16], last["count"])) if last else ""))
            print("links: %d announcement%s, fee %s" % (len(node.index["links"]), "" if len(node.index["links"]) == 1 else "s",
                                                        node.config.get("steward_fee", DEFAULT_STEWARD_FEE)))
        else:
            s = node.state().get("sample")
            print("sample: %s" % (("t %d, rx %d, tx %d" % (s["t"], s["rx"], s["tx"])) if s else "none (run `bwkit sample`)"))
            print("outbox: %d waiting" % len(os.listdir(node.path("outbox"))))
            if node.role == "hop":
                link = node.my_link()
                print("link: %s %s, margin %s, min_bytes %d, patience %d, clock tolerance %d s, %s/%s" % (
                    link["price"], link["unit"], link["margin"], link["min_bytes"], link["patience_windows"],
                    link["clock_tolerance_s"], link["medium"], link["range_class"]))
                declined = [n for n, ps in node.patience_state().items() if ps.get("declined")]
                if declined:
                    print("declined payers: %s" % ", ".join(declined))
    elif args.cmd == "purchase":
        if args.pucmd == "post":
            if node.role != "payer":
                raise SystemExit("only a payer posts a purchase from the command line (the Steward posts its own)")
            d = args.deadline
            if d.startswith("+"):
                unit = {"d": 86400, "h": 3600, "w": 7 * 86400}.get(d[-1])
                if not unit or not d[1:-1].isdigit():
                    raise SystemExit("--deadline is Unix seconds or +Nd, +Nh, +Nw")
                deadline = int(time.time()) + int(d[1:-1]) * unit
            elif d.isdigit():
                deadline = int(d)
            else:
                raise SystemExit("--deadline is Unix seconds or +Nd, +Nh, +Nw")
            rec, pid = node.purchase_post(args.to, args.min_bytes, args.premium, args.windows, args.escrow, deadline, post=not args.print_only)
            if args.print_only:
                print(json.dumps(rec, indent=1, sort_keys=True))
            else:
                print("posted %s: %s -> %s, >= %d bytes a window, premium %s USD/GB, %d windows, escrow %s USD, until %s" % (
                    pid, rec["route"]["from"], rec["route"]["to"], rec["min_bytes"], rec["premium_per_gb"], rec["windows"], rec["escrow"],
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(rec["deadline"]))))
        elif args.pucmd in ("topup", "close"):
            if node.role != "payer":
                raise SystemExit("only a payer adjusts a purchase from the command line (the Steward adjusts its own)")
            rec, aid = node.purchase_adjust("top-up" if args.pucmd == "topup" else "close", args.id, getattr(args, "escrow", None))
            print("posted %s %s on %s" % (rec["kind"], aid, args.id))
        else:
            peers = node.peers()
            if node.role == "mint":
                node.load_index()
                book = node.index["book"]
                recs = [book.purchases[pid] for pid in book.order]
            else:
                book, recs = None, [r for r in node.load_purchases() if r.get("kind") == "purchase"]
            steward = node.steward_nym() if node.role == "mint" else None
            for rec in recs:
                u = book.use.get(rec["id"], {}) if book else {}
                who = "the Steward" if rec["poster"] == steward else peers.get(rec["poster"], rec["poster"][:20])
                print("%s  %-13s %s  %s -> %s%s  >=%s  %s/GB  %d window%s%s  escrow %s%s  until %s" % (
                    rec["id"][:16], rec["program"], who, rec["route"]["from"] if rec["route"]["from"] and not rec["route"]["from"].startswith("ed25519:") else who,
                    peers.get(rec["route"]["to"], rec["route"]["to"]), (" !via " + ",".join(rec["route"]["not_via"])) if rec["route"]["not_via"] else "",
                    fmt_bytes(rec["min_bytes"]), rec["premium_per_gb"], rec["windows"], "" if rec["windows"] == 1 else "s",
                    (", %d used" % u["windows"]) if u.get("windows") else "", book.total_escrow(rec) if book else rec["escrow"],
                    (", %s paid" % round(u.get("own", 0.0) + u.get("steward", 0.0), 6)) if u else "",
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(rec["deadline"])) + (", closed" if book and rec["id"] in book.closed else "")))
    elif args.cmd == "steward":
        st = Steward(node)
        if args.swcmd == "keygen":
            print(st.keygen())
        elif args.swcmd == "nym":
            print(st.nym)
        elif args.swcmd == "run":
            run = st.run(args.now)
            print("steward run at %d: income %s, escrow %s, posted %d (%s), adjusted %d (%s), skipped %d" % (
                run["t"], usd(run["income_usd"]), usd(run["escrow_usd"]), len(run["posted"]), ", ".join(x[:8] for x in run["posted"]),
                len(run["adjusted"]), ", ".join("%s %s" % (a["kind"], a["id"][:8]) for a in run["adjusted"]), len(run["skipped"])))
            for line in run["skipped"]:
                print("  skipped: " + line)
        elif args.swcmd == "book":
            print(st.book())
        elif args.swcmd == "feed":
            print(json.dumps(st.feed(), indent=1, sort_keys=True))
        elif args.swcmd == "status":
            fd = st.feed()
            print("steward %s: income %s, escrow %s, in open offers %s, paid %s, returned %s, held by the cap %s; %d purchases, last run %s" % (
                fd["nym"][:20] if fd["nym"] else "(no key)", usd(fd["income_usd"]), usd(fd["escrow_usd"]), usd(fd["in_offers_usd"]), usd(fd["paid_usd"]),
                usd(fd["refunded_usd"]), usd(fd["held_by_cap_usd"]), len(fd["purchases"]),
                time.strftime("%Y-%m-%d %H:%M", time.localtime(fd["last_run"])) if fd["last_run"] else "never"))
        elif args.swcmd == "map":
            m = node.load_map()
            peers = node.peers()
            for did, d in sorted(m.get("domains", {}).items()):
                print("domain %s: %s, unit %s, Mint %s" % (did, d.get("name", did), d.get("unit", "USD"), d.get("mint", "?")))
            for pid, pl in sorted(m.get("places", {}).items()):
                here = sorted(n for n, where in m.get("nodes", {}).items() if where == pid)
                print("place  %-12s %s  %s/%s%s  nodes: %s" % (pid, pl.get("name", pid), pl.get("medium"), pl.get("range_class"),
                                                           "" if pl.get("mapped", True) else " (not mapped)", ", ".join(here) or "none"))
            for n in sorted(peers.values()) + ([node.name] if node.name not in peers.values() else []):
                if n not in m.get("nodes", {}):
                    print("node   %s is on the peer list but at no place on the map" % n)
    elif args.cmd == "links":
        if node.role != "mint":
            raise SystemExit("only the Mint holds announcements")
        peers = node.peers()
        for rec in node.load_links():
            print("%s  %s for %s: %s %s, margin %s, min_bytes %d, patience %d, %s/%s, since %s, hash %s" % (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(rec["since"])), peers.get(rec["relay"], rec["relay"]),
                "any payer" if rec["payer"] == "*" else peers.get(rec["payer"], rec["payer"]), rec["price"], rec["unit"],
                rec["margin"], rec["min_bytes"], rec["patience_windows"], rec["medium"], rec["range_class"], rec["since"], link_hash(rec)[:16]))
    elif args.cmd == "ledger":
        for e in node.ledger_lines():
            if args.disputes and not (e["kind"] == "settlement" and e["status"] == "disputed"):
                continue
            if args.json:
                print(json.dumps(e, sort_keys=True))
            elif e["kind"] == "commitment":
                print("%s  COMMIT    hour %s  root %s  %d settlement%s  %s" % (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(e["t"])),
                    time.strftime("%Y-%m-%d %H:00", time.localtime(e["window"])), e["root"][:16],
                    e["count"], "" if e["count"] == 1 else "s", "signed" if e.get("signature") else "unsigned"))
            elif e["kind"] == "settlement":
                print("%s  %s  %d..%d  payer %s  hop %s  settled %s  $%.6f%s" % (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(e["t_last"])), e["status"].upper(),
                    e["t_first"], e["t_last"], fmt_bytes(e["payer_bytes"]), fmt_bytes(e["hop_bytes"]),
                    fmt_bytes(e["settled_bytes"]), e["amount_usd"],
                    "  fee $%.6f  margin %s%s" % (e["fee_usd"], e["margin"], "" if e.get("link") else " (default)") if "fee_usd" in e else ""))
            else:
                r = e["receipt"]
                print("%s  %-7s from %s  %d..%d  %s  sig %s" % (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(e["received_at"])), e["kind"], e["from"],
                    r["t_first"], r["t_last"], fmt_bytes(r["bytes"]), e["sig"][:12]))
    elif args.cmd == "invoice":
        peers = node.peers()
        tot = node.invoice(args.month)
        if not tot:
            print("no settlements in %s" % args.month)
        fees = 0.0
        for (payer, hop), o in sorted(tot.items()):
            fees = round(fees + o["fee_usd"], 6)
            print("%s  %s pays %s: %d windows (%d disputed), %s, $%.4f%s: the relay's $%.4f, the Steward's $%.4f" % (
                args.month, peers.get(payer, payer), peers.get(hop, hop), o["windows"], o["disputed"],
                fmt_bytes(o["settled_bytes"]), o["amount_usd"],
                " + $%.4f premium on %d purchased window%s ($%.4f of it the Steward's)" % (
                    o["premium_usd"], o["purchases"], "" if o["purchases"] == 1 else "s", o["from_steward_usd"]) if o["purchases"] else "",
                o["relay_usd"], o["fee_usd"]))
            if o["refund_usd"]:
                print("%s    returned to %s on short windows: $%.4f" % (args.month, peers.get(payer, payer), o["refund_usd"]))
        if tot:
            print("%s  the Network Steward's share: $%.4f (f = %s of every settlement since 0.3.0; none before)" % (
                args.month, fees, node.config.get("steward_fee", DEFAULT_STEWARD_FEE)))
    elif args.cmd == "prove":
        if node.role != "mint":
            raise SystemExit("only the Mint proves")
        print(json.dumps(node.proof(parse_hour(args.window)), indent=1, sort_keys=True))
    elif args.cmd == "report":
        if node.role != "mint":
            raise SystemExit("only the Mint reports")
        if args.print_only:
            print(json.dumps(node.status_report(service=node.service_up()), indent=1, sort_keys=True))
            return 0
        try:
            status, body = node.report()
        except (urlerror.URLError, socket.timeout, OSError) as e:
            log("status page unreachable: %s" % e)
            return 1
        if not 200 <= status < 300:
            log("status page refused the report: %s %s" % (status, body))
            return 1
        log("status posted: %s" % json.dumps(body, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
