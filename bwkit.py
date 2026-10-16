#!/usr/bin/env python3
"""bwkit: the Bandwidth Swap Phase 0 receipt daemon and Mint, in one file.

Needs Python 3.9+ and the `cryptography` package (Debian: python3-cryptography).

Three roles, chosen by "role" in the config file:

  payer  (bw-house)  Every hour: read the tunnel's byte counters, build the
                     Figure 2 receipt for the window, sign it, ask the hop
                     to acknowledge it, send receipt and ack to the Mint.
  hop    (bw-forest) Serve /ack: check the payer's signature and clock,
                     read its own counters for the same window, sign its own
                     count with prev_sig = the payer's signature, send that
                     ack to the Mint too, and return it.
  mint   (bw-mint)   Serve /receipt: verify every signature, append each
                     receipt and ack to an append-only ledger, and settle a
                     window once both are there, at the smaller of the two
                     signed counts.

The Mint also reports: `bwkit report` (the bwkit-status timer, every two
minutes) summarises the ledger per node, names and roles only, and posts it
to the status page's Worker, so bandwidthswap.com can say which nodes are up
while nothing reaches the LAN.

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
outbox/, sent/.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerror
from urllib import request as urlrequest

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

VERSION = "0.1.2"
FIELDS = ("payer", "hop", "hop_index", "merkle_root", "bytes", "t_first", "t_last", "prev_sig")
DOMAIN = b"bandwidthswap/receipt/v1\n"   # prefixed to every signed message
CLOCK_TOLERANCE_S = 5                   # OPEN-QUESTIONS B9 default: 5 seconds, decline
AGREE_PCT = 1.0                         # two honest meters of one window differ by less than this
AGREE_MIN_BYTES = 65536                 # or by less than this, whichever is larger
DEFAULT_PORT = 8710
DEFAULT_PRICE_USD_PER_GB = 0.10         # the lab's posted price; dollars, no token
HTTP_TIMEOUT_S = 10
HOP_FLUSH_DELAY_S = 5                   # the hop sends its own copy of an ack this long after answering the payer
FLUSH_EVERY_S = 300                     # and retries whatever is left in its outbox this often
MAX_BODY = 1 << 20
STATUS_TIMEOUT_S = 10                   # posting the status report
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
        if not isinstance(r[k], int) or isinstance(r[k], bool) or r[k] < 0:
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
    """Raise ValueError when text carries an address. A status report is names and roles only."""
    for pat in PRIVATE_PATTERNS:
        m = pat.search(text)
        if m:
            raise ValueError("a status report may not carry an address (%s)" % m.group(0))


def resolve(receipt_env, ack_env, price_usd_per_gb=DEFAULT_PRICE_USD_PER_GB, now=None):
    """Settle one window from a payer's receipt and a hop's ack. Pure: needs nothing else.

    Raises ValueError when the two don't describe the same window or a
    signature fails. Returns the settlement record.
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
    x, y = r["bytes"], a["bytes"]
    settled = min(x, y)
    agree = abs(x - y) <= max(AGREE_MIN_BYTES, AGREE_PCT / 100.0 * max(x, y))
    return {
        "kind": "settlement",
        "t": int(now if now is not None else time.time()),
        "payer": r["payer"], "hop": r["hop"], "hop_index": r["hop_index"],
        "t_first": r["t_first"], "t_last": r["t_last"],
        "payer_bytes": x, "hop_bytes": y, "settled_bytes": settled,
        "status": "settled" if agree else "disputed",
        "rule": "the smaller of the two signed counts; neither party is bound beyond what it signed",
        "receipt_sig": receipt_env["sig"], "ack_sig": ack_env["sig"],
        "price_usd_per_gb": price_usd_per_gb,
        "amount_usd": round(settled / 1e9 * price_usd_per_gb, 6),
    }


# ---------------------------------------------------------------- http

def http_post(url, body, timeout=HTTP_TIMEOUT_S, headers=None):
    data = json.dumps(body).encode()
    h = {"Content-Type": "application/json"}
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
    with urlrequest.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read().decode()


class Decline(Exception):
    """A request refused for a protocol reason (HTTP 409)."""


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
        return {"kind": "receipt", "from": self.name, "bwkit": VERSION,
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

    # hop

    def ack(self, env, now=None):
        """Acknowledge a payer's receipt with our own count. Raises Decline or ValueError."""
        now = int(now if now is not None else time.time())
        signer = check_envelope(env)
        if env["kind"] != "receipt":
            raise ValueError("expected a payer's receipt")
        r = env["receipt"]
        if r["hop"] != self.nym:
            raise ValueError("this receipt names another hop")
        if signer not in self.peers():
            raise Decline("unknown payer %s" % signer)
        if abs(now - r["t_last"]) > CLOCK_TOLERANCE_S:
            raise Decline("clock: payer's t_last is %d s from ours (tolerance %d s)" % (now - r["t_last"], CLOCK_TOLERANCE_S))
        try:
            _, nbytes, meter = self.window(now)
        except NoBaseline:
            self.sample(now)
            raise Decline("no baseline here; took one now, acks start with the next window")
        a = dict(r)
        a["bytes"] = nbytes
        a["prev_sig"] = env["sig"]
        ack = {"kind": "ack", "from": self.name, "bwkit": VERSION,
               "receipt": a, "sig": sign_receipt(self.priv, a), "meter": meter}
        with self.lock:
            self.commit_sample(r["t_last"], meter)
            self.append("acks.jsonl", ack)
            self.outbox_put(ack)
        log("ack for %s %d..%d: payer says %d, we say %d" % (env.get("from"), r["t_first"], r["t_last"], r["bytes"], nbytes))
        if self.config.get("mint"):
            self.flush_later(HOP_FLUSH_DELAY_S)   # our own copy, in case the payer drops an ack it dislikes
        return ack

    # mint

    def load_index(self, skip_partial=False):
        """Rebuild the Mint's view from the ledger.

        skip_partial: a reader beside the running Mint (the status reporter)
        may catch the last line half written; skip it rather than fail.
        """
        ix = {"by_sig": {}, "receipt_by_window": {}, "acks_by_prev": {}, "settled": {}}
        p = self.path("ledger.jsonl")
        if os.path.exists(p):
            with open(p) as f:
                lines = f.read().split("\n")
            for i, line in enumerate(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    if skip_partial and i == len(lines) - 1:
                        break
                    raise
                self._index_line(ix, entry)
        self.index = ix
        return ix

    @staticmethod
    def _index_line(ix, entry):
        k = entry.get("kind")
        if k == "receipt":
            ix["by_sig"][entry["sig"]] = entry
            ix["receipt_by_window"][window_key(entry["receipt"])] = entry
        elif k == "ack":
            ix["by_sig"][entry["sig"]] = entry
            ix["acks_by_prev"][entry["receipt"]["prev_sig"]] = entry
        elif k == "settlement":
            ix["settled"][entry["receipt_sig"]] = entry

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
        stored, duplicates, settlements = [], [], []
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
                entry = {"kind": env["kind"], "received_at": now, "from": peers[signer],
                         "receipt": env["receipt"], "sig": env["sig"], "meter": env.get("meter")}
                self.ledger_append(entry)
                stored.append(env["sig"])
            for env in envs:
                s = self.try_settle(env, now)
                if s:
                    settlements.append(s)
        if not stored and duplicates and not settlements:
            raise Decline("duplicate")
        return {"stored": stored, "duplicates": duplicates, "settlements": settlements}

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
        try:
            s = resolve(rec, ack, self.config.get("price_usd_per_gb", DEFAULT_PRICE_USD_PER_GB), now)
        except ValueError as e:
            log("receipt %s and ack %s do not settle: %s" % (rec["sig"][:12], ack["sig"][:12], e))
            return None
        self.ledger_append(s)
        log("%s %s..%d: payer %d, hop %d, settled %d bytes ($%.6f)" % (
            s["status"], s["t_first"], s["t_last"], s["payer_bytes"], s["hop_bytes"], s["settled_bytes"], s["amount_usd"]))
        return s

    def ledger_lines(self):
        p = self.path("ledger.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(l) for l in f if l.strip()]

    def pending(self):
        """Receipts with no ack yet, and acks with no receipt."""
        if self.index is None:
            self.load_index()
        ix = self.index
        no_ack = [e for s, e in ix["by_sig"].items() if e["kind"] == "receipt" and s not in ix["acks_by_prev"]]
        no_receipt = [e for e in ix["acks_by_prev"].values() if e["receipt"]["prev_sig"] not in ix["by_sig"]]
        return no_ack, no_receipt

    def invoice(self, month):
        """Sum the settlements of a month (YYYY-MM) per payer and hop, in local time."""
        out = {}
        for e in self.ledger_lines():
            if e.get("kind") != "settlement":
                continue
            if time.strftime("%Y-%m", time.localtime(e["t_last"])) != month:
                continue
            k = (e["payer"], e["hop"])
            o = out.setdefault(k, {"windows": 0, "disputed": 0, "settled_bytes": 0, "amount_usd": 0.0})
            o["windows"] += 1
            o["disputed"] += 1 if e["status"] == "disputed" else 0
            o["settled_bytes"] += e["settled_bytes"]
            o["amount_usd"] = round(o["amount_usd"] + e["amount_usd"], 6)
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

        Names and roles only: never a NYM, an address or a hostname beyond the
        nodes' names. Per node: when its last receipt (for the hop, its last
        ack) reached the Mint, the bytes it signed for its latest window, how
        many it has signed, how many of its windows settled and how many of
        those were disputed. The Mint's own line counts what it recorded.
        `service` is whether the Mint's server answered (service_up), None
        when not checked.
        """
        now = int(now if now is not None else time.time())
        self.load_index(skip_partial=True)
        ix = self.index
        peers = self.peers()
        order = {"payer": 0, "hop": 1, "mint": 2, "peer": 3}
        nodes = {}

        def node(name, role):
            n = nodes.get(name)
            if n is None:
                n = nodes[name] = {"name": name, "role": role, "last_receipt": None, "window_bytes": None,
                                   "window_t_first": None, "window_t_last": None,
                                   "receipts": 0, "windows": 0, "disputed": 0}
            elif order[role] < order[n["role"]]:
                n["role"] = role
            return n

        for name in peers.values():
            node(name, "peer")                      # a peer with nothing on the ledger yet is still listed
        entries = list(ix["by_sig"].values())
        for e in entries:
            n = node(e["from"], "payer" if e["kind"] == "receipt" else "hop")
            r = e["receipt"]
            n["receipts"] += 1
            if n["last_receipt"] is None or e["received_at"] > n["last_receipt"]:
                n["last_receipt"] = e["received_at"]
            if n["window_t_last"] is None or r["t_last"] > n["window_t_last"]:
                n["window_bytes"], n["window_t_first"], n["window_t_last"] = r["bytes"], r["t_first"], r["t_last"]
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
        latest = max(ix["settled"].values(), key=lambda st: (st["t_last"], st["t"]), default=None)
        if latest:
            me["window_bytes"], me["window_t_first"], me["window_t_last"] = latest["settled_bytes"], latest["t_first"], latest["t_last"]
        no_ack, no_receipt = self.pending()
        return {
            "v": 1, "generated_at": now, "bwkit": VERSION, "phase": 0, "window_s": 3600,
            "service": service,
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
        assert_public(json.dumps(rep, sort_keys=True))
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
            server_version = "bwkit/" + VERSION

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
                if self.path == "/status":
                    st = {"name": node.name, "role": node.role, "nym": node.nym, "bwkit": VERSION,
                          "time": int(time.time())}
                    if node.role == "mint":
                        no_ack, no_receipt = node.pending()
                        st["ledger_lines"] = len(node.index["by_sig"]) + len(node.index["settled"])
                        st["settlements"] = len(node.index["settled"])
                        st["disputed"] = sum(1 for s in node.index["settled"].values() if s["status"] == "disputed")
                        st["pending"] = {"receipts_without_ack": len(no_ack), "acks_without_receipt": len(no_receipt)}
                    else:
                        st["sample"] = node.state().get("sample")
                        st["outbox"] = len(os.listdir(node.path("outbox")))
                    return self.reply(200, st)
                if self.path == "/ledger" and node.role == "mint":
                    p = node.path("ledger.jsonl")
                    data = open(p, "rb").read() if os.path.exists(p) else b""
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
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
                    return self.reply(404, {"error": "no such path"})
                except Decline as e:
                    code = 409
                    msg = str(e)
                    if msg.startswith("unknown"):
                        code = 403
                    log("declined %s from %s: %s" % (self.path, self.client_address[0], msg))
                    return self.reply(code, {"error": msg.split(":")[0] if msg.startswith("clock") else msg, "detail": msg})
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


# ---------------------------------------------------------------- cli

def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.2f %s" % (n, unit)
        n /= 1000.0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bwkit", description="Bandwidth Swap Phase 0 receipt daemon and Mint")
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
    sv = sub.add_parser("serve", help="hop or mint: serve HTTP")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=None)
    sub.add_parser("status", help="what this node knows")
    lg = sub.add_parser("ledger", help="mint: print the ledger")
    lg.add_argument("--disputes", action="store_true", help="only disputed settlements")
    lg.add_argument("--json", action="store_true")
    iv = sub.add_parser("invoice", help="mint: sum a month's settlements (YYYY-MM)")
    iv.add_argument("month")
    vf = sub.add_parser("verify", help="check the signature on receipt files (no node state needed)")
    vf.add_argument("files", nargs="+")
    st = sub.add_parser("settle", help="resolve one window offline from a receipt file and an ack file")
    st.add_argument("receipt")
    st.add_argument("ack")
    st.add_argument("--price", type=float, default=DEFAULT_PRICE_USD_PER_GB)
    rp = sub.add_parser("report", help="mint: post the ledger's public summary to the status page "
                                       "(BWKIT_STATUS_URL and BWKIT_STATUS_TOKEN, from /etc/bwkit/status.env)")
    rp.add_argument("--print", dest="print_only", action="store_true", help="print the report instead of posting it")
    args = ap.parse_args(argv)

    if args.cmd == "verify":
        ok = True
        for fn in args.files:
            with open(fn) as f:
                env = json.load(f)
            try:
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
        s = resolve(rec, ack, args.price)
        print(json.dumps(s, indent=1, sort_keys=True))
        return 0

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
    elif args.cmd == "serve":
        node.serve(args.host, args.port)
    elif args.cmd == "status":
        print("%s (%s) %s" % (node.name, node.role, node.nym if node.priv else "no key"))
        if node.role == "mint":
            node.load_index()
            no_ack, no_receipt = node.pending()
            print("ledger: %d receipts and acks, %d settlements (%d disputed), %d receipts without ack, %d acks without receipt" % (
                len(node.index["by_sig"]), len(node.index["settled"]),
                sum(1 for s in node.index["settled"].values() if s["status"] == "disputed"), len(no_ack), len(no_receipt)))
        else:
            s = node.state().get("sample")
            print("sample: %s" % (("t %d, rx %d, tx %d" % (s["t"], s["rx"], s["tx"])) if s else "none (run `bwkit sample`)"))
            print("outbox: %d waiting" % len(os.listdir(node.path("outbox"))))
    elif args.cmd == "ledger":
        for e in node.ledger_lines():
            if args.disputes and not (e["kind"] == "settlement" and e["status"] == "disputed"):
                continue
            if args.json:
                print(json.dumps(e, sort_keys=True))
            elif e["kind"] == "settlement":
                print("%s  %s  %d..%d  payer %s  hop %s  settled %s  $%.6f" % (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(e["t_last"])), e["status"].upper(),
                    e["t_first"], e["t_last"], fmt_bytes(e["payer_bytes"]), fmt_bytes(e["hop_bytes"]),
                    fmt_bytes(e["settled_bytes"]), e["amount_usd"]))
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
        for (payer, hop), o in sorted(tot.items()):
            print("%s  %s pays %s: %d windows (%d disputed), %s, $%.4f" % (
                args.month, peers.get(payer, payer), peers.get(hop, hop), o["windows"], o["disputed"],
                fmt_bytes(o["settled_bytes"]), o["amount_usd"]))
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
