#!/usr/bin/env python3
"""The bwl/1 codec against the spec's vectors: python3 radio/test_wire.py (from the kit's root; bwkit verifies)."""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import wire   # noqa: E402
import bwkit  # noqa: E402

VECTORS = os.path.join(os.path.dirname(HERE), "spec", "vectors")


def vector(name):
    with open(os.path.join(VECTORS, name)) as f:
        return json.load(f)


class Codec(unittest.TestCase):
    def setUp(self):
        keys = vector("keys.json")
        self.peers = {keys[r]["nym"]: keys[r]["name"] for r in ("payer", "hop", "mint")}
        self.receipt = vector("receipt.json")
        self.ack = vector("ack.json")

    def test_the_json_forms_do_not_fit_and_the_wire_forms_do(self):
        compact = lambda d: len(json.dumps(d, sort_keys=True, separators=(",", ":")))   # noqa: E731
        self.assertGreater(compact(self.receipt), wire.MAX_PAYLOAD)
        self.assertGreater(compact(self.ack), wire.MAX_PAYLOAD)
        self.assertEqual(len(wire.encode(self.receipt)), 131)
        self.assertEqual(len(wire.encode(self.ack)), 195)
        self.assertLessEqual(len(wire.encode(self.ack)), wire.MAX_PAYLOAD)

    def test_a_receipt_round_trips_and_verifies(self):
        packet = wire.encode(self.receipt)
        self.assertEqual(wire.kind_of(packet), wire.RECEIPT)
        env = wire.decode(packet, self.peers)
        self.assertEqual(env["receipt"], self.receipt["receipt"])      # the eight fields, exactly
        self.assertEqual(env["sig"], self.receipt["sig"])
        self.assertEqual((env["kind"], env["proto"], env["from"], env["impl"], env["meter"]), ("receipt", 1, "vec-payer", "bwl/1", None))
        self.assertEqual(bwkit.check_envelope(env), self.receipt["receipt"]["payer"])   # the signature holds on the rebuilt envelope

    def test_an_ack_round_trips_and_verifies(self):
        packet = wire.encode(self.ack)
        self.assertEqual(wire.kind_of(packet), wire.ACK)
        env = wire.decode(packet, self.peers)
        self.assertEqual(env["receipt"], self.ack["receipt"])
        self.assertEqual(env["receipt"]["prev_sig"], self.receipt["sig"])
        self.assertEqual(env["from"], "vec-hop")
        self.assertEqual(bwkit.check_envelope(env), self.ack["receipt"]["hop"])

    def test_a_tampered_packet_fails_the_kits_check(self):
        packet = bytearray(wire.encode(self.receipt))
        packet[2 + 8 + 8 + 1 + 32 + 7] ^= 1                            # one bit of `bytes`
        env = wire.decode(bytes(packet), self.peers)
        with self.assertRaises(ValueError):
            bwkit.check_envelope(env)

    def test_an_unknown_or_ambiguous_fingerprint_is_refused(self):
        packet = wire.encode(self.receipt)
        with self.assertRaises(wire.WireError):
            wire.decode(packet, {k: v for k, v in self.peers.items() if v != "vec-payer"})
        # two peers whose keys share their first eight bytes: neither is named
        payer = self.receipt["receipt"]["payer"]
        twin = wire.nym_of_pub(wire.pub_of_nym(payer)[:8] + bytes(24))
        with self.assertRaises(wire.WireError):
            wire.decode(packet, dict(self.peers, **{twin: "vec-twin"}))

    def test_not_a_packet(self):
        for bad in (b"", b"\x00\x01", b"\xb1\x09" + bytes(20), wire.encode(self.receipt)[:-1], wire.encode(self.receipt) + b"\x00"):
            with self.assertRaises(wire.WireError):
                wire.decode(bad, self.peers)
        with self.assertRaises(wire.WireError):
            wire.encode({"kind": "status"})
        bad = json.loads(json.dumps(self.receipt))
        bad["receipt"]["prev_sig"] = self.receipt["sig"]
        with self.assertRaises(wire.WireError):
            wire.encode(bad)                                            # a receipt with a prev_sig is not a receipt

    def test_delivered_and_beacon(self):
        d = wire.delivered(self.receipt["sig"], wire.STORED)
        self.assertEqual(len(d), 12)
        self.assertEqual(wire.read_delivered(d), {"sig_prefix": wire.b64u_dec(self.receipt["sig"])[:8], "status": 0, "reason": 0})
        self.assertTrue(wire.matches(self.receipt["sig"], d))
        self.assertFalse(wire.matches(self.ack["sig"], d))
        b = wire.beacon(self.receipt["receipt"]["hop"], 1800007200, True, 3, 1800007200)
        self.assertEqual(len(b), 20)
        self.assertEqual(wire.read_beacon(b, self.peers), {"nym": self.receipt["receipt"]["hop"], "name": "vec-hop", "time": 1800007200,
                                                           "serving": True, "outbox": 3, "last_t_last": 1800007200})
        self.assertIsNone(wire.read_beacon(wire.beacon(self.receipt["receipt"]["payer"], 1, False, 0, None), {})["name"])

    def test_every_packet_fits_the_largest_values(self):
        big = json.loads(json.dumps(self.ack))
        big["receipt"]["bytes"] = (1 << 64) - 1
        big["receipt"]["t_first"] = big["receipt"]["t_last"] = (1 << 32) - 1
        big["receipt"]["hop_index"] = 255
        self.assertEqual(len(wire.encode(big)), 195)
        self.assertEqual(wire.decode(wire.encode(big), self.peers)["receipt"]["bytes"], (1 << 64) - 1)
        big["receipt"]["bytes"] = 1 << 64
        with self.assertRaises(wire.WireError):
            wire.encode(big)


if __name__ == "__main__":
    unittest.main(verbosity=1)
