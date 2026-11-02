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
payer appends receipts.jsonl and acks.jsonl, the hop receipts.jsonl (the
payer's receipts it countersigned), acks.jsonl and links.jsonl, and both
append sent.jsonl with the Mint's answer to each
envelope, which carries the Mint's signed commitment when one was made.
"""

import argparse
import base64
import fractions
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

VERSION = "0.5.1"
IMPL = "bwkit/" + VERSION               # `impl` in every envelope: informational, never the profile
PROTO = 1                               # the profile every record this kit writes carries (spec, Section 14)
PROTOS = (1,)                           # the profiles it accepts; a record without `proto` is profile 1
FIELDS = ("payer", "hop", "hop_index", "merkle_root", "bytes", "t_first", "t_last", "prev_sig")
DOMAIN = b"bandwidthswap/receipt/v1\n"   # prefixed to every signed receipt and ack
COMMIT_DOMAIN = b"bandwidthswap/commitment/v1\n"   # prefixed to every signed commitment
LINK_DOMAIN = b"bandwidthswap/link/v1\n"           # prefixed to every signed link announcement
STATUS_DOMAIN = b"bandwidthswap/status/v1\n"       # prefixed to the Mint's signed status record, the public feed's (spec, Section 9)
MAP_DOMAIN = b"bandwidthswap/map/v1\n"             # prefixed to the signed map a feed lists Mints by (spec, Section 9)
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
MICRO = 10 ** 6                         # prepayment amounts are integers in micro-units of USD (spec, Section 16)
DEPOSIT_DOMAIN = b"bandwidthswap/deposit/v1\n"      # value entering a payer's balance at the Mint, signed by the Mint
LOCK_DOMAIN = b"bandwidthswap/lock/v1\n"            # a payer's reservation of one window's value with one hop
LOCK_MINT_DOMAIN = b"bandwidthswap/lock/v1/mint\n"  # the Mint's countersignature over the lock, sig included
CLAIM_DOMAIN = b"bandwidthswap/claim/v1\n"          # a hop's count for a window the payer never closed
DEPOSIT_FIELDS = ("kind", "proto", "id", "mint", "payer", "amount", "unit", "posted_at", "note", "sig")
LOCK_FIELDS = ("kind", "proto", "id", "payer", "hop", "t_first", "amount", "unit", "carry", "link", "posted_at", "sig")
LOCK_MINT_FIELDS = ("mint", "mint_sig")             # added by the Mint when it registers a lock
CLAIM_FIELDS = ("kind", "proto", "id", "hop", "payer", "lock", "bytes", "meter", "posted_at", "sig")
PREPAY_KINDS = ("deposit", "lock", "claim")
LOCK_AHEAD_S = 3600                     # a lock names the window about to open: t_first within one window of the Mint's clock
DEFAULT_STEWARD = {                     # the Steward's parameters (paper, Section 7; the mockup's values), under "steward" in the Mint's config
    "m": 0.04, "margin": 0.01, "program_window_s": 7 * 86400, "first_link_usd": 1.0, "first_link_premium": 0.05,
    "min_payer_age_s": 86400, "bounty_deadline_s": 7 * 86400,
}
DEFAULT_MAP_PATH = "/etc/bwkit/map.json"   # BWKIT_MAP: the membership's map of domains, places and which node is where
DASHBOARD_DIRS = ("/usr/local/share/bwkit/dashboard",         # the node's own page (the kit's dashboard/), served at / and /dash/
                  os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard"))
DASHBOARD_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                   ".mjs": "application/javascript; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml"}
DASHBOARD_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")      # a file of the directory itself, never a path
SNAPSHOT_PROOFS = {"mint": 24, "hop": 2, "payer": 2}  # how many committed hours' proofs a snapshot embeds: the Mint's own are cheap, a node's come over the wire
BSP_MODULE = """<script type="module">import * as bsp from './dash/bsp.js'; window.bsp = bsp;</script>"""   # index.html's verifier line
DECLINE_WORDS = ("clock", "patience", "since", "domain", "profile", "replay", "route", "duplicate", "balance")   # 409 reasons, the word before the colon
TOKEN_RE = re.compile(r"^[a-z0-9-]{1,32}$")   # a medium or range class
HTTP_TIMEOUT_S = 10
MINT_INFO_S = (600, 60)                 # a hop or payer re-reads its Mint's name and NYM this often: after an answer, after none
MINT_INFO_TIMEOUT_S = 2                 # and waits this long for it, so its own /status is not held up by a Mint that is away
HOP_FLUSH_DELAY_S = 5                   # the hop sends its own copy of an ack this long after answering the payer
FLUSH_EVERY_S = 300                     # and retries whatever is left in its outbox this often
MAX_BODY = 1 << 20
STATUS_TIMEOUT_S = 10                   # posting the status report
REPORT_AFTER_COMMIT_S = 10              # a serving Mint posts a status report this long after a commitment, once for all of them
HISTORY_HOURS = 12                      # the status report carries each node's windows of the last twelve hours
HISTORY_MAX = 36                        # at most this many per node (the forced tests made several in one hour)
REPORT_MAX = 64 * 1024                  # the status Worker refuses a larger body
HEALTH = {                              # the checks every view shows (dashboard/PLAN.md 1.3), as /status.health
    "clock_s": 1.0,                     # a clock this far from its source is bad: receipts fail the link's tolerance soon after
    "disk_bytes": 500_000_000,          # less free disk than this for the ledger is bad
    "ack_wait_s": 600,                  # a receipt without its ack for this long needs attention
    "lost_after_s": 7200,               # two windows without an ack: the hop never countersigned, the window is lost
    "lost_shown_s": 86400,              # a lost window is listed for a day; after that the ledger and the open-windows table hold it
    "commit_slack_s": 300,              # an hour is committed within five minutes of closing
    "report_s": 1500,                   # the public status is posted every ten minutes and after each commitment; twenty-five without one is a warning
    "gap_windows": 24,                  # the gap is judged as the median over this many of a link's newest windows
    "gap_min_windows": 3,               # and not before this many
}
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


# ---------------------------------------------------------------- the status record and the map (the public feed; spec, Section 9, informative)

def status_canon(rec):
    """The signed bytes of a status record: the domain string and the canonical JSON of the record without sig."""
    return STATUS_DOMAIN + canon_json({k: v for k, v in rec.items() if k != "sig"}).encode()


def sign_status(priv, body):
    return b64u(priv.sign(STATUS_DOMAIN + canon_json(body).encode()))


def verify_status(rec):
    """True when rec carries the signature of the Mint it names in `mint`."""
    try:
        pub_of_nym(rec["mint"]).verify(b64u_dec(rec["sig"]), status_canon(rec))
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


def map_canon(rec):
    """The signed bytes of a map record: the domain string and the canonical JSON of the record without sig."""
    return MAP_DOMAIN + canon_json({k: v for k, v in rec.items() if k != "sig"}).encode()


def sign_map(priv, body):
    return b64u(priv.sign(MAP_DOMAIN + canon_json(body).encode()))


def verify_map(rec):
    """True when rec carries the signature of the Mint it names in `by`."""
    try:
        pub_of_nym(rec["by"]).verify(b64u_dec(rec["sig"]), map_canon(rec))
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


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


def invoice_sums(entries, month):
    """The sums `bwkit invoice` prints, per (payer, hop), over the settlements of a month (YYYY-MM, local time, by t_last).

    A settlement from before profile 1 carries no fee, so none is taken from it. The Mint sums its ledger; a hop sums
    the ledger it rebuilt from its own copies (`bwkit owed`), and the two agree or the invoice is wrong.
    """
    out = {}
    for e in entries:
        if e.get("kind") != "settlement":
            continue
        if time.strftime("%Y-%m", time.localtime(e["t_last"])) != month:
            continue
        k = (e["payer"], e["hop"])
        o = out.setdefault(k, {"windows": 0, "disputed": 0, "settled_bytes": 0, "amount_usd": 0.0, "fee_usd": 0.0, "relay_usd": 0.0,
                               "premium_usd": 0.0, "refund_usd": 0.0, "from_steward_usd": 0.0, "purchases": 0,
                               "claimed": 0, "covered": 0, "drawn": 0, "credit_usd": 0.0})
        o["windows"] += 1
        o["disputed"] += 1 if e["status"] == "disputed" else 0
        o["claimed"] += 1 if e["status"] == "claimed" else 0
        if e.get("lock"):
            o["covered"] += 1
            o["drawn"] += int(e.get("debit") or 0)
        else:
            o["credit_usd"] = round(o["credit_usd"] + e["amount_usd"] + (e.get("premium_usd") or 0.0), 6)
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


def invoice_pair_line(month, payer_name, hop_name, o):
    """One pair's line of the invoice, the form `parse_invoice_line` reads back."""
    return "%s  %s pays %s: %d windows (%d disputed), %s, $%.4f%s: the relay's $%.4f, the Steward's $%.4f" % (
        month, payer_name, hop_name, o["windows"], o["disputed"],
        fmt_bytes(o["settled_bytes"]), o["amount_usd"],
        " + $%.4f premium on %d purchased window%s ($%.4f of it the Steward's)" % (
            o["premium_usd"], o["purchases"], "" if o["purchases"] == 1 else "s", o["from_steward_usd"]) if o["purchases"] else "",
        o["relay_usd"], o["fee_usd"])


INVOICE_LINE_RE = re.compile(
    r"^(?P<month>\d{4}-\d{2})  (?P<payer>\S+) pays (?P<hop>\S+): (?P<windows>\d+) windows \((?P<disputed>\d+) disputed\), "
    r"(?P<bytes>[0-9.]+ [KMGT]?B), \$(?P<amount>[0-9.]+)"
    r"(?: \+ \$(?P<premium>[0-9.]+) premium on (?P<purchases>\d+) purchased windows? \(\$(?P<from_steward>[0-9.]+) of it the Steward's\))?"
    r": the relay's \$(?P<relay>[0-9.]+), the Steward's \$(?P<fee>[0-9.]+)$")


def parse_invoice_line(line):
    """A pair's line of an invoice back into its figures (the strings as printed), or None for any other line."""
    m = INVOICE_LINE_RE.match(line.rstrip("\n"))
    if not m:
        return None
    d = m.groupdict()
    return {"month": d["month"], "payer": d["payer"], "hop": d["hop"], "windows": int(d["windows"]), "disputed": int(d["disputed"]),
            "bytes": d["bytes"], "amount": d["amount"], "premium": d["premium"] or "0.0000", "purchases": int(d["purchases"] or 0),
            "from_steward": d["from_steward"] or "0.0000", "relay": d["relay"], "fee": d["fee"]}


def invoice_prepaid_line(month, payer_name, deposits, drawn, covered, credit_usd, credit_windows, balance):
    """A payer's prepayment line of the invoice (spec, Section 16), the form `parse_prepaid_line` reads back. Amounts are micro-units but the credit."""
    return "%s  %s prepaid: deposits %s, drawn %s on %d covered window%s, balance %s; on credit $%.4f on %d window%s" % (
        month, payer_name, micro_usd(deposits), micro_usd(drawn), covered, "" if covered == 1 else "s", micro_usd(balance),
        credit_usd, credit_windows, "" if credit_windows == 1 else "s")


PREPAID_LINE_RE = re.compile(
    r"^(?P<month>\d{4}-\d{2})  (?P<payer>\S+) prepaid: deposits \$(?P<deposits>[0-9.]+), drawn \$(?P<drawn>[0-9.]+) on (?P<covered>\d+) covered windows?, "
    r"balance \$(?P<balance>-?[0-9.]+); on credit \$(?P<credit>[0-9.]+) on (?P<credit_windows>\d+) windows?$")


def parse_prepaid_line(line):
    """A payer's prepayment line back into its figures (the strings as printed), or None for any other line."""
    m = PREPAID_LINE_RE.match(line.rstrip("\n"))
    return m.groupdict() if m else None


def invoice_lines(tot, peers, month, fee, prepay=None, now=None):
    """The invoice's text from its sums: one line per payer and hop, a refund line under a pair with short windows, a prepayment
    line per payer that holds a balance or had a covered window (spec, Section 16), the Steward's line last."""
    lines = []
    if not tot:
        lines.append("no settlements in %s" % month)
    fees = 0.0
    for (payer, hop), o in sorted(tot.items()):
        fees = round(fees + o["fee_usd"], 6)
        lines.append(invoice_pair_line(month, peers.get(payer, payer), peers.get(hop, hop), o))
        if o["refund_usd"]:
            lines.append("%s    returned to %s on short windows: $%.4f" % (month, peers.get(payer, payer), o["refund_usd"]))
    if prepay is not None:
        now = now if now is not None else time.time()
        by_payer = {}
        for (payer, hop), o in tot.items():
            b = by_payer.setdefault(payer, {"drawn": 0, "covered": 0, "credit_usd": 0.0, "credit_windows": 0})
            b["drawn"] += o["drawn"]; b["covered"] += o["covered"]
            b["credit_usd"] = round(b["credit_usd"] + o["credit_usd"], 6); b["credit_windows"] += o["windows"] - o["covered"]
        for payer in sorted(set(by_payer) | set(prepay.payers())):
            b = by_payer.get(payer, {"drawn": 0, "covered": 0, "credit_usd": 0.0, "credit_windows": 0})
            bal = prepay.balance(payer, now)
            deposits = sum(d["amount"] for d in prepay.deposits if d["payer"] == payer and time.strftime("%Y-%m", time.localtime(d["posted_at"])) == month)
            if not (b["covered"] or deposits or bal["deposits"]):
                continue
            lines.append(invoice_prepaid_line(month, peers.get(payer, payer), deposits, b["drawn"], b["covered"], b["credit_usd"], b["credit_windows"], bal["balance"]))
    if tot:
        lines.append("%s  the Network Steward's share: $%.4f (f = %s of every settlement since 0.3.0; none before)" % (month, fees, fee))
    return "\n".join(lines) + "\n"


def previous_month(now=None):
    """The month before the one `now` falls in, YYYY-MM, in local time: what the invoice timer sums on the first."""
    t = time.localtime(now if now is not None else time.time())
    y, m = (t.tm_year, t.tm_mon - 1) if t.tm_mon > 1 else (t.tm_year - 1, 12)
    return "%04d-%02d" % (y, m)


def parse_moment(text):
    """A moment from the command line: Unix seconds, or YYYY-MM-DDTHH:MM (a space for the T, or the hour alone) in local time."""
    if text.isdigit():
        return int(text)
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H", "%Y-%m-%d %H:%M", "%Y-%m-%d %H"):
        try:
            return int(time.mktime(time.strptime(text, fmt)))
        except ValueError:
            continue
    raise SystemExit("a moment is Unix seconds or YYYY-MM-DDTHH:MM (local time)")


def settle_own_window(node, moment, price=DEFAULT_PRICE_USD_PER_GB, fee=DEFAULT_STEWARD_FEE):
    """`bwkit settle --window`: the window of this node's own copies that holds `moment`, settled by the Mint's rule.

    Reads receipts.jsonl, acks.jsonl and links.jsonl in the state directory
    and nothing else: the payer's receipt whose window holds the moment, the
    ack that chains to it, and the announcement in force at its start, each
    checked by its signature. Prints what each side signed and the settlement
    record as `resolve` makes it. Exit 0 when the window settles, 1 when a
    copy is missing or fails its check.
    """
    if node.role not in ("hop", "payer"):
        print("settle --window reads a hop's or a payer's own copies; the Mint has the ledger (`bwkit ledger`)")
        return 1
    names = node.peers()
    name_of = lambda nym: names.get(nym) or (node.name if nym == node.nym else nym[:20] + "...")
    when = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    rec_path, ack_path, link_path = node.path("receipts.jsonl"), node.path("acks.jsonl"), node.path("links.jsonl")
    if not os.path.exists(rec_path):
        print("no receipts.jsonl here: this node holds no receipt" + (" (a hop keeps the payer's receipts since bwkit 0.4.0)" if node.role == "hop" else ""))
        return 1
    receipts = [e for _, e in read_jsonl(rec_path)[0] if isinstance(e, dict) and e.get("kind") == "receipt"]
    held = [e for e in receipts if e["receipt"]["t_first"] <= moment <= e["receipt"]["t_last"]]
    if not held:
        print("no window of my copies holds %s (%d): the receipts here run %s" % (
            when(moment), moment, ("from %s to %s" % (when(min(e["receipt"]["t_first"] for e in receipts)),
                                                     when(max(e["receipt"]["t_last"] for e in receipts)))) if receipts else "nowhere, the file is empty"))
        return 1
    env = held[-1]
    r = env["receipt"]
    try:
        check_envelope(env)
    except ValueError as e:
        print("the receipt for %s..%s in my copies fails its check: %s" % (when(r["t_first"]), when(r["t_last"]), e))
        return 1
    acks = [e for _, e in read_jsonl(ack_path)[0] if isinstance(e, dict) and e.get("kind") == "ack"] if os.path.exists(ack_path) else []
    chained = [a for a in acks if a["receipt"].get("prev_sig") == env["sig"]]
    print("the window %s to %s, from my own copies:" % (when(r["t_first"]), when(r["t_last"])))
    print("  the receipt, signed by %s: %d bytes, ok" % (name_of(r["payer"]), r["bytes"]))
    if not chained:
        print("  no ack chains to it in my copies: the hop declined the receipt, or never countersigned it; the window is not settled")
        return 1
    ack = chained[-1]
    try:
        check_envelope(ack)
    except ValueError as e:
        print("  the ack in my copies fails its check: %s" % e)
        return 1
    print("  the ack, signed by %s: %d bytes, ok" % (name_of(ack["receipt"]["hop"]), ack["receipt"]["bytes"]))
    links = []
    if os.path.exists(link_path):
        for _, l in read_jsonl(link_path)[0]:
            try:
                check_link(l)
                if verify_link(l) and not profile_1_link(l):
                    links.append(l)
            except ValueError:
                continue
    link = link_in_force(links, r["payer"], r["hop"], r["t_first"])
    if link:
        print("  the announcement in force: %s, margin %s, %s %s, since %s" % (link_hash(link)[:16], link["margin"], link["price"], link["unit"], when(link["since"])))
    else:
        print("  no announcement of mine in force at its start: the pilot defaults, margin %s, %s USD/GB" % (PILOT_LINK["margin"], price))
    try:
        s = resolve(env, ack, price, link=link, fee=fee)
    except ValueError as e:
        print("  the two do not settle: %s" % e)
        return 1
    print("  settled at %d bytes, %s%s: $%.6f, the Steward's fee $%.6f (no purchase considered: premiums are the Mint's registry)" % (
        s["settled_bytes"], s["status"].upper(), (" beyond the margin" if s["status"] == "disputed" else " within the margin"), s["amount_usd"], s["fee_usd"]))
    print(json.dumps(s, indent=1, sort_keys=True))
    return 0


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


# ---------------------------------------------------------------- prepayment (spec, Section 16; step 1: the records and the registry)

def is_hex64(x):
    return isinstance(x, str) and len(x) == 64 and all(c in "0123456789abcdef" for c in x)


def prepay_body(rec):
    """The signed part of a prepayment record: without id and sig, and without the Mint's countersignature on a lock."""
    return {k: v for k, v in rec.items() if k not in ("id", "sig") + LOCK_MINT_FIELDS}


def prepay_id(body):
    """A prepayment record's id: the SHA-256 of the canonical JSON of the record without id and sig (and the Mint's countersignature)."""
    return hashlib.sha256(canon_json(prepay_body(body)).encode()).hexdigest()


def prepay_domain(kind):
    return {"deposit": DEPOSIT_DOMAIN, "lock": LOCK_DOMAIN, "claim": CLAIM_DOMAIN}[kind]


def prepay_signer(rec):
    """Who signs each kind: the Mint a deposit, the payer a lock, the hop a claim."""
    return rec[{"deposit": "mint", "lock": "payer", "claim": "hop"}[rec["kind"]]]


def sign_prepay(priv, body):
    return b64u(priv.sign(prepay_domain(body["kind"]) + canon_json(prepay_body(body)).encode()))


def verify_prepay(rec):
    """True when rec carries its signer's signature (the deposit's Mint, the lock's payer, the claim's hop)."""
    try:
        pub_of_nym(prepay_signer(rec)).verify(b64u_dec(rec["sig"]), prepay_domain(rec["kind"]) + canon_json(prepay_body(rec)).encode())
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


def sign_lock_mint(priv, rec):
    """The Mint's countersignature on a registered lock: over the lock with the payer's sig, without mint and mint_sig."""
    return b64u(priv.sign(LOCK_MINT_DOMAIN + canon_json({k: v for k, v in rec.items() if k not in LOCK_MINT_FIELDS}).encode()))


def verify_lock_mint(rec):
    """True when a lock carries the countersignature of the Mint it names."""
    try:
        pub_of_nym(rec["mint"]).verify(b64u_dec(rec["mint_sig"]),
                                       LOCK_MINT_DOMAIN + canon_json({k: v for k, v in rec.items() if k not in LOCK_MINT_FIELDS}).encode())
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


def _check_micro(v, what, positive=True):
    if not is_int(v) or v < 0 or (positive and v == 0):
        raise ValueError("%s must be a %s integer, in micro-units" % (what, "positive" if positive else "non-negative"))


def check_prepay(rec):
    """Raise ValueError unless rec is a well-formed deposit, lock or claim with its own id (spec, Section 16). Signatures not checked."""
    if not isinstance(rec, dict) or rec.get("kind") not in PREPAY_KINDS:
        raise ValueError("kind must be one of %s" % ", ".join(PREPAY_KINDS))
    kind = rec["kind"]
    fields = {"deposit": DEPOSIT_FIELDS, "lock": LOCK_FIELDS, "claim": CLAIM_FIELDS}[kind]
    have = set(rec)
    if kind == "lock" and have >= set(LOCK_MINT_FIELDS):
        have -= set(LOCK_MINT_FIELDS)
    elif kind == "lock" and have & set(LOCK_MINT_FIELDS):
        raise ValueError("a registered lock carries both mint and mint_sig")
    if have != set(fields):
        raise ValueError("a %s has exactly the fields %s%s" % (kind, ", ".join(fields), " (plus mint and mint_sig once registered)" if kind == "lock" else ""))
    check_proto(rec)
    if not is_int(rec["posted_at"]) or rec["posted_at"] < 0:
        raise ValueError("posted_at must be a non-negative integer")
    if kind == "deposit":
        pub_of_nym(rec["mint"])
        pub_of_nym(rec["payer"])
        _check_micro(rec["amount"], "amount")
        if rec["unit"] != "USD":
            raise ValueError("unit must be USD in profile 1")
        if not isinstance(rec["note"], str) or len(rec["note"]) > 200:
            raise ValueError("note is a string of at most 200 characters")
    elif kind == "lock":
        pub_of_nym(rec["payer"])
        pub_of_nym(rec["hop"])
        if not is_int(rec["t_first"]) or rec["t_first"] < 0:
            raise ValueError("t_first must be a non-negative integer")
        _check_micro(rec["amount"], "amount")
        _check_micro(rec["carry"], "carry", positive=False)
        if rec["unit"] != "USD":
            raise ValueError("unit must be USD in profile 1")
        if rec["link"] is not None and not is_hex64(rec["link"]):
            raise ValueError("link is an announcement's hash, or null")
        if "mint" in rec:
            pub_of_nym(rec["mint"])
            if not isinstance(rec["mint_sig"], str):
                raise ValueError("mint_sig is a string")
    else:
        pub_of_nym(rec["hop"])
        pub_of_nym(rec["payer"])
        if not is_hex64(rec["lock"]):
            raise ValueError("lock is a lock's id")
        if not is_int(rec["bytes"]) or rec["bytes"] < 0:
            raise ValueError("bytes must be a non-negative integer")
        if rec["meter"] is not None and not isinstance(rec["meter"], dict):
            raise ValueError("meter is the hop's sample pair, or null")
    if rec["id"] != prepay_id(rec):
        raise ValueError("id is not the hash of the record")


def micro_usd(n):
    """An integer in micro-units as dollars, six places."""
    return "$%.6f" % (n / MICRO)


class PrepayBook:
    """The deposits, locks and claims a Mint holds and what the settlements have drawn on the locks, kept in order.

    Pure bookkeeping shared by the Mint and the rebuild (spec, Section 16). A
    lock is open from its registration until a settlement or a claim debits
    it, or until its claim window closes with neither, when it is released;
    the balance is deposits less open locks less debits, integers.
    """

    def __init__(self, lost_after_s=None):
        self.lost_after_s = HEALTH["lost_after_s"] if lost_after_s is None else lost_after_s
        self.ids, self.records = set(), []
        self.deposits, self.locks, self.lock_by_window, self.claims = [], {}, {}, {}
        self.debits = {}                      # lock id -> what a settlement or a claim drew, micro-units

    def add(self, rec):
        """A deposit, a lock or a claim, in registration order. Returns False when its id is already held."""
        if not isinstance(rec, dict) or rec.get("kind") not in PREPAY_KINDS or rec.get("id") in self.ids:
            return False
        self.ids.add(rec["id"])
        self.records.append(rec)
        if rec["kind"] == "deposit":
            self.deposits.append(rec)
        elif rec["kind"] == "lock":
            self.locks[rec["id"]] = rec
            self.lock_by_window[(rec["payer"], rec["hop"], rec["t_first"])] = rec
        else:
            self.claims[rec["lock"]] = rec
        return True

    def lock_for(self, payer, hop, t_first):
        return self.lock_by_window.get((payer, hop, t_first))

    def note(self, s):
        """A settlement happened: what it drew on its lock, if it had one."""
        if s.get("lock") and s.get("debit") is not None:
            self.debits[s["lock"]] = s["debit"]

    def claim_window(self, lock):
        """When a hop may claim a lock's window: from two windows after its start (the payer's receipt may be in its outbox) for two more."""
        return lock["t_first"] + self.lost_after_s, lock["t_first"] + 2 * self.lost_after_s

    def state(self, lock, now):
        """open, settled, claimed, or released (its claim window closed with no settlement and no claim)."""
        if lock["id"] in self.debits:
            return "claimed" if lock["id"] in self.claims else "settled"
        return "released" if now >= self.claim_window(lock)[1] else "open"

    def balance(self, payer, now):
        """A payer's balance, every amount an integer in micro-units."""
        now = int(now)
        mine = sorted((l for l in self.locks.values() if l["payer"] == payer), key=lambda l: (l["t_first"], l["id"]))
        deposits = sum(d["amount"] for d in self.deposits if d["payer"] == payer)
        states = {l["id"]: self.state(l, now) for l in mine}
        open_locks = [l for l in mine if states[l["id"]] == "open"]
        locked = sum(l["amount"] for l in open_locks)
        debited = sum(self.debits[l["id"]] for l in mine if l["id"] in self.debits)
        released = sum(l["amount"] for l in mine if states[l["id"]] == "released")
        return {"payer": payer, "unit": "USD", "deposits": deposits, "locked": locked, "debited": debited, "released": released,
                "balance": deposits - locked - debited,
                "open_locks": [{"id": l["id"], "hop": l["hop"], "t_first": l["t_first"], "amount": l["amount"]} for l in open_locks],
                "locks": [{"id": l["id"], "hop": l["hop"], "t_first": l["t_first"], "amount": l["amount"], "state": states[l["id"]],
                           "debit": self.debits.get(l["id"])} for l in mine]}

    def payers(self):
        return sorted({d["payer"] for d in self.deposits} | {l["payer"] for l in self.locks.values()})


def apply_lock(s, lock):
    """A settlement against a lock: it names the lock and draws the window's carriage and premium from it, in micro-units.

    Only a covered window carries these three fields; an uncovered one is the
    settlement of Section 8 unchanged, so a ledger from before Section 16 and
    its rebuild still agree.
    """
    s["lock"] = lock["id"]
    s["debit"] = int(round((s["amount_usd"] + s.get("premium_usd", 0.0)) * MICRO))
    s["covered"] = True
    return s


CLAIM_RULE = "the hop's count, up to what the payer locked; the payer never closed the window"


def claimed_settlement(lock, claim, link=None, price_usd_per_gb=DEFAULT_PRICE_USD_PER_GB, fee=DEFAULT_STEWARD_FEE, now=None):
    """The settlement of a window the payer never closed, from the hop's claim on the lock (spec, Section 16).

    The hop is paid for what it counted, up to what the payer locked at the
    link's price; there is no payer count and no receipt, and the record says
    so in its rule. Pure, like resolve.
    """
    now = int(now if now is not None else time.time())
    p = link_params(link)
    price = p["price"] if link else price_usd_per_gb
    cap = int(fractions.Fraction(lock["amount"]) * 10 ** 9 / (fractions.Fraction(str(price)) * MICRO)) if price > 0 else claim["bytes"]   # exact: the Mint and a rebuild agree
    settled = min(claim["bytes"], cap)
    amount = round(settled / 1e9 * price, 6)
    s = {"kind": "settlement", "proto": PROTO, "t": now,
         "payer": lock["payer"], "hop": lock["hop"], "hop_index": 1, "t_first": lock["t_first"], "t_last": lock["t_first"] + 3600,
         "payer_bytes": None, "hop_bytes": claim["bytes"], "settled_bytes": settled, "status": "claimed", "rule": CLAIM_RULE,
         "receipt_sig": None, "ack_sig": None, "claim_sig": claim["sig"],
         "price_usd_per_gb": price, "amount_usd": amount, "steward_fee": fee, "fee_usd": round(amount * fee, 6),
         "margin": p["margin"], "min_bytes": p["min_bytes"], "link": link_hash(link) if link else None}
    return apply_lock(s, lock)


def settlement_key(s):
    """What a settlement is keyed by in an index: the receipt it settles, or the claim it settles when the payer never closed the window."""
    return s.get("receipt_sig") or s.get("claim_sig")


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
            names=("receipts.jsonl", "acks.jsonl", "links.jsonl", "purchases.jsonl", "prepay.jsonl"), purchases=(), place_of=None,
            steward_margin=DEFAULT_STEWARD["margin"], prepay=()):
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
    pbook = PrepayBook()
    for i, rec in prepay:
        try:
            check_prepay(rec)
            if not verify_prepay(rec):
                raise ValueError("bad signature")
            if rec["kind"] == "lock" and ("mint" not in rec or not verify_lock_mint(rec)):
                raise ValueError("a lock in the registry carries the Mint's countersignature")
        except ValueError as e:
            bad.append("%s line %d: %s" % (names[4] if len(names) > 4 else "prepay.jsonl", i, e))
            continue
        pbook.add(rec)
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
    rep = {"receipts": len(good["receipt"]), "acks": len(good["ack"]), "links": len(good["link"]), "purchases": len(book.order), "prepay": len(pbook.records),
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
        lock = pbook.lock_for(r["payer"], r["hop"], r["t_first"])
        if lock is not None and lock["id"] not in pbook.debits:
            apply_lock(s, lock)
        book.note(s)
        pbook.note(s)
        entries.append(s)
        rep["settlements"] += 1
        rep["disputed"] += 1 if s["status"] == "disputed" else 0
        by_hour.setdefault(commit_hour(s), []).append(s)
    # a claim on a lock whose window never settled: the claimed settlement, in the window's place among the others
    claimed = []
    for lock_id, claim in sorted(pbook.claims.items(), key=lambda kv: (pbook.locks.get(kv[0], {}).get("t_first", 0), kv[0])):
        lock = pbook.locks.get(lock_id)
        if lock is None or lock_id in pbook.debits:
            continue
        opens, closes = pbook.claim_window(lock)
        if not opens <= claim["posted_at"] < closes:
            bad.append("%s: claim %s is outside its lock's claim window (%d..%d)" % (names[4] if len(names) > 4 else "prepay.jsonl", claim["id"][:12], opens, closes))
            continue
        s = claimed_settlement(lock, claim, link_in_force(good["link"], lock["payer"], lock["hop"], lock["t_first"]), price_usd_per_gb, fee, claim["posted_at"])
        pbook.note(s)
        claimed.append(s)
        rep["settlements"] += 1
        by_hour.setdefault(commit_hour(s), []).append(s)
    if claimed:
        for s in claimed:                                            # after the receipts and acks it follows in time, before later ones
            at = next((i for i, e in enumerate(entries) if e.get("kind") == "settlement" and settlement_order(e) > settlement_order(s)), len(entries))
            entries.insert(at, s)
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


# The health texts are the page's own wording (dashboard/rules.js, attention), so a verdict reads the same wherever it is
# shown and the page can drop its own copy of one the node already made. These format as the page's F.* do.

def page_tstamp(t):
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "–"


def page_dur(s):
    s = max(0, int(round(s)))
    if s < 90:
        return "%d s" % s
    if s < 5400:
        return "%d min" % int(s / 60.0 + 0.5)
    if s < 172800:
        return "%.1f h" % (s / 3600.0)
    return "%d d" % int(s / 86400.0 + 0.5)


def page_ago(t, now):
    return page_dur(now - t) + " ago" if t else "never"


def page_bytes(n):
    if n is None:
        return "–"
    if n >= 1e9:
        return "%.2f GB" % (n / 1e9)
    if n >= 1e6:
        return "%.2f MB" % (n / 1e6)
    if n >= 1e3:
        return "%.1f kB" % (n / 1e3)
    return "%d B" % n


def page_pct(x, d=1):
    return "%.*f%%" % (d, 100 * x)


def gap_of(payer_bytes, hop_bytes):
    return (hop_bytes - payer_bytes) / float(payer_bytes) if payer_bytes else 0.0


def median(xs):
    """The page's F.median: the middle of the sorted list, the upper one of an even count."""
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[len(s) // 2]


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
        return {settlement_key(e): e for e in entries if e.get("kind") == "settlement"}

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
        m = (merkle_root([my_leaf[settlement_key(s)] for s in ss]).hex(), len(ss)) if ss else None
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
        self._serving = False                 # set by serve(): only a serving Mint posts a report after a commitment
        self._report_timer = None
        self._report_lock = threading.Lock()

    @classmethod
    def from_env(cls):
        directory = os.environ.get("BWKIT_DIR", "/var/lib/bwkit")
        cfg = os.environ.get("BWKIT_CONFIG", "/etc/bwkit/config.json")
        try:
            with open(cfg) as f:
                return cls(directory, json.load(f))
        except OSError as e:
            raise SystemExit("could not read the config %s: %s" % (cfg, e.strerror or e))
        except ValueError as e:
            # An operator edits this file by hand (phase0/NEIGHBOR.md): the line, not a traceback.
            raise SystemExit("the config %s is not JSON: %s. Put it back to shape (every value but the last in a block ends with a comma, "
                             "numbers bare, words in double quotes), then restart bwkit." % (cfg, e))

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

    def file_bytes(self, name):
        """One of this node's files as served: its bytes, or none when it does not exist yet."""
        p = self.path(name)
        if not os.path.exists(p):
            return b""
        with open(p, "rb") as f:
            return f.read()

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
        # Two processes may flush the same outbox at once (the hourly `bwkit receipt` and the radio sidecar's sweep on a
        # gateway, Nov 1, 2026): each posts the record, the Mint answers the second with 409 duplicate, and the one that
        # moves the file second finds it gone. The lock is per process, so the move tolerates that: the record was sent.
        names = sorted(os.listdir(self.path("outbox")))
        sent = 0

        def moved(name, dest):
            try:
                os.replace(self.path("outbox", name), self.path("sent", dest))
                return True
            except FileNotFoundError:
                return False                                    # another flusher of this outbox moved it first

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
                if moved(name, name):
                    self.append("sent.jsonl", {"t": int(time.time()), "file": name, "mint": body})
                sent += 1
            elif status == 409 and isinstance(body, dict) and body.get("error") == "duplicate":
                moved(name, name)
                sent += 1
            else:
                log("mint refused %s: %s %s" % (name, status, body))
                moved(name, name + ".refused")
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
        held = self.held_ack(env["sig"])
        if held:
            return held                             # asked twice (a retry, or once by HTTP and once by radio): the same ack, not a second one
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
            self.append("receipts.jsonl", env)        # the payer's receipt as it came, beside our ack: both counts, for our own page
            self.append("acks.jsonl", ack)
            self.outbox_put(ack)
            ps = self.note_short(signer, short, now)
        log("ack for %s %d..%d: payer says %d, we say %d%s" % (env.get("from"), r["t_first"], r["t_last"], r["bytes"], nbytes,
            " (short beyond the margin, %d so far)" % ps["short"] if short else ""))
        if self.config.get("mint"):
            self.flush_later(HOP_FLUSH_DELAY_S)   # our own copy, in case the payer drops an ack it dislikes
        return ack

    def held_ack(self, receipt_sig):
        """Hop: the acknowledgement this node already signed for the receipt with this signature, or None."""
        p = self.path("acks.jsonl")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    a = json.loads(line)
                except ValueError:
                    continue
                if a.get("kind") == "ack" and a.get("receipt", {}).get("prev_sig") == receipt_sig:
                    return a
        return None

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
        ix["prepay"] = PrepayBook()
        for rec in self.load_prepay(skip_partial):
            ix["prepay"].add(rec)
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
            ix["settled"][settlement_key(entry)] = entry
            ix["book"].note(entry)
            ix["prepay"].note(entry)
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

    # prepayment (spec, Section 16; step 1 of the build: the records and the registry, the balance; settlement, expiry and the
    # claimed settlement are step 2, the payer's lock step 3, the hop's gate step 4)

    def load_prepay(self, skip_partial=False):
        return self.read_lines("prepay.jsonl", skip_partial)

    def prepay_balance(self, payer, now=None):
        """A payer's balance at this Mint (PrepayBook.balance): deposits less open locks less debits, integers in micro-units."""
        return self.index["prepay"].balance(payer, now if now is not None else time.time())

    def prepay_payers(self):
        """Every payer with a deposit or a lock at this Mint."""
        return self.index["prepay"].payers()

    def register_prepay(self, rec, now=None):
        """The Mint's /prepay (spec, Section 16): a deposit, a lock or a claim. Appends it to prepay.jsonl, returns the record as stored.

        A lock comes back with the Mint's countersignature (mint, mint_sig), which is what the payer carries to the hop.
        """
        now = int(now if now is not None else time.time())
        if self.index is None:
            self.load_index()
        check_prepay(rec)
        if not verify_prepay(rec):
            raise ValueError("bad signature")
        peers = self.peers()
        p = self.index["prepay"]
        claimed = None
        with self.lock:
            if rec["id"] in p.ids:
                raise Decline("duplicate")
            if rec["posted_at"] < now - CLOCK_TOLERANCE_S:
                raise Decline("since: posted_at %d is in the past (the Mint's clock is %d); a %s cannot be backdated" % (rec["posted_at"], now, rec["kind"]))
            if rec["kind"] == "deposit":
                if rec["mint"] != self.nym:
                    raise Decline("unknown mint %s: a deposit is signed by this Mint, %s" % (rec["mint"][:20], self.nym[:20]))
                if rec["payer"] not in peers:
                    raise Decline("unknown payer %s" % rec["payer"])
                stored = rec
            elif rec["kind"] == "lock":
                if "mint" in rec:
                    raise Decline("route: a lock is posted before the Mint has countersigned it")
                if rec["payer"] not in peers:
                    raise Decline("unknown payer %s" % rec["payer"])
                if rec["hop"] not in peers:
                    raise Decline("unknown hop %s" % rec["hop"])
                if not now - CLOCK_TOLERANCE_S <= rec["t_first"] <= now + LOCK_AHEAD_S + CLOCK_TOLERANCE_S:
                    raise Decline("since: a lock names the window about to open; t_first %d against the Mint's clock %d" % (rec["t_first"], now))
                if p.lock_for(rec["payer"], rec["hop"], rec["t_first"]) is not None:
                    raise Decline("duplicate: a lock for that window with that hop is held")
                link = link_in_force(self.index["links"], rec["payer"], rec["hop"], rec["t_first"])
                want = link_hash(link) if link else None
                if rec["link"] != want:
                    raise Decline("route: the announcement in force at %d is %s, not %s" % (rec["t_first"], want[:16] if want else "none", rec["link"][:16] if rec["link"] else "none"))
                bal = self.prepay_balance(rec["payer"], now)
                if bal["balance"] < rec["amount"]:
                    raise Decline("balance: %s on hand, %s asked (deposits %s, locked %s, debited %s)" % (
                        micro_usd(bal["balance"]), micro_usd(rec["amount"]), micro_usd(bal["deposits"]), micro_usd(bal["locked"]), micro_usd(bal["debited"])))
                stored = dict(rec, mint=self.nym)
                stored["mint_sig"] = sign_lock_mint(self.priv, stored)
            else:
                lock = p.locks.get(rec["lock"])
                if lock is None:
                    raise Decline("route: no lock %s is held" % rec["lock"][:16])
                if rec["hop"] != lock["hop"] or rec["payer"] != lock["payer"]:
                    raise Decline("route: the claim's hop and payer are not the lock's")
                if rec["lock"] in p.claims:
                    raise Decline("duplicate: that lock is claimed")
                if lock["id"] in p.debits or any(s["payer"] == lock["payer"] and s["hop"] == lock["hop"] and s["t_first"] == lock["t_first"]
                                                  for s in self.index["settled"].values()):
                    raise Decline("since: the window starting at %d settled; nothing to claim" % lock["t_first"])
                opens, closes = p.claim_window(lock)
                if rec["posted_at"] < opens:
                    raise Decline("since: a claim waits %d s after the window's start (%d), in case the payer's receipt is in its outbox" % (
                        HEALTH["lost_after_s"], lock["t_first"]))
                if rec["posted_at"] >= closes or now >= closes:
                    raise Decline("since: the claim window on that lock closed at %d and its amount went back to the payer" % closes)
                stored = rec
                link = link_in_force(self.index["links"], lock["payer"], lock["hop"], lock["t_first"])
                claimed = claimed_settlement(lock, rec, link, self.config.get("price_usd_per_gb", DEFAULT_PRICE_USD_PER_GB),
                                             self.config.get("steward_fee", DEFAULT_STEWARD_FEE), now)
            self.append("prepay.jsonl", stored)
            p.add(stored)
            if claimed is not None:
                self.ledger_append(claimed)                        # the window settles on the hop's count, up to the lock, and joins its hour
                self.commit(commit_hour(claimed), now)
        who = lambda n: peers.get(n, n[:20])
        if rec["kind"] == "deposit":
            log("deposit %s registered: %s for %s (%s); balance %s" % (rec["id"][:16], micro_usd(rec["amount"]), who(rec["payer"]), rec["note"],
                                                                      micro_usd(self.prepay_balance(rec["payer"])["balance"])))
        elif rec["kind"] == "lock":
            log("lock %s registered: %s by %s with %s for the window at %d, countersigned; balance %s" % (
                rec["id"][:16], micro_usd(rec["amount"]), who(rec["payer"]), who(rec["hop"]), rec["t_first"], micro_usd(self.prepay_balance(rec["payer"])["balance"])))
        else:
            log("claim %s registered: %s counts %d bytes on lock %s of %s; settled claimed at %d bytes, %s drawn" % (
                rec["id"][:16], who(rec["hop"]), rec["bytes"], rec["lock"][:16], who(rec["payer"]), claimed["settled_bytes"], micro_usd(claimed["debit"])))
        return stored

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
        lock = ix["prepay"].lock_for(r["payer"], r["hop"], r["t_first"])
        if lock is not None and lock["id"] not in ix["prepay"].debits:
            apply_lock(s, lock)                                   # the window was covered: its value is drawn from the lock (spec, Section 16)
        self.ledger_append(s)
        log("%s %s..%d: payer %d, hop %d, settled %d bytes ($%.6f, fee $%.6f, margin %s%s%s)" % (
            s["status"], s["t_first"], s["t_last"], s["payer_bytes"], s["hop_bytes"], s["settled_bytes"], s["amount_usd"],
            s["fee_usd"], s["margin"], " from link %s" % s["link"][:16] if s["link"] else ", the default",
            "; purchase %s: premium $%.6f (poster $%.6f, Steward $%.6f), $%.6f returned" % (
                s["purchase"][:16], s["premium_usd"], s["from_poster_usd"], s["from_steward_usd"], s["refund_usd"]) if s.get("purchase") else "")
            + ("; lock %s: %s drawn" % (s["lock"][:16], micro_usd(s["debit"])) if s.get("lock") else ""))
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
        self.report_later()                   # the hour shows on the public feed within seconds, not at the next heartbeat
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

    def ledger_text(self, since=None):
        """The ledger as the file holds it (JSONL bytes); with `since`, only the lines received at or after that second.

        A receipt or ack is placed by `received_at`, a settlement or
        commitment by its `t`. The page keeps what it has and asks for the
        rest; the same second can come back twice, and the page drops the
        copy.
        """
        p = self.path("ledger.jsonl")
        if not os.path.exists(p):
            return b""
        with open(p, "rb") as f:
            data = f.read()
        if not since:
            return data
        out = []
        for line in data.split(b"\n"):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue                                     # a half-written last line; the next request has it whole
            if (e.get("received_at") or e.get("t") or 0) >= since:
                out.append(line)
        return b"\n".join(out) + (b"\n" if out else b"")

    def pending(self):
        """Receipts with no ack yet, and acks with no receipt."""
        if self.index is None:
            self.load_index()
        ix = self.index
        no_ack = [e for s, e in ix["by_sig"].items() if e["kind"] == "receipt" and s not in ix["acks_by_prev"]]
        no_receipt = [e for e in ix["acks_by_prev"].values() if e["receipt"]["prev_sig"] not in ix["by_sig"]]
        return no_ack, no_receipt

    def invoice(self, month):
        """Sum the settlements of a month (YYYY-MM) per payer and hop, in local time, with the Steward's share apart (invoice_sums)."""
        return invoice_sums(self.ledger_lines(), month)

    def invoice_text(self, month, now=None):
        """The invoice as `bwkit invoice` prints it: one line per payer and hop, a refund line under a pair with short windows,
        a prepayment line per payer with a balance or a covered window, the Steward's line last."""
        if self.index is None:
            self.load_index(skip_partial=True)
        return invoice_lines(self.invoice(month), self.peers(), month, self.config.get("steward_fee", DEFAULT_STEWARD_FEE),
                             prepay=self.index["prepay"], now=now)

    def invoice_save(self, month, now=None):
        """Write the month's invoice to invoices/<month>.txt (bwkit-invoice.timer does this on the first of the next month) and return the path.

        The file is the text with one line added saying when it was written; a second run rewrites it the same.
        """
        text = self.invoice_text(month) + "written %s by bwkit %s on %s\n" % (
            time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(now if now is not None else time.time())), VERSION, self.config.get("name", "the Mint"))
        d = self.path("invoices")
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, month + ".txt.tmp")
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, os.path.join(d, month + ".txt"))
        return os.path.join(d, month + ".txt")

    def invoice_saved(self, month):
        """The saved invoice's text, or None when the month has none."""
        if not re.match(r"^\d{4}-\d{2}$", month):
            return None
        p = self.path("invoices", month + ".txt")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return f.read()

    def invoice_marks(self, month):
        """The paid marks kept beside a month's invoice (invoices/<month>.paid.jsonl), oldest first; [] when none."""
        p = self.path("invoices", month + ".paid.jsonl")
        if not re.match(r"^\d{4}-\d{2}$", month) or not os.path.exists(p):
            return []
        return [rec for _, rec in read_jsonl(p)[0] if isinstance(rec, dict) and rec.get("kind") == "paid"]

    @staticmethod
    def mark_line(rec):
        """A paid mark as one line of the invoice's text: the pair, the note, when and where it was noted."""
        return "%s  %s paid %s: %s (noted %s on %s)" % (rec["month"], rec["payer"], rec["hop"], rec["note"],
                                                       time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(rec["t"])), rec["by"])

    def invoice_served(self, month):
        """The saved invoice's text with its paid marks after it, one line each, or None when the month has no saved invoice."""
        text = self.invoice_saved(month)
        if text is None:
            return None
        return text + "".join(self.mark_line(rec) + "\n" for rec in self.invoice_marks(month))

    def invoice_mark(self, month, hop, note, payer=None, now=None):
        """Note that a line of a saved invoice was paid: `bwkit invoice <month> --paid <hop> "<note>"`.

        The mark names the pair of one line of the saved invoice (the hop, and
        the payer when the hop has more than one), carries the note as given
        (how and when it was settled, in the parties' words) and the moment it
        was noted, and is appended to invoices/<month>.paid.jsonl beside the
        invoice, which it never changes. GET /invoice serves the two together.
        There is no payment rail; this is the Mint's record that the parties
        said it was paid. Returns the record. Raises SystemExit with the reason
        when the month has no saved invoice or no such line.
        """
        if not re.match(r"^\d{4}-\d{2}$", month):
            raise SystemExit("the month is YYYY-MM, as in 2026-10")
        note = (note or "").strip()
        if not note:
            raise SystemExit("a paid mark carries a note: how and when it was settled, as in \"settled in person, Nov 2\"")
        text = self.invoice_saved(month)
        if text is None:
            raise SystemExit("no saved invoice for %s: the Mint writes it on the first of the next month (`bwkit invoice %s --save` writes it now)" % (month, month))
        lines = [r for r in (parse_invoice_line(l) for l in text.split("\n")) if r]
        mine = [r for r in lines if r["hop"] == hop and (payer is None or r["payer"] == payer)]
        if not mine:
            raise SystemExit("no line of the %s invoice has %s%s paid: the lines are %s" % (
                month, hop, "" if payer is None else " by " + payer, ", ".join("%s pays %s" % (r["payer"], r["hop"]) for r in lines) or "none"))
        if len(mine) > 1:
            raise SystemExit("%d lines of the %s invoice pay %s (%s): say which with --payer" % (
                len(mine), month, hop, ", ".join(r["payer"] for r in mine)))
        rec = {"kind": "paid", "month": month, "payer": mine[0]["payer"], "hop": hop, "note": note,
               "t": int(now if now is not None else time.time()), "by": self.config.get("name", "the Mint")}
        d = self.path("invoices")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, month + ".paid.jsonl"), "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
        return rec

    def invoices_saved(self):
        """The months with a saved invoice, oldest first."""
        d = self.path("invoices")
        if not os.path.isdir(d):
            return []
        return sorted(n[:-4] for n in os.listdir(d) if re.match(r"^\d{4}-\d{2}\.txt$", n))

    def owed(self, month, purchases_path=None, invoice_path=None, links_path=None, price=DEFAULT_PRICE_USD_PER_GB, fee=DEFAULT_STEWARD_FEE,
             receipts_path=None, acks_path=None, prepay_path=None):
        """Hop or payer: the month's invoice line from this node's own copies alone, set against the Mint's.

        The receipts and acks this node kept (its own and the other side's,
        countersigned) and the announcements it made are rebuilt into
        settlements by the Mint's own rule (rebuild, spec Section 11) and
        summed as the Mint sums an invoice (invoice_sums), with no Mint
        involved. Then the Mint's invoice for the month is read, through this
        node's proxy when it answers or from --invoice, and the line that
        names this node is compared figure by figure. The Mint's word was
        never the proof; this is that sentence as a command.

        Purchases are the Mint's: --purchases, or /purchases through the
        proxy (signed records, checked like everything else); without them a
        purchased window's premium cannot be computed and the text says so.
        A payer keeps no announcements, so --links or the Mint's /links.
        A window from before profile 1 (its receipt carries no `proto`) is
        summed without a fee, as the Mint of its day took none. A hop keeps
        the payer's receipt only since bwkit 0.4.0, so an older ack stands
        alone and its window is not rebuilt: the other party's copy of its
        file, --receipts or --acks, is added to this node's own to fill such
        a gap (every record is still checked by its signature).

        Returns (text, rc): 0 when every figure of every line agrees, 1 when
        one differs, 2 when the Mint's invoice could not be read or compared.
        """
        if self.role == "mint":
            raise SystemExit("the Mint prints its invoice with `bwkit invoice`; `owed` is for a hop or a payer")
        me, my_name = self.nym, self.config.get("name") or self.nym

        def own(name):
            p = self.path(name)
            return read_jsonl(p) if os.path.exists(p) else ([], [])

        receipts, bad_r = own("receipts.jsonl")
        acks, bad_a = own("acks.jsonl")
        sources = []
        if receipts_path:
            more, bad = read_jsonl(receipts_path)
            receipts, bad_r = receipts + more, bad_r + bad                  # rebuild keeps each signature once
            sources.append("receipts from %s added to mine" % os.path.basename(receipts_path))
        if acks_path:
            more, bad = read_jsonl(acks_path)
            acks, bad_a = acks + more, bad_a + bad
            sources.append("acks from %s added to mine" % os.path.basename(acks_path))
        if links_path:
            links, bad_l = read_jsonl(links_path)
            sources.append("announcements from %s" % os.path.basename(links_path))
        elif self.role == "hop":
            links, bad_l = own("links.jsonl")
            sources.append("my own announcements")
        else:
            links, bad_l = [], []
        purchases, bad_p, purchases_known = [], [], False      # known: the Mint's registry was read, even if it is empty
        if purchases_path:
            purchases, bad_p = read_jsonl(purchases_path)
            purchases_known = True
            sources.append("purchases from %s" % os.path.basename(purchases_path))
        prepay, bad_pp = [], []
        if prepay_path:
            prepay, bad_pp = read_jsonl(prepay_path)
            sources.append("the prepayment registry from %s" % os.path.basename(prepay_path))
        mint_text = None
        if invoice_path:
            with open(invoice_path) as f:
                mint_text = f.read()
        proxied = []
        try:
            if not purchases_path:
                status, data, _ = self.proxy_get("/purchases")
                if status == 200:
                    purchases = [(i, json.loads(line)) for i, line in enumerate(data.decode("utf-8").split("\n"), 1) if line.strip()]
                    purchases_known = True
                    proxied.append("purchases")
            if links_path is None and self.role != "hop":
                status, data, _ = self.proxy_get("/links")
                if status == 200:
                    links = [(i, rec) for i, rec in enumerate(json.loads(data.decode("utf-8")), 1)]
                    proxied.append("announcements")
            if mint_text is None:
                status, data, _ = self.proxy_get("/invoice?month=" + month)
                if status == 200:
                    mint_text = data.decode("utf-8")
                    proxied.append("the invoice")
            if not prepay_path:
                status, data, _ = self.proxy_get("/prepay")
                if status == 200:
                    prepay = [(i, json.loads(line)) for i, line in enumerate(data.decode("utf-8").split("\n"), 1) if line.strip()]
                    proxied.append("the prepayment registry")
        except (urlerror.URLError, socket.timeout, OSError, ValueError):
            pass
        if proxied:
            sources.append("from the Mint through this node: " + ", ".join(proxied))
        place_of = place_lookup(self.load_map(), self.peers()) if self.map_path_exists() else None
        entries, rep = rebuild(receipts, acks, links, price, fee, purchases=purchases, place_of=place_of, prepay=prepay)
        left_out = bad_r + bad_a + bad_l + bad_p + bad_pp + rep["bad"]
        pbook = PrepayBook()
        for _, rec in prepay:
            if isinstance(rec, dict) and rec.get("kind") in PREPAY_KINDS and verify_prepay(rec):
                pbook.add(rec)
        for e in entries:
            if e.get("kind") == "settlement":
                pbook.note(e)                                            # what the rebuilt settlements drew on the locks
        # a window whose receipt is from before profile 1 was settled by a Mint that took no fee: summed as the Mint summed it
        pre = {env["sig"] for _, env in receipts if isinstance(env, dict) and "proto" not in env and env.get("sig")}
        before = 0
        settled = []
        for e in entries:
            if e.get("kind") == "settlement" and e.get("receipt_sig") in pre:
                e = dict(e, fee_usd=0.0)
                before += 1
            settled.append(e)
        names = dict(self.peers())
        names.setdefault(me, my_name)
        mine = {k: v for k, v in invoice_sums(settled, month).items() if (k[1] if self.role == "hop" else k[0]) == me}
        lines = ["from my own copies: %d receipts and %d acks under %d announcements and %d purchases (%s) rebuilt into %d settlements, %d of them mine in %s%s" % (
            rep["receipts"], rep["acks"], rep["links"], rep["purchases"], "; ".join(sources) or "nothing else",
            rep["settlements"], sum(o["windows"] for o in mine.values()), month,
            "; %d lines left out" % len(left_out) if left_out else "")]
        if rep["acks_without_receipt"]:
            lines.append("%d acks have no receipt in my copies, so their windows are not rebuilt here (a hop keeps the payer's receipt since bwkit 0.4.0, Oct 18, 2026; "
                         "the payer's receipts.jsonl fills the gap: --receipts <file>)" % rep["acks_without_receipt"])
        answered = {a["receipt"].get("prev_sig") for _, a in acks if isinstance(a, dict) and isinstance(a.get("receipt"), dict)}
        unanswered = sum(1 for _, r in receipts if isinstance(r, dict) and isinstance(r.get("receipt"), dict)
                         and r["receipt"].get("hop" if self.role == "hop" else "payer") == me and r.get("sig") not in answered)
        if unanswered:
            lines.append("%d of my receipts have no ack in my copies: a window the hop declined or never answered, or a gap the hop's acks.jsonl fills (--acks <file>)" % unanswered)
        if before:
            lines.append("%d settlements are from before profile 1 (no `proto` on the receipt): summed without a fee, as the Mint of their day took none" % before)
        for line in left_out:
            lines.append("left out: " + line)
        if not purchases_known:
            lines.append("no purchases at hand (the Mint did not answer; pass --purchases <file>): a purchased window's premium is not computed here")
        for (payer, hop), o in sorted(mine.items()):
            lines.append(invoice_pair_line(month, names.get(payer, payer), names.get(hop, hop), o))
            if o["refund_usd"]:
                lines.append("%s    returned to %s on short windows: $%.4f" % (month, names.get(payer, payer), o["refund_usd"]))
        if not mine:
            lines.append("%s  no window of mine settled" % month)
        total = round(sum(o["relay_usd"] if self.role == "hop" else o["amount_usd"] + o["premium_usd"] for o in mine.values()), 6)
        lines.append("%s  %s: $%.4f" % (month, "owed to me, the relay's share of every line" if self.role == "hop" else "I owe, carriage and premiums of every line", total))
        my_prepaid = None
        if self.role == "payer" and (pbook.payers() or any(o["covered"] for o in mine.values())):
            drawn = sum(o["drawn"] for o in mine.values()); covered = sum(o["covered"] for o in mine.values())
            credit = round(sum(o["credit_usd"] for o in mine.values()), 6); credit_windows = sum(o["windows"] - o["covered"] for o in mine.values())
            bal = pbook.balance(me, time.time())
            deposits = sum(d["amount"] for d in pbook.deposits if d["payer"] == me and time.strftime("%Y-%m", time.localtime(d["posted_at"])) == month)
            my_prepaid = parse_prepaid_line(invoice_prepaid_line(month, my_name, deposits, drawn, covered, credit, credit_windows, bal["balance"]))
            lines.append(invoice_prepaid_line(month, my_name, deposits, drawn, covered, credit, credit_windows, bal["balance"]))
            lines.append("%s  the deposits and the balance are the Mint's own registry read back; the drawn and credit figures are from my copies" % month)
        # against the Mint's invoice
        if mint_text is None:
            lines.append("the Mint's invoice was not read (no Mint answered through this node; pass --invoice <file>), so nothing was compared")
            return "\n".join(lines) + "\n", 2
        mint_lines = [r for r in (parse_invoice_line(l) for l in mint_text.split("\n")) if r]
        mint_mine = [r for r in mint_lines if (r["hop"] if self.role == "hop" else r["payer"]) == my_name]
        if not mine and not mint_mine:
            lines.append("matches the Mint's invoice: neither side has a window of mine in %s" % month)
            return "\n".join(lines) + "\n", 0
        if not mint_mine:
            lines.append("DIFFERS: the Mint's invoice has no line naming %s (it names %s); the Mint may know me by another name, or holds none of my windows" % (
                my_name, ", ".join(sorted({r["hop"] if self.role == "hop" else r["payer"] for r in mint_lines})) or "no pair at all"))
            return "\n".join(lines) + "\n", 2
        diffs = []
        by_other = {(r["payer"] if self.role == "hop" else r["hop"]): r for r in mint_mine}
        for (payer, hop), o in sorted(mine.items()):
            other = names.get(payer, payer) if self.role == "hop" else names.get(hop, hop)
            r = by_other.pop(other, None)
            if r is None:
                diffs.append("the Mint has no line for %s" % other)
                continue
            own_line = parse_invoice_line(invoice_pair_line(month, names.get(payer, payer), names.get(hop, hop), o))
            for field in ("windows", "disputed", "bytes", "amount", "premium", "purchases", "from_steward", "relay", "fee"):
                if own_line[field] != r[field]:
                    unit = "" if field in ("windows", "disputed", "purchases", "bytes") else "$"
                    diffs.append("%s with %s: %s%s here, %s%s on the Mint" % (field, other, unit, own_line[field], unit, r[field]))
        for other in by_other:
            diffs.append("the Mint has a line for %s that my copies do not" % other)
        if my_prepaid is not None:
            theirs_pp = [parse_prepaid_line(l) for l in mint_text.split("\n")]
            theirs_pp = next((r for r in theirs_pp if r and r["payer"] == my_name), None)
            if theirs_pp is None:
                diffs.append("prepaid: my copies and the registry give a prepayment line, the Mint's invoice has none for %s" % my_name)
            else:
                for field in ("deposits", "drawn", "covered", "balance", "credit", "credit_windows"):
                    if my_prepaid[field] != theirs_pp[field]:
                        unit = "" if field in ("covered", "credit_windows") else "$"
                        diffs.append("prepaid %s: %s%s here, %s%s on the Mint" % (field, unit, my_prepaid[field], unit, theirs_pp[field]))
        if diffs:
            lines.append("DIFFERS from the Mint's invoice:")
            lines.extend("  " + d for d in diffs)
            return "\n".join(lines) + "\n", 1
        lines.append("matches the Mint's invoice, figure for figure: %s $%.4f" % ("the relay's" if self.role == "hop" else "I owe", total))
        return "\n".join(lines) + "\n", 0

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

    def status_url(self):
        """Where the Mint posts its status record: BWKIT_STATUS_URL, else `status_url` in the config, the feed's /report; None when neither is set."""
        return os.environ.get("BWKIT_STATUS_URL") or self.config.get("status_url") or None

    def map_url(self):
        """Where the map goes: the feed's /map beside the /report that status_url names; None when status_url is not a /report."""
        url = self.status_url()
        return re.sub(r"/report/?$", "/map", url) if url and re.search(r"/report/?$", url) else None

    def status_record(self, now=None, service=None):
        """Mint: status_report() as the signed record the public feed takes (spec, Section 9, informative).

        The report with `kind` "status", `proto`, `mint` (this Mint's NYM) and
        `sig`: Ed25519 by the Mint over STATUS_DOMAIN and the canonical JSON of
        the record without `sig`. The key is the credential: the feed verifies
        the signature under `mint` and asks for nothing else, so there is no
        token to issue, store or rotate, and a reader of the feed checks the
        same signature on what the feed serves.
        """
        if self.role != "mint":
            raise SystemExit("only the Mint reports")
        if self.priv is None:
            raise SystemExit("no key: the status record is signed by the Mint (bwkit keygen)")
        rec = self.status_report(now, service)
        rec["kind"], rec["proto"], rec["mint"] = "status", PROTO, self.nym
        rec["sig"] = sign_status(self.priv, rec)
        return rec

    def report(self, url=None, now=None):
        """Mint: post the signed status record to the public feed. Returns (HTTP status, body).

        The URL comes from the argument, else BWKIT_STATUS_URL, else
        `status_url` in the config: the feed's /report. The feed answers 429
        to a second report from the same Mint within a minute and 409 to one
        whose generated_at is not newer than the last it stored; a caller
        that lands on either has lost nothing, the next heartbeat carries
        the same ledger.
        """
        if self.role != "mint":
            raise SystemExit("only the Mint reports")
        url = url or self.status_url()
        if not url:
            raise SystemExit("no status URL: set status_url in the config (the feed's /report), or BWKIT_STATUS_URL")
        rec = self.status_record(now, service=self.service_up())
        text = json.dumps(rec, sort_keys=True)
        assert_public(text)
        if len(text) > REPORT_MAX:
            raise ValueError("the report is %d bytes; the status feed takes at most %d" % (len(text), REPORT_MAX))
        status, body = http_post(url, rec, timeout=STATUS_TIMEOUT_S)
        if 200 <= status < 300:
            st = self.state()
            st["last_report_at"] = int(now if now is not None else time.time())
            self.write_json("state.json", st)
        return status, body

    def report_later(self, delay=None):
        """A serving Mint with a status URL posts a report `delay` seconds after a commitment (REPORT_AFTER_COMMIT_S by
        default), once however many commitments land meanwhile, so the hour shows on the feed within seconds of
        closing; the timer's heartbeat does the rest. Anything else is a no-op."""
        if not self._serving or self.role != "mint" or not self.status_url():
            return
        with self._report_lock:
            if self._report_timer is not None:
                return
            t = threading.Timer(REPORT_AFTER_COMMIT_S if delay is None else delay, self.report_quietly)
            t.daemon = True
            self._report_timer = t
            t.start()

    def report_quietly(self):
        """report() from the timer thread: logs the outcome, raises nothing."""
        with self._report_lock:
            self._report_timer = None
        try:
            status, body = self.report()
        except (urlerror.URLError, socket.timeout, OSError, ValueError, SystemExit) as e:
            log("status not posted after the commitment: %s" % e)
            return
        if 200 <= status < 300:
            log("status posted after the commitment: %s" % json.dumps(body, sort_keys=True))
        elif status in (409, 429):
            log("status not posted after the commitment (%s %s); the next heartbeat carries it" % (status, body.get("error") if isinstance(body, dict) else body))
        else:
            log("status feed refused the report after the commitment: %s %s" % (status, body))

    def map_record(self, now=None):
        """Mint: the membership's map (load_map) as the signed record a feed lists Mints by (spec, Section 9, informative).

        `kind` "map", `proto`, `by` (this Mint's NYM), `at`, `map` (the file's
        domains, places and nodes, and `mints`: this Mint's NYM with its name
        and its domain, the one whose `mint` is this node, else the first) and
        `sig`: Ed25519 by the Mint over MAP_DOMAIN and the canonical JSON of
        the record without `sig`. The feed takes it from a Mint its operator
        names and lists the Mints in `mints`; when Mints reconcile the map
        among themselves (paper, A.5) the record is what they reconcile.
        """
        if self.role != "mint":
            raise SystemExit("only the Mint signs the map")
        if self.priv is None:
            raise SystemExit("no key: the map is signed by the Mint (bwkit keygen)")
        now = int(now if now is not None else time.time())
        m = self.load_map()
        mine = [k for k, d in m["domains"].items() if isinstance(d, dict) and d.get("mint") == self.name]
        domain = mine[0] if mine else next(iter(m["domains"]), None)
        rec = {"kind": "map", "proto": PROTO, "by": self.nym, "at": now,
               "map": {"mints": [{"nym": self.nym, "name": self.name, "domain": domain}],
                       "domains": m["domains"], "places": m["places"], "nodes": m["nodes"]}}
        assert_public(canon_json(rec))
        rec["sig"] = sign_map(self.priv, rec)
        return rec

    def post_map(self, url, rec=None):
        """Mint: post the signed map to the feed's /map. Returns (HTTP status, body)."""
        return http_post(url, rec or self.map_record(), timeout=STATUS_TIMEOUT_S)

    # the node's own page

    def clock(self):
        """(offset in seconds, positive when this clock is ahead of its source; stratum) from chronyc, or (None, None)."""
        try:
            out = subprocess.run(["chronyc", "-c", "tracking"], capture_output=True, text=True, timeout=3).stdout.strip().split(",")
            return -float(out[4]), int(out[2])            # chronyc's field is positive when the clock is slow
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            return None, None

    def disk_free(self):
        try:
            v = os.statvfs(self.dir)
            return v.f_bavail * v.f_frsize
        except OSError:
            return None

    def status_dict(self):
        """What GET /status answers on this role: the node, its clock and disk, and what it holds."""
        offset, stratum = self.clock()
        st = {"name": self.name, "role": self.role, "nym": self.nym, "bwkit": VERSION, "impl": IMPL, "proto": list(PROTOS),
              "time": int(time.time()), "clock_offset_s": offset, "stratum": stratum, "disk_free_bytes": self.disk_free(),
              "last_report_at": self.state().get("last_report_at")}
        if self.role == "mint":
            if self.index is None:
                self.load_index()
            no_ack, no_receipt = self.pending()
            st["ledger_lines"] = self.index["lines"]
            st["settlements"] = len(self.index["settled"])
            st["commitments"] = len(self.index["commitments"])
            st["disputed"] = sum(1 for x in self.index["settled"].values() if x["status"] == "disputed")
            st["pending"] = {"receipts_without_ack": len(no_ack), "acks_without_receipt": len(no_receipt)}
            st["links"] = self.index["links"]
            st["purchases"] = len(self.index["book"].order)
            st["steward"] = self.index["steward_nym"]
        else:
            st["sample"] = self.state().get("sample")
            st["outbox"] = len(os.listdir(self.path("outbox")))
            st["mint_name"] = self.mint_info()["name"]
            if self.role == "hop":
                st["link"] = self.my_link()
                st["patience"] = self.patience_state()
            else:
                st["purchases"] = sum(1 for r in self.load_purchases(skip_partial=True) if r.get("kind") == "purchase")
        st["health"] = self.health(st, st["time"])
        return st

    # health: the checks every view shows, computed once here (dashboard/PLAN.md 1.3)

    def own_pairs(self):
        """A payer's or a hop's own windows, oldest first: (the receipt envelope, the ack envelope or None).

        From receipts.jsonl and acks.jsonl: a payer holds what it signed and
        what the hop answered; a hop holds what it countersigned and its
        acks. An ack pairs with its receipt by prev_sig.
        """
        acks = {a["receipt"]["prev_sig"]: a for a in self.read_lines("acks.jsonl", skip_partial=True) if a.get("receipt")}
        pairs = [(r, acks.get(r["sig"])) for r in self.read_lines("receipts.jsonl", skip_partial=True) if r.get("receipt")]
        pairs.sort(key=lambda p: (p[0]["receipt"]["t_first"], p[0]["receipt"]["t_last"]))
        return pairs

    def link_terms(self):
        """The margin and min_bytes a hop's or a payer's windows settle under: the hop's own link; for a payer, the newest
        settlement the Mint returned to it (sent.jsonl), else the pilot's defaults."""
        if self.role == "hop":
            link = self.my_link()
            return link["margin"], link["min_bytes"]
        newest = None
        for e in self.read_lines("sent.jsonl", skip_partial=True):
            for s in (e.get("mint") or {}).get("settlements") or []:
                if s.get("margin") is not None and (newest is None or s["t_first"] >= newest["t_first"]):
                    newest = s
        if newest:
            return newest["margin"], newest.get("min_bytes", PILOT_LINK["min_bytes"])
        return PILOT_LINK["margin"], PILOT_LINK["min_bytes"]

    def health(self, st=None, now=None):
        """The node's own verdict on itself: `{items: [{level, text, since}], checked_at}`, as /status.health.

        Computed from the node's own records, so the node's page, a fleet
        page and a timer without a browser read the same checks; the page
        adds its own on top (dashboard/rules.js). `level` is bad, warn or
        info; `text` is plain, in the page's wording; `since` is when the
        condition began where that is known, else None. `st` is the status
        dict the clock and disk are read from (status_dict passes its own).
        The checks: a clock off its source beyond a second; disk under 500
        MB; a receipt waiting more than ten minutes for its ack; a receipt
        two windows old without one (a lost window, listed for a day); an
        hour with settlements and no commitment (the Mint, since
        commitments began); the public status not posted in ten minutes
        (the Mint); an outbox not empty (a hop, a payer); a payer declined
        or counted short (a hop); a link whose median gap over its last 24
        windows exceeds its margin; more than one open first-link offer for
        a place (the Mint).
        """
        now = int(now if now is not None else time.time())
        st = st or {}
        items = []

        def add(level, text, since=None):
            items.append({"level": level, "text": text, "since": since})

        peers = self.peers()
        me = self.nym if self.priv else None

        def name(nym):
            return self.name if nym == me else peers.get(nym) or (str(nym).split(":", 1)[-1][:8] + "…")

        def waiting(frm, r, t0):
            """A receipt (from `frm`, for window r) with no ack since t0."""
            waited = now - t0
            if waited > HEALTH["lost_after_s"]:
                if waited <= HEALTH["lost_shown_s"]:
                    add("warn", "The %s window%s is lost: two windows have passed and the hop never countersigned, so it is unpaid." % (
                        page_tstamp(r["t_first"]), " from " + frm if frm else ""), t0)
            elif waited > HEALTH["ack_wait_s"]:
                add("warn", "A receipt%s for the %s window has waited %s for the hop's countersignature." % (
                    " from " + frm if frm else "", page_tstamp(r["t_first"]), page_dur(waited)), t0)

        def gap(payer, hop, windows, margin, min_bytes):
            """windows: (payer_bytes, hop_bytes, disputed) newest first; the median gap over the last 24 against the margin."""
            w = windows[:HEALTH["gap_windows"]]
            if len(w) < HEALTH["gap_min_windows"]:
                return
            med = median([gap_of(p, h) for p, h, _ in w])
            if med > margin:
                add("warn", "%s → %s: the gap runs at %s%s over the last %d windows against a margin of %s, so %d of them settled disputed; "
                    "raise the margin in the link's announcement or mend the path." % (
                        name(payer), name(hop), "+" if med >= 0 else "", page_pct(med), len(w), page_pct(margin, 0), sum(1 for x in w if x[2])))

        offset = st.get("clock_offset_s")
        if offset is not None and abs(offset) > HEALTH["clock_s"]:
            add("bad", "The clock is %.2f s off its source; receipts are declined beyond the link's tolerance." % offset)
        free = st.get("disk_free_bytes")
        if free is not None and free < HEALTH["disk_bytes"]:
            add("bad", "Only %s of disk is free for the ledger." % page_bytes(free))

        if self.role == "mint":
            if self.index is None:
                self.load_index(skip_partial=True)
            ix = self.index
            no_ack, _ = self.pending()
            for e in sorted(no_ack, key=lambda e: e["received_at"]):
                waiting(e.get("from"), e["receipt"], e["received_at"])
            hours = {}
            for s in ix["settled"].values():
                hours.setdefault(commit_hour(s), []).append(s)
            first = min(ix["commitments"]) if ix["commitments"] else None
            cur = now // 3600 * 3600
            for w in sorted(hours):
                if first is not None and first <= w < cur and w not in ix["commitments"] and now - w > 3600 + HEALTH["commit_slack_s"]:
                    add("bad", "The %s hour has %s and no commitment." % (page_tstamp(w), plural(len(hours[w]), "settlement")), w + 3600)
            last = st.get("last_report_at", self.state().get("last_report_at"))
            if last is not None and now - last > HEALTH["report_s"]:
                add("warn", "The public status was last posted %s." % page_ago(last, now), last)
            links = {}
            for s in ix["settled"].values():
                links.setdefault((s["payer"], s["hop"]), []).append(s)
            for (payer, hop), ss in sorted(links.items()):
                ss.sort(key=lambda s: (s["t_first"], s["t_last"]), reverse=True)
                rec = link_in_force(ix["links"], payer, hop, now)
                margin = link_params(rec)["margin"] if rec else (ss[0]["margin"] if ss[0].get("margin") is not None else PILOT_LINK["margin"])
                gap(payer, hop, [(s["payer_bytes"], s["hop_bytes"], s["status"] == "disputed") for s in ss], margin, None)
            book, open_first = ix["book"], {}
            for pid in book.order:
                p = book.purchases[pid]
                if p["program"] == "first_link" and p["deadline"] >= now and pid not in book.closed:
                    open_first.setdefault(p["route"]["to"], []).append(p["posted_at"])
            for place, posted in sorted(open_first.items()):
                if len(posted) > 1:
                    add("warn", "%d first-link offers are open for %s; the rule should hold one standing offer and top it up." % (
                        len(posted), place), sorted(posted)[1])
        else:
            names = sorted(os.listdir(self.path("outbox")))
            if names:
                oldest = int(names[0].split("-")[0]) if names[0].split("-")[0].isdigit() else None
                add("bad" if len(names) > 2 else "warn", "%s wait%s in the outbox for the Mint; nothing is lost, they go when it answers." % (
                    plural(len(names), "record"), "s" if len(names) == 1 else ""), oldest)
            if self.role == "hop":
                patience = self.my_link()["patience_windows"]
                for payer, ps in sorted(self.patience_state().items()):
                    if ps.get("declined"):
                        add("bad", "Payer %s is declined since %s after %d short windows; service to it has stopped." % (
                            name(payer), page_tstamp(ps.get("declined_at")), ps.get("short", 0)), ps.get("declined_at"))
                    elif ps.get("short", 0) > 0:
                        add("info", "Payer %s has %s short beyond the margin against a patience of %d." % (
                            name(payer), plural(ps["short"], "window"), patience), None)
            pairs = self.own_pairs()
            if self.role == "payer":
                for r, a in pairs:
                    if a is None:
                        waiting(r.get("from"), r["receipt"], r["receipt"]["t_last"])
            margin, min_bytes = self.link_terms()
            links = {}
            for r, a in pairs:
                if a is not None:
                    x, y = r["receipt"]["bytes"], a["receipt"]["bytes"]
                    links.setdefault((r["receipt"]["payer"], r["receipt"]["hop"]), []).append((x, y, abs(x - y) > max(min_bytes, margin * max(x, y))))
            for (payer, hop), ws in sorted(links.items()):
                gap(payer, hop, list(reversed(ws)), margin, min_bytes)
        order = {"bad": 0, "warn": 1, "info": 2}
        items.sort(key=lambda i: order.get(i["level"], 3))
        return {"items": items, "checked_at": now}

    def dashboard_dir(self):
        """The directory the page is served from: `dashboard` in the config, else the installed copy, else the kit's own."""
        for d in ((self.config.get("dashboard"),) if self.config.get("dashboard") else ()) + DASHBOARD_DIRS:
            if d and os.path.isfile(os.path.join(d, "index.html")):
                return d
        return None

    def dashboard_file(self, name):
        """(bytes, content type) of one file of the page's directory, or None: a plain name of a known type, nothing else."""
        d = self.dashboard_dir()
        ext = os.path.splitext(name)[1]
        if d is None or not DASHBOARD_NAME.match(name) or ext not in DASHBOARD_TYPES or not os.path.isfile(os.path.join(d, name)):
            return None
        with open(os.path.join(d, name), "rb") as f:
            return f.read(), DASHBOARD_TYPES[ext]

    def snapshot_remote(self):
        """A hop's or a payer's share of a snapshot that comes from its Mint, fetched before the node's lock is taken:
        {mint: {ledger, commitments, name, invoice}, map, proofs}. Nothing of it is re-serialised: the ledger is the
        Mint's text, the proofs are the Mint's text, for the hours that hold this node's windows (the newest
        SNAPSHOT_PROOFS), and `invoice` is {month: text} for the month before this one and this one so far."""
        info = self.mint_info()
        out = {"mint": {"ledger": "", "commitments": [], "name": info["name"], "invoice": {}}, "map": None, "proofs": {}}
        try:
            status, data, _ = self.proxy_get("/ledger")
            if status == 200:
                out["mint"]["ledger"] = data.decode("utf-8")
            status, data, _ = self.proxy_get("/commitments")
            if status == 200:
                out["mint"]["commitments"] = json.loads(data.decode("utf-8"))
            for month in (previous_month(), time.strftime("%Y-%m")):
                status, data, _ = self.proxy_get("/invoice?month=" + month)
                if status == 200:
                    out["mint"]["invoice"][month] = data.decode("utf-8")
            if not self.map_path_exists():
                status, data, _ = self.proxy_get("/map")
                if status == 200:
                    out["map"] = json.loads(data.decode("utf-8"))
        except (urlerror.URLError, socket.timeout, OSError, ValueError):
            return out
        me = self.nym if self.priv else None
        mine = set()
        for line in out["mint"]["ledger"].split("\n"):
            if '"settlement"' in line:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("kind") == "settlement" and me in (e.get("payer"), e.get("hop")):
                    mine.add(commit_hour(e))
        hours = sorted((c["window"] for c in out["mint"]["commitments"] if c["window"] in mine), reverse=True)[:SNAPSHOT_PROOFS[self.role]]
        for w in hours:
            try:
                status, data, _ = self.proxy_get("/proof?window=%d" % w)
            except (urlerror.URLError, socket.timeout, OSError):
                break
            if status == 200:
                out["proofs"][str(w)] = data.decode("utf-8")
        return out

    def snapshot_data(self, remote=None):
        """The node's data as the page's `<script id="snapshot">` holds it (dashboard/fixtures/<role>.json has each role's shape).

        Every .jsonl file goes in as its text; a proof as the text GET /proof
        serves, since its leaves hash the Mint's own number spelling; a hop's
        or payer's Mint comes from `remote` (snapshot_remote).
        """
        st = self.status_dict()
        snap = {"taken_at": st["time"], "status": st}
        if self.role == "mint":
            snap["ledger"] = self.file_bytes("ledger.jsonl").decode("utf-8")
            snap["commitments"] = self.commitments()
            snap["purchases"] = self.file_bytes("purchases.jsonl").decode("utf-8")
            snap["steward"] = self.steward_view()
            snap["steward_runs"] = self.steward_runs()
            snap["map"] = self.load_map() if self.map_path_exists() else None
            proofs = {}
            for c in snap["commitments"][-SNAPSHOT_PROOFS["mint"]:]:
                try:
                    proofs[str(c["window"])] = json.dumps(self.proof(c["window"], reload=False), sort_keys=True)
                except (SystemExit, ValueError):
                    pass                                     # an hour settled since its commitment: the next one has both
            snap["proofs"] = proofs
            return snap
        remote = remote or {"mint": {"ledger": "", "commitments": [], "name": self.mint_info()["name"], "invoice": {}}, "map": None, "proofs": {}}
        if self.role == "payer":
            snap["receipts"] = self.file_bytes("receipts.jsonl").decode("utf-8")
            snap["acks"] = self.file_bytes("acks.jsonl").decode("utf-8")
            snap["purchases"] = self.file_bytes("purchases.jsonl").decode("utf-8")
        else:
            snap["acks"] = self.file_bytes("acks.jsonl").decode("utf-8")
            snap["receipts"] = self.file_bytes("receipts.jsonl").decode("utf-8")
            snap["links"] = self.file_bytes("links.jsonl").decode("utf-8")
        snap["outbox"] = self.outbox_list()
        snap["mint"] = remote["mint"]
        snap["map"] = self.load_map() if self.map_path_exists() else remote["map"]
        snap["proofs"] = remote["proofs"]
        return snap

    def dashboard_html(self, snapshot=False, remote=None):
        """index.html as served at /, /fleet and /snapshot; with snapshot, one self-contained file: the stylesheet and the
        scripts inlined (bsp.js as an inline module) and the node's data in the `<script id="snapshot">` element."""
        d = self.dashboard_dir()
        if d is None:
            return None

        def read(name):
            with open(os.path.join(d, name), encoding="utf-8") as f:
                return f.read().replace("</script", "<\\/script")

        html = read("index.html").replace("<\\/script", "</script")
        if not snapshot:
            return html
        html = re.sub(r'<link rel="stylesheet" href="dash/([A-Za-z0-9_.-]+)">', lambda m: "<style>\n%s</style>" % read(m.group(1)), html)
        html = re.sub(r'<script src="dash/([A-Za-z0-9_.-]+)"></script>', lambda m: "<script>\n%s</script>" % read(m.group(1)), html)
        if BSP_MODULE in html:
            text = read("bsp.js")
            names = re.findall(r"^export (?:async )?(?:function|const|let|var|class) (\w+)", text, re.M)   # an inline module may export; nothing imports it
            html = html.replace(BSP_MODULE, '<script type="module">\n%s\nwindow.bsp = {%s};</script>' % (text, ", ".join(names)), 1)
        marker = '<script id="snapshot" type="application/json"></script>'
        filled = '<script id="snapshot" type="application/json">%s</script>' % json.dumps(self.snapshot_data(remote)).replace("</", "<\\/")
        return html.replace(marker, filled, 1) if marker in html else html

    def map_path_exists(self):
        return os.path.exists(os.environ.get("BWKIT_MAP") or self.config.get("map") or DEFAULT_MAP_PATH)

    def steward_runs(self, since=0):
        """GET /steward/history: the Steward's runs from steward.jsonl at or after `since`, oldest first, balances only.

        Each run is the book as that run left it, so the page's as-of control
        can draw the Steward's balances at any moment and not only now.
        """
        if self.role != "mint":
            return []
        out = []
        for r in self.read_lines("steward.jsonl", skip_partial=True):
            if not isinstance(r, dict) or r.get("kind") != "steward-run" or r.get("t", 0) < since:
                continue
            out.append({"t": r["t"], "income_usd": r.get("income_usd", 0), "escrow_usd": r.get("escrow_usd", 0), "posted_usd": r.get("posted_usd", 0),
                        "paid_usd": r.get("paid_usd", 0), "refunded_usd": r.get("refunded_usd", 0), "held_by_cap_usd": r.get("held_by_cap_usd", 0),
                        "posted": len(r.get("posted") or []), "skipped": list(r.get("skipped") or [])})
        return out

    def steward_view(self):
        """GET /steward: the feed's steward object with the latest run's `skipped`, or None without a Steward key."""
        if self.role != "mint" or not self.steward_nym():
            return None
        try:
            st = Steward(self)
            fd = st.feed()
            last = st.last_run()
            fd["skipped"] = last["skipped"] if last else []
            fd["adjusted"] = last.get("adjusted", []) if last else []
            return fd
        except (SystemExit, ValueError, OSError):
            return None

    # a hop's and a payer's view of its Mint

    def outbox_list(self):
        """GET /outbox: what waits for the Mint, oldest first: [{file, kind, t}], t the window's end the record is for."""
        out = []
        for name in sorted(os.listdir(self.path("outbox"))):
            parts = name.split("-")
            out.append({"file": name, "kind": parts[2] if len(parts) > 2 else None, "t": int(parts[0]) if parts and parts[0].isdigit() else None})
        return out

    def mint_url(self):
        return (self.config.get("mint") or "").rstrip("/") or None

    def mint_info(self, now=None):
        """The Mint's name and NYM, read from its /status now and then (MINT_INFO_S) and remembered; `mint_name` in the
        config stands in for the name. {name, nym, at, ok}; name None until the Mint has answered once."""
        now = now if now is not None else time.time()
        info = getattr(self, "_mint_info", None)
        if info and now - info["at"] < MINT_INFO_S[0 if info["ok"] else 1]:
            return info
        info = {"name": self.config.get("mint_name") or (info["name"] if info else None), "nym": info["nym"] if info else None, "at": now, "ok": False}
        if self.mint_url():
            try:
                status, text = http_get(self.mint_url() + "/status", timeout=MINT_INFO_TIMEOUT_S)
                st = json.loads(text)
                info.update({"name": self.config.get("mint_name") or st.get("name"), "nym": st.get("nym"), "ok": True})
            except (urlerror.URLError, socket.timeout, OSError, ValueError, KeyError):
                pass
        self._mint_info = info
        return info

    def proxy_get(self, path):
        """GET a path of this node's Mint for its own page (/mint/...): (status, body bytes, content type). Raises OSError
        when the Mint does not answer. The Mint's bytes pass through untouched: a proof's floats are its own."""
        url = self.mint_url()
        if not url:
            raise OSError("no Mint configured")
        req = urlrequest.Request(url + path, headers={"User-Agent": USER_AGENT})
        try:
            with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type") or "application/json"
        except urlerror.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type") or "application/json"

    # the server

    def serve(self, host="0.0.0.0", port=None, block=True):
        """Serve HTTP: the Mint's and the hop's endpoints, and on every role the node's own page at / (GET only on a payer)."""
        if self.role == "mint":
            self.load_index()
        node = self
        port = port if port is not None else int(self.config.get("port", DEFAULT_PORT))

        class Handler(BaseHTTPRequestHandler):
            server_version = IMPL

            def log_message(self, fmt, *args):
                pass

            # Every answer carries Access-Control-Allow-Origin: *. Nothing a node serves is secret or carries an address
            # (assert_public is the test's check), and the fleet page in an operator's browser, and a hop's page reading
            # its Mint, come from another origin.
            def cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")

            def reply(self, status, obj):
                data = json.dumps(obj, sort_keys=True).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.cors()
                self.end_headers()
                self.wfile.write(data)

            def raw(self, data, ctype, filename=None, status=200):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                if filename:
                    self.send_header("Content-Disposition", 'inline; filename="%s"' % filename)
                self.cors()
                self.end_headers()
                self.wfile.write(data)

            def do_OPTIONS(self):
                """The CORS preflight: any origin may GET; a POST is for nodes, which send no preflight."""
                self.send_response(204)
                self.cors()
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Access-Control-Max-Age", "86400")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                url = urlparse.urlsplit(self.path)
                if url.path in ("/", "/index.html", "/fleet", "/snapshot"):
                    snapshot = url.path == "/snapshot"
                    remote = node.snapshot_remote() if snapshot and node.role != "mint" else None   # the Mint's part, outside the lock
                    with node.lock:
                        html = node.dashboard_html(snapshot=snapshot, remote=remote)
                    if html is None:
                        return self.reply(404, {"error": "no dashboard: the page's directory is not installed (config `dashboard`, or /usr/local/share/bwkit/dashboard)"})
                    return self.raw(html.encode("utf-8"), "text/html; charset=utf-8",
                                    "%s-snapshot-%s.html" % (node.name, time.strftime("%Y%m%dT%H%M")) if snapshot else None)
                if url.path.startswith("/dash/"):
                    got = node.dashboard_file(url.path[6:])
                    if got is None:
                        return self.reply(404, {"error": "no such file of the page"})
                    return self.raw(got[0], got[1])
                if url.path == "/status":
                    if node.role != "mint":
                        node.mint_info()                     # the Mint's name, read outside the lock when it is time
                    with node.lock:
                        return self.reply(200, node.status_dict())
                if node.role != "mint":
                    if url.path in ("/acks", "/receipts") or (url.path == "/links" and node.role == "hop"):
                        return self.raw(node.file_bytes(url.path[1:] + ".jsonl"), "application/x-ndjson")
                    if url.path == "/outbox":
                        return self.reply(200, node.outbox_list())
                    if url.path in ("/mint/ledger", "/mint/commitments", "/mint/links", "/mint/proof", "/mint/map", "/mint/invoice", "/mint/invoices", "/mint/prepay", "/mint/balance") or (url.path == "/map" and not node.map_path_exists()):
                        path = url.path[5:] if url.path.startswith("/mint/") else url.path
                        try:
                            status, data, ctype = node.proxy_get(path + ("?" + url.query if url.query else ""))
                        except (urlerror.URLError, socket.timeout, OSError, ValueError):
                            return self.reply(502, {"error": "the Mint did not answer"})   # never its address
                        return self.raw(data, ctype, status=status)
                if url.path == "/ledger" and node.role == "mint":
                    since = urlparse.parse_qs(url.query).get("since", [""])[0]
                    if since and not since.isdigit():
                        return self.reply(400, {"error": "since must be Unix seconds"})
                    return self.raw(node.ledger_text(int(since) if since else None), "application/x-ndjson")
                if url.path == "/purchases":
                    return self.raw(node.file_bytes("purchases.jsonl"), "application/x-ndjson")
                if url.path == "/commitments" and node.role == "mint":
                    return self.reply(200, node.commitments())
                if url.path == "/links" and node.role == "mint":
                    return self.reply(200, node.index["links"])
                if url.path == "/prepay" and node.role == "mint":
                    return self.raw(node.file_bytes("prepay.jsonl"), "application/x-ndjson")
                if url.path == "/balance" and node.role == "mint":
                    q = urlparse.parse_qs(url.query).get("payer", [""])[0]
                    if not q:
                        return self.reply(400, {"error": "payer=<NYM or peer name> is required"})
                    nym = q if q.startswith("ed25519:") else next((n for n, name in node.peers().items() if name == q), None)
                    if nym is None or nym not in node.peers():
                        return self.reply(404, {"error": "no peer %s" % q})
                    with node.lock:
                        return self.reply(200, node.prepay_balance(nym))
                if url.path == "/steward/history" and node.role == "mint":
                    q = urlparse.parse_qs(url.query).get("since", ["0"])[0]
                    if not q.isdigit():
                        return self.reply(400, {"error": "since must be Unix seconds"})
                    with node.lock:
                        return self.reply(200, node.steward_runs(int(q)))
                if url.path == "/steward" and node.role == "mint":
                    with node.lock:
                        view = node.steward_view()
                    if view is None:
                        return self.reply(404, {"error": "this Mint has no Steward key"})
                    return self.reply(200, view)
                if url.path == "/map":
                    if not node.map_path_exists():
                        return self.reply(404, {"error": "no map on this node"})
                    return self.reply(200, node.load_map())
                if url.path == "/invoice" and node.role == "mint":
                    month = urlparse.parse_qs(url.query).get("month", [""])[0] or previous_month()
                    if not re.match(r"^\d{4}-\d{2}$", month):
                        return self.reply(400, {"error": "month must be YYYY-MM"})
                    text = node.invoice_served(month)
                    if text is None:
                        text = node.invoice_text(month) + "the month so far, computed now; the Mint writes %s.txt on the first of the next month\n" % month
                    return self.raw(text.encode(), "text/plain; charset=utf-8", filename="invoice-%s.txt" % month)
                if url.path == "/invoices" and node.role == "mint":
                    return self.reply(200, node.invoices_saved())
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
                    if node.role == "payer":
                        return self.reply(404, {"error": "a payer takes no records; its page is at /"})
                    if self.path == "/ack" and node.role == "hop":
                        return self.reply(200, node.ack(body))
                    if self.path == "/receipt" and node.role == "mint":
                        return self.reply(200, node.receive(body))
                    if self.path == "/link" and node.role == "mint":
                        return self.reply(200, {"stored": node.register_link(body)})
                    if self.path == "/purchase" and node.role == "mint":
                        return self.reply(200, {"stored": node.register_purchase(body)})
                    if self.path == "/prepay" and node.role == "mint":
                        rec = node.register_prepay(body)
                        return self.reply(200, {"stored": rec["id"], "record": rec})
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
        self._serving = True
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
                         "returned": round(max(0.0, pool), 6) if status != "open" else 0.0,   # what goes back to whoever put it in
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
                              "returned_usd": r["returned"], "match_usd": r["match"]["escrow"] if r["match"] else None, "status": r["status"]})
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
    sv = sub.add_parser("serve", help="serve HTTP: the hop's and the Mint's endpoints, and the node's own page at / on every role")
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
    dp = sub.add_parser("deposit", help="mint: note that a payer put value on its balance here (spec, Section 16); signed by this Mint, kept in prepay.jsonl")
    dp.add_argument("payer", help="the payer, by peer name or NYM")
    dp.add_argument("usd", type=float, help="the amount in USD, kept as micro-units")
    dp.add_argument("note", help="how it came, in the parties' words (\"a dollar in cash, Nov 2\")")
    pr = sub.add_parser("prepay", help="mint: the prepayment registry and the balances (spec, Section 16)")
    prs = pr.add_subparsers(dest="prcmd", required=True)
    prs.add_parser("list", help="every deposit, lock and claim held, one line each")
    prb = prs.add_parser("balance", help="each payer's deposits, open locks, debits and balance")
    prb.add_argument("payer", nargs="?", help="one payer, by peer name or NYM; all by default")
    lg = sub.add_parser("ledger", help="mint: print the ledger")
    lg.add_argument("--disputes", action="store_true", help="only disputed settlements")
    lg.add_argument("--json", action="store_true")
    iv = sub.add_parser("invoice", help="mint: sum a month's settlements (YYYY-MM; without one, last month), with the Steward's share apart")
    iv.add_argument("month", nargs="?", help="YYYY-MM in the Mint's local time; the default is the month before this one")
    iv.add_argument("--save", action="store_true", help="also write it to invoices/<month>.txt under the state directory, where GET /invoice serves it")
    iv.add_argument("--paid", nargs=2, metavar=("HOP", "NOTE"), help="note that the saved invoice's line paying HOP was settled, with the note in the parties' "
                                                                  "words (\"settled in person, Nov 2\"); kept beside the invoice and served with it")
    iv.add_argument("--payer", help="with --paid, which payer's line when more than one pays that hop")
    ow = sub.add_parser("owed", help="hop or payer: a month's invoice line computed from this node's own receipts, acks and announcements alone, "
                                     "then set against the Mint's invoice figure by figure (exit 0 agree, 1 differ, 2 not compared)")
    ow.add_argument("month", nargs="?", help="YYYY-MM in local time; the default is the month before this one")
    ow.add_argument("--invoice", metavar="FILE", help="the Mint's invoice as a text file, when the Mint cannot be reached through this node")
    ow.add_argument("--purchases", metavar="FILE", help="the Mint's purchases.jsonl, when it cannot be reached; without purchases no premium is computed")
    ow.add_argument("--links", metavar="FILE", help="announcements to settle under (a payer keeps none; the Mint's /links otherwise)")
    ow.add_argument("--receipts", metavar="FILE", help="the payer's receipts.jsonl, added to this node's own copies to fill a gap in them")
    ow.add_argument("--acks", metavar="FILE", help="the hop's acks.jsonl, added to this node's own copies to fill a gap in them")
    ow.add_argument("--prepay", metavar="FILE", help="the Mint's prepay.jsonl, when it cannot be reached; without it no window is rebuilt as covered")
    ow.add_argument("--price", type=float, default=DEFAULT_PRICE_USD_PER_GB, help="the default price when no announcement is in force")
    ow.add_argument("--fee", type=float, default=DEFAULT_STEWARD_FEE, help="the Steward's fraction f")
    vf = sub.add_parser("verify", help="check the signature on receipt files (no node state needed)")
    vf.add_argument("files", nargs="+")
    st = sub.add_parser("settle", help="resolve one window offline from a receipt file and an ack file, or, with --window, "
                                       "from this node's own copies of that window (receipts.jsonl, acks.jsonl, links.jsonl)")
    st.add_argument("receipt", nargs="?")
    st.add_argument("ack", nargs="?")
    st.add_argument("--window", metavar="MOMENT", help="on a hop or a payer: settle the window of my own copies that holds this moment, "
                                                       "Unix seconds or YYYY-MM-DDTHH:MM in local time, by the Mint's rule, with no Mint involved")
    st.add_argument("--link", help="the announcement in force (a JSON file); without it, the pilot defaults (not with --window, which finds it)")
    st.add_argument("--price", type=float, default=DEFAULT_PRICE_USD_PER_GB, help="the default price when there is no announcement")
    st.add_argument("--fee", type=float, default=DEFAULT_STEWARD_FEE, help="the Steward's fraction f")
    rp = sub.add_parser("report", help="mint: post the ledger's public summary, signed with the Mint's key, to the status feed "
                                       "(status_url in the config, or BWKIT_STATUS_URL: the feed's /report)")
    rp.add_argument("--print", dest="print_only", action="store_true", help="print the signed record instead of posting it")
    rp.add_argument("--url", help="post here instead of status_url")
    mp = sub.add_parser("map", help="the membership's map as a signed record: what a status feed lists Mints by")
    mps = mp.add_subparsers(dest="mapcmd", required=True)
    mpp = mps.add_parser("post", help="mint: sign the map (BWKIT_MAP, or `map` in the config) and post it to the feed's /map")
    mpp.add_argument("--print", dest="print_only", action="store_true", help="print the signed record instead of posting it")
    mpp.add_argument("--url", help="post here instead of the /map beside status_url")
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
    rb.add_argument("--prepay", metavar="FILE", help="prepay.jsonl, the Mint's prepayment registry; without it no window is rebuilt as covered and no claim settles")
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
    if args.cmd == "settle" and args.window:
        return settle_own_window(Node.from_env(), parse_moment(args.window), args.price, args.fee)
    if args.cmd == "settle":
        if not (args.receipt and args.ack):
            raise SystemExit("settle takes a receipt file and an ack file, or --window <moment> on a hop or a payer")
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
        prepay, bad_pp = read_jsonl(args.prepay) if args.prepay else ([], [])
        names = (os.path.basename(args.receipts), os.path.basename(args.acks), os.path.basename(args.links or "links.jsonl"),
                 os.path.basename(args.purchases or "purchases.jsonl"), os.path.basename(args.prepay or "prepay.jsonl"))
        place_of = None
        if args.map:
            with open(args.map) as f:
                map_obj = json.load(f)
            peers = {}
            if args.peers:
                with open(args.peers) as f:
                    peers = json.load(f)
            place_of = place_lookup(map_obj, peers)
        entries, rep = rebuild(receipts, acks, links, args.price, args.fee, names=names, purchases=purchases, place_of=place_of, prepay=prepay)
        bad_p = bad_p + bad_pp
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
    elif args.cmd == "deposit":
        if node.role != "mint":
            raise SystemExit("only the Mint signs a deposit")
        node.load_index()
        nym = args.payer if args.payer.startswith("ed25519:") else node.nym_by_name(args.payer)
        amount = int(round(args.usd * MICRO))
        if amount <= 0:
            raise SystemExit("the amount is a positive number of dollars (at least 0.000001)")
        body = {"kind": "deposit", "proto": PROTO, "mint": node.nym, "payer": nym, "amount": amount, "unit": "USD",
                "posted_at": int(time.time()), "note": args.note.strip()}
        rec = dict(body, id=prepay_id(body))
        rec["sig"] = sign_prepay(node.priv, rec)
        # through the Mint's own server when it runs, so its index sees the deposit at once; straight into the file when it does not
        own = (node.config.get("steward_mint") or "http://127.0.0.1:%d" % int(node.config.get("port", DEFAULT_PORT))).rstrip("/")
        try:
            status, ans = http_post(own + "/prepay", rec)
            if status != 200:
                raise SystemExit("refused: %s" % (ans.get("detail") or ans.get("error") if isinstance(ans, dict) else ans))
            node.load_index()
        except (urlerror.URLError, socket.timeout, OSError):
            try:
                node.register_prepay(rec)
            except (Decline, ValueError) as e:
                raise SystemExit("refused: %s" % e)
        bal = node.prepay_balance(nym)
        print("deposit %s: %s for %s (%s); balance now %s" % (rec["id"][:16], micro_usd(amount), node.peers().get(nym, nym[:20]), rec["note"], micro_usd(bal["balance"])))
    elif args.cmd == "prepay":
        if node.role != "mint":
            raise SystemExit("the registry is the Mint's; a payer reads its balance through its proxy (/mint/balance)")
        node.load_index()
        peers = node.peers()
        who = lambda n: peers.get(n, n[:20])
        if args.prcmd == "list":
            n = 0
            for rec in node.load_prepay():
                n += 1
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(rec["posted_at"]))
                if rec["kind"] == "deposit":
                    print("%s  deposit %s  %s for %s: %s" % (when, rec["id"][:16], micro_usd(rec["amount"]), who(rec["payer"]), rec["note"]))
                elif rec["kind"] == "lock":
                    print("%s  lock    %s  %s by %s with %s for the window at %s%s" % (
                        when, rec["id"][:16], micro_usd(rec["amount"]), who(rec["payer"]), who(rec["hop"]),
                        time.strftime("%Y-%m-%d %H:%M", time.localtime(rec["t_first"])), ", countersigned" if "mint_sig" in rec else ""))
                else:
                    print("%s  claim   %s  %s counts %d bytes on lock %s of %s" % (when, rec["id"][:16], who(rec["hop"]), rec["bytes"], rec["lock"][:16], who(rec["payer"])))
            if not n:
                print("no deposit, lock or claim held; `bwkit deposit <payer> <usd> \"<note>\"` is the first")
        else:
            payers = [args.payer if args.payer.startswith("ed25519:") else node.nym_by_name(args.payer)] if args.payer else node.prepay_payers()
            if not payers:
                print("no payer has a balance here")
            for nym in payers:
                b = node.prepay_balance(nym)
                print("%s  deposits %s  locked %s (%d open)  debited %s  balance %s" % (
                    who(nym), micro_usd(b["deposits"]), micro_usd(b["locked"]), len(b["open_locks"]), micro_usd(b["debited"]), micro_usd(b["balance"])))
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
                done = book and (rec["id"] in book.closed or time.time() > rec["deadline"] or u.get("windows", 0) >= rec["windows"])
                back = round(max(0.0, book.pool(rec)), 6) if done else 0.0
                who = "the Steward" if rec["poster"] == steward else peers.get(rec["poster"], rec["poster"][:20])
                print("%s  %-13s %s  %s -> %s%s  >=%s  %s/GB  %d window%s%s  escrow %s%s  until %s" % (
                    rec["id"][:16], rec["program"], who, rec["route"]["from"] if rec["route"]["from"] and not rec["route"]["from"].startswith("ed25519:") else who,
                    peers.get(rec["route"]["to"], rec["route"]["to"]), (" !via " + ",".join(rec["route"]["not_via"])) if rec["route"]["not_via"] else "",
                    fmt_bytes(rec["min_bytes"]), rec["premium_per_gb"], rec["windows"], "" if rec["windows"] == 1 else "s",
                    (", %d used" % u["windows"]) if u.get("windows") else "", book.total_escrow(rec) if book else rec["escrow"],
                    (", %s paid" % round(u.get("own", 0.0) + u.get("steward", 0.0), 6)) if u else "",
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(rec["deadline"])) + (", closed" if book and rec["id"] in book.closed else "")
                    + ((", %s returned" % back) if back else "")))
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
                    e["t_first"], e["t_last"], fmt_bytes(e["payer_bytes"]) if e["payer_bytes"] is not None else "no count", fmt_bytes(e["hop_bytes"]),
                    fmt_bytes(e["settled_bytes"]), e["amount_usd"],
                    "  fee $%.6f  margin %s%s" % (e["fee_usd"], e["margin"], "" if e.get("link") else " (default)") if "fee_usd" in e else ""))
            else:
                r = e["receipt"]
                print("%s  %-7s from %s  %d..%d  %s  sig %s" % (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(e["received_at"])), e["kind"], e["from"],
                    r["t_first"], r["t_last"], fmt_bytes(r["bytes"]), e["sig"][:12]))
    elif args.cmd == "invoice":
        month = args.month or previous_month()
        if not re.match(r"^\d{4}-\d{2}$", month):
            raise SystemExit("the month is YYYY-MM, as in 2026-10")
        if args.paid:
            if node.role != "mint":
                raise SystemExit("only the Mint keeps the paid mark")
            rec = node.invoice_mark(month, args.paid[0], args.paid[1], payer=args.payer)
            print(node.mark_line(rec))
            return 0
        print(node.invoice_text(month), end="")
        if args.save:
            print("saved to %s" % node.invoice_save(month))
        for rec in node.invoice_marks(month):
            print(node.mark_line(rec))
    elif args.cmd == "owed":
        month = args.month or previous_month()
        if not re.match(r"^\d{4}-\d{2}$", month):
            raise SystemExit("the month is YYYY-MM, as in 2026-10")
        text, rc = node.owed(month, purchases_path=args.purchases, invoice_path=args.invoice, links_path=args.links, price=args.price, fee=args.fee,
                             receipts_path=args.receipts, acks_path=args.acks, prepay_path=args.prepay)
        print(text, end="")
        return rc
    elif args.cmd == "prove":
        if node.role != "mint":
            raise SystemExit("only the Mint proves")
        print(json.dumps(node.proof(parse_hour(args.window)), indent=1, sort_keys=True))
    elif args.cmd == "report":
        if node.role != "mint":
            raise SystemExit("only the Mint reports")
        if args.print_only:
            print(json.dumps(node.status_record(service=node.service_up()), indent=1, sort_keys=True))
            return 0
        if not (args.url or node.status_url()):
            log("no status URL (status_url in the config, or BWKIT_STATUS_URL): nothing posted")
            return 0
        try:
            status, body = node.report(args.url)
        except (urlerror.URLError, socket.timeout, OSError) as e:
            log("status feed unreachable: %s" % e)
            return 1
        if not 200 <= status < 300:
            log("status feed refused the report: %s %s" % (status, body))
            return 1
        log("status posted: %s" % json.dumps(body, sort_keys=True))
    elif args.cmd == "map":
        if node.role != "mint":
            raise SystemExit("only the Mint signs the map")
        rec = node.map_record()
        if args.print_only:
            print(json.dumps(rec, indent=1, sort_keys=True))
            return 0
        url = args.url or node.map_url()
        if not url:
            raise SystemExit("no map URL: set status_url in the config to the feed's /report (the map goes to its /map), or pass --url")
        try:
            status, body = node.post_map(url, rec)
        except (urlerror.URLError, socket.timeout, OSError) as e:
            log("status feed unreachable: %s" % e)
            return 1
        if not 200 <= status < 300:
            log("status feed refused the map: %s %s" % (status, body))
            return 1
        log("map posted: %s" % json.dumps(body, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
