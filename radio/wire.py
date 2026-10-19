"""bwl/1: the kit's signed records on the air, one LoRa packet each (radio/PLAN.md, Section 2).

A receipt or an acknowledgement travels as a fixed binary form of the spec's eight-field tuple with the
signature, naming the payer and the hop by an 8-byte fingerprint of their keys; the receiver, holding the peer
list, rebuilds the JSON envelope the Mint and the hop already understand and verifies the signature over the
full NYMs as for any other envelope. No dependencies: the standard library only.

    packet := magic (0xB1) · type · body
    receipt   (1): payer fp 8 · hop fp 8 · hop_index 1 · merkle_root 32 · bytes 8 · t_first 4 · t_last 4 · sig 64
    ack       (2): the same with prev_sig 64 before sig
    delivered (3): sig prefix 8 · status 1 · reason 1
    beacon    (4): fp 8 · time 4 · flags 1 · outbox 1 · last_t_last 4

Integers are big-endian. Every packet fits a Meshtastic data payload (MAX_PAYLOAD).
"""

import base64
import struct

MAGIC = 0xB1
RECEIPT, ACK, DELIVERED, BEACON = 1, 2, 3, 4
MAX_PAYLOAD = 237                    # Meshtastic's DATA_PAYLOAD_LEN; confirm on the bench
FP_LEN = 8
IMPL = "bwl/1"
STORED, DUPLICATE, REFUSED = 0, 1, 2  # a delivered packet's status
FLAG_SERVING, FLAG_OUTBOX = 1, 2      # a beacon's flags

_HEAD = struct.Struct(">BB")
_TUPLE = struct.Struct(">8s8sB32sQII")   # fp, fp, hop_index, root, bytes, t_first, t_last
_DELIVERED = struct.Struct(">8sBB")
_BEACON = struct.Struct(">8sIBBI")


class WireError(ValueError):
    """A packet that is not bwl/1, or names a peer the receiver does not hold."""


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s):
    if not isinstance(s, str):
        raise WireError("not base64url")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError):
        raise WireError("not base64url")


def pub_of_nym(nym):
    """The 32 raw bytes of an ed25519 NYM."""
    if not isinstance(nym, str) or not nym.startswith("ed25519:"):
        raise WireError("not an ed25519 NYM")
    raw = b64u_dec(nym[len("ed25519:"):])
    if len(raw) != 32:
        raise WireError("NYM is not 32 bytes")
    return raw


def nym_of_pub(raw):
    return "ed25519:" + b64u(raw)


def fingerprint(nym):
    """The first 8 bytes of the key: how a packet names a peer the receiver already holds."""
    return pub_of_nym(nym)[:FP_LEN]


def _u(v, name, bits):
    if not isinstance(v, int) or isinstance(v, bool) or v < 0 or v >= 1 << bits:
        raise WireError("%s must be a %d-bit non-negative integer" % (name, bits))
    return v


def encode(env):
    """A receipt or acknowledgement envelope (spec, Sections 4 and 5) as one packet. The envelope is taken as the kit
    wrote it; its shape and signature are the kit's to check before sending."""
    if not isinstance(env, dict) or env.get("kind") not in ("receipt", "ack"):
        raise WireError("only a receipt or an ack goes on the air")
    r = env.get("receipt")
    if not isinstance(r, dict):
        raise WireError("no receipt in the envelope")
    try:
        root = bytes.fromhex(r["merkle_root"])
    except (KeyError, TypeError, ValueError):
        raise WireError("merkle_root must be hex")
    if len(root) != 32:
        raise WireError("merkle_root must be 32 bytes")
    sig = b64u_dec(env.get("sig"))
    if len(sig) != 64:
        raise WireError("sig must be 64 bytes")
    body = _TUPLE.pack(fingerprint(r["payer"]), fingerprint(r["hop"]), _u(r["hop_index"], "hop_index", 8), root,
                       _u(r["bytes"], "bytes", 64), _u(r["t_first"], "t_first", 32), _u(r["t_last"], "t_last", 32))
    if env["kind"] == "receipt":
        if r.get("prev_sig") != "":
            raise WireError("a receipt has no prev_sig")
        packet = _HEAD.pack(MAGIC, RECEIPT) + body + sig
    else:
        prev = b64u_dec(r.get("prev_sig"))
        if len(prev) != 64:
            raise WireError("an ack's prev_sig must be 64 bytes")
        packet = _HEAD.pack(MAGIC, ACK) + body + prev + sig
    assert len(packet) <= MAX_PAYLOAD
    return packet


def kind_of(packet):
    """RECEIPT, ACK, DELIVERED or BEACON, or a WireError."""
    if len(packet) < _HEAD.size or packet[0] != MAGIC:
        raise WireError("not a bwl/1 packet")
    t = packet[1]
    if t not in (RECEIPT, ACK, DELIVERED, BEACON):
        raise WireError("unknown packet type %d" % t)
    return t


def peer_table(peers):
    """The receiver's peer list (the kit's peers.json: NYM -> name) as fingerprint -> (NYM, name). A fingerprint two
    peers share maps to None, so a packet naming it is refused rather than guessed."""
    table = {}
    for nym, name in peers.items():
        fp = fingerprint(nym)
        table[fp] = None if fp in table else (nym, name)
    return table


def decode(packet, peers):
    """A receipt or ack packet back into the kit's envelope, the peers resolved from the receiver's peer list. The
    signature is not checked here: the caller runs the rebuilt envelope through bwkit.check_envelope as it would any
    envelope that arrived over HTTP."""
    t = kind_of(packet)
    if t not in (RECEIPT, ACK):
        raise WireError("not a receipt or an ack")
    want = _HEAD.size + _TUPLE.size + (64 if t == RECEIPT else 128)
    if len(packet) != want:
        raise WireError("a %s packet is %d bytes, not %d" % ("receipt" if t == RECEIPT else "ack", want, len(packet)))
    fp_payer, fp_hop, hop_index, root, nbytes, t_first, t_last = _TUPLE.unpack_from(packet, _HEAD.size)
    tail = packet[_HEAD.size + _TUPLE.size:]
    table = peer_table(peers)
    payer, hop = table.get(fp_payer), table.get(fp_hop)
    if payer is None or hop is None:
        raise WireError("a fingerprint names no peer in the list, or two")
    receipt = {"payer": payer[0], "hop": hop[0], "hop_index": hop_index, "merkle_root": root.hex(),
               "bytes": nbytes, "t_first": t_first, "t_last": t_last,
               "prev_sig": "" if t == RECEIPT else b64u(tail[:64])}
    sig = b64u(tail[-64:])
    signer = payer if t == RECEIPT else hop
    return {"kind": "receipt" if t == RECEIPT else "ack", "proto": 1, "from": signer[1], "impl": IMPL,
            "receipt": receipt, "sig": sig, "meter": None}


def delivered(sig, status, reason=0):
    """The Mint's gateway's answer to a record: the first 8 bytes of its signature, STORED, DUPLICATE or REFUSED, and a
    reason code of the gateway's (0 for none)."""
    return _HEAD.pack(MAGIC, DELIVERED) + _DELIVERED.pack(b64u_dec(sig)[:8], _u(status, "status", 8), _u(reason, "reason", 8))


def read_delivered(packet):
    if kind_of(packet) != DELIVERED or len(packet) != _HEAD.size + _DELIVERED.size:
        raise WireError("not a delivered packet")
    prefix, status, reason = _DELIVERED.unpack_from(packet, _HEAD.size)
    return {"sig_prefix": prefix, "status": status, "reason": reason}


def beacon(nym, now, serving, outbox, last_t_last):
    """A node's liveness, every ten minutes: who, when, whether it serves, how many records wait, its latest window."""
    flags = (FLAG_SERVING if serving else 0) | (FLAG_OUTBOX if outbox else 0)
    return _HEAD.pack(MAGIC, BEACON) + _BEACON.pack(fingerprint(nym), _u(now, "now", 32), flags, min(_u(outbox, "outbox", 32), 255),
                                                    _u(last_t_last or 0, "last_t_last", 32))


def read_beacon(packet, peers):
    if kind_of(packet) != BEACON or len(packet) != _HEAD.size + _BEACON.size:
        raise WireError("not a beacon")
    fp, now, flags, outbox, last = _BEACON.unpack_from(packet, _HEAD.size)
    who = peer_table(peers).get(fp)
    return {"nym": who[0] if who else None, "name": who[1] if who else None, "time": now,
            "serving": bool(flags & FLAG_SERVING), "outbox": outbox, "last_t_last": last or None}


def matches(sig, delivered_packet):
    """True when a delivered packet answers the record with this signature."""
    return read_delivered(delivered_packet)["sig_prefix"] == b64u_dec(sig)[:8]
