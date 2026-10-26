#!/usr/bin/env python3
"""The carrier against a fake mesh, with real nodes: python3 radio/test_radio.py (from the kit's root).

The lab of test_bwkit.py (a payer, a hop and a Mint in temp dirs, the hop and the Mint served on loopback), with the
payer's path to the hop and the hop's path to the Mint cut, as when the house's internet is down: the payer is the
Mint's gateway over the LAN. Packets cross an in-memory mesh the test delivers by hand, dropping or doubling some.
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import bwradio  # noqa: E402
import wire     # noqa: E402
import bwkit    # noqa: E402
from test_bwkit import Lab  # noqa: E402

bwkit.log = lambda msg: None   # the nodes' and the carrier's log lines, quiet here
bwradio.log = bwkit.log


class FakeMesh:
    """Radios by id; a packet sent to an id (or None, broadcast) waits in the queue until deliver() moves it."""
    def __init__(self):
        self.radios, self.queue, self.delivered = {}, [], []

    def radio(self, rid):
        r = FakeRadio(self, rid)
        self.radios[rid] = r
        return r

    def deliver(self, drop=None, double=None, now=None):
        """Deliver everything queued. drop(packet, dest) -> True loses it; double(...) -> True delivers it twice."""
        n = 0
        while self.queue:
            dest, data, src = self.queue.pop(0)
            if drop and drop(data, dest):
                continue
            targets = [r for rid, r in self.radios.items() if rid != src and (dest is None or rid == dest)]
            for r in targets:
                for _ in range(2 if double and double(data, dest) else 1):
                    self.delivered.append((src, r.id, data[1]))
                    r.cb(data, src, now) if r.cb else None
                    n += 1
        return n


class FakeRadio:
    def __init__(self, mesh, rid):
        self.mesh, self.id, self.cb = mesh, rid, None

    def on_receive(self, cb):
        self.cb = cb

    def send(self, dest, data):
        assert len(data) <= wire.MAX_PAYLOAD
        self.mesh.queue.append((dest, data, self.id))


class Outage(unittest.TestCase):
    """The house's internet is down: the payer cannot reach the hop over the tunnel, the hop cannot reach the Mint,
    the payer reaches the Mint over the LAN and is the gateway."""

    def setUp(self):
        self.lab = Lab()
        lab = self.lab
        lab.payer.config["hop_url"] = "http://127.0.0.1:1"      # the tunnel is down
        lab.hop.config["mint"] = "http://127.0.0.1:1"           # and so is the hop's own way to the Mint
        self.mesh = FakeMesh()
        lab.payer.config["radio"] = {"gateway": True, "peers": {lab.hop.nym: "!hop", lab.mint.nym: "!house"}}
        lab.hop.config["radio"] = {"gateway": False, "gateway_id": "!house", "self_url": lab.hop_url, "peers": {lab.payer.nym: "!house"}}
        self.payer = bwradio.Carrier(lab.payer, self.mesh.radio("!house"))
        self.hop = bwradio.Carrier(lab.hop, self.mesh.radio("!hop"))
        self.t0 = lab.t
        for c in (self.payer, self.hop):                        # the beacons have their own test; here they stay out of the queue
            c.last_beacon = self.t0 + 10 ** 9

    def tearDown(self):
        self.lab.close()

    def lines(self, node, name):
        p = node.path(name)
        return [json.loads(x) for x in open(p).read().splitlines() if x.strip()] if os.path.exists(p) else []

    def settled(self):
        self.lab.mint.load_index()
        return list(self.lab.mint.index["settled"].values())

    def run_hour(self, payer_bytes, hop_bytes, **deliver):
        """One window with the tunnel down: the receipt by radio, the ack back by radio, the Mint settles over the LAN."""
        lab = self.lab
        out = lab.hour(payer_bytes, hop_bytes)                  # the payer signs; the hop is unreachable; the receipt goes to the Mint over the LAN
        self.assertIsNone(out["ack"])
        now = lab.t
        self.payer.tick(now)                                    # too soon: the HTTP path gets RADIO_AFTER_S
        self.assertEqual(self.mesh.queue, [])
        self.payer.tick(now + bwradio.RADIO_AFTER_S)
        self.assertEqual([q[1][1] for q in self.mesh.queue], [wire.RECEIPT])
        self.mesh.deliver(now=now + 3, **deliver)              # the hop acks through its own /ack and answers by radio
        self.mesh.deliver(now=now + 5, **deliver)              # the ack reaches the payer, which is the gateway: the Mint gets it, the hop hears so
        self.mesh.deliver(now=now + 6, **deliver)
        for name in os.listdir(lab.hop.path("outbox")):         # the lab's clock, not the Mac's, on what the hop just wrote
            os.utime(lab.hop.path("outbox", name), (now, now))
        return out

    def test_a_window_settles_by_radio_while_the_internet_is_down(self):
        lab = self.lab
        out = self.run_hour(1_000, 1_000)
        receipt = out["receipt"]
        acks = self.lines(lab.payer, "acks.jsonl")
        self.assertEqual(len(acks), 1)
        self.assertEqual(acks[0]["receipt"]["prev_sig"], receipt["sig"])
        self.assertEqual(acks[0]["impl"], "bwl/1")              # rebuilt from the air, not the hop's JSON
        self.assertEqual(acks[0]["meter"], None)
        s = self.settled()
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0]["payer_bytes"], s[0]["hop_bytes"], s[0]["status"]), (1_000, 1_000, "settled"))
        self.assertEqual(self.payer.pending, {})                # the ask is answered
        kinds = [d[2] for d in self.mesh.delivered]
        self.assertEqual(kinds, [wire.RECEIPT, wire.ACK, wire.DELIVERED])   # a receipt out, an ack back, the gateway's word on it
        self.assertEqual(len(self.lines(lab.hop, "acks.jsonl")), 1)
        self.assertEqual(os.listdir(lab.hop.path("outbox")), [])            # the hop's own copy need not fly: the Mint has the ack
        self.assertEqual(self.lines(lab.hop, "sent.jsonl")[-1]["status"], "duplicate")

    def test_the_hops_own_copy_reaches_the_mint_through_the_gateway(self):
        lab = self.lab
        self.run_hour(1_000, 1_000, drop=lambda data, dest: data[1] == wire.DELIVERED)   # the gateway's word is lost on the air
        self.assertEqual(len(os.listdir(lab.hop.path("outbox"))), 1)   # so the hop's ack copy waits: its Mint is unreachable
        self.hop.tick(lab.t + 10)
        self.assertEqual(self.mesh.queue, [])                   # younger than GRACE_S: the IP path still had its chance
        # make the file old
        name = os.listdir(lab.hop.path("outbox"))[0]
        p = lab.hop.path("outbox", name)
        os.utime(p, (lab.t - 100, lab.t - 100))
        self.hop.last_sweep = None
        self.hop.tick(lab.t + 10)
        self.assertEqual([q[1][1] for q in self.mesh.queue], [wire.ACK])
        self.mesh.deliver(now=lab.t + 12)                       # the gateway posts it to the Mint: a duplicate of the payer's copy
        self.mesh.deliver(now=lab.t + 14)                       # the delivered packet comes back
        self.assertEqual(os.listdir(lab.hop.path("outbox")), [])
        self.assertEqual(os.listdir(lab.hop.path("sent")), [name])
        self.assertEqual(self.lines(lab.hop, "sent.jsonl")[-1]["status"], "duplicate")
        self.assertEqual(self.hop.pending, {})

    def test_a_doubled_receipt_gets_the_same_ack_once(self):
        lab = self.lab
        self.run_hour(1_000, 1_000, double=lambda data, dest: data[1] == wire.RECEIPT)
        self.assertEqual(len(self.lines(lab.hop, "acks.jsonl")), 1)      # held_ack: one ack, not two
        self.assertEqual(len(self.lines(lab.payer, "acks.jsonl")), 1)    # and the payer keeps it once
        self.assertEqual(len(self.settled()), 1)

    def test_a_lost_receipt_is_asked_again_within_the_clock_rule(self):
        lab = self.lab
        lab.hour(1_000, 1_000)
        now = lab.t
        self.payer.tick(now)                                    # first seen
        self.payer.tick(now + 2)                                # sent
        dropped = self.mesh.deliver(now=now + 3, drop=lambda data, dest: True)
        self.assertEqual(dropped, 0)
        self.payer.tick(now + 4)
        self.assertEqual(self.mesh.queue, [])                   # not yet: ASK_RETRY_S
        self.payer.tick(now + 2 + bwradio.ASK_RETRY_S)
        self.assertEqual([q[1][1] for q in self.mesh.queue], [wire.RECEIPT])
        self.mesh.deliver(now=now + 8)
        self.mesh.deliver(now=now + 9)
        self.assertEqual(len(self.settled()), 1)
        # and a receipt older than the give-up is left alone
        self.payer.first_seen.clear()
        self.payer.tick(now + bwradio.ASK_GIVE_UP_S + 100)
        self.assertEqual(self.mesh.queue, [])

    def test_a_declined_receipt_stops_the_asking(self):
        lab = self.lab
        lab.hour(1_000, 1_000)
        now = lab.t
        lab.hop_now = now + 60                                  # the hop's clock says the receipt is a minute late
        self.payer.tick(now)
        self.payer.tick(now + 2)
        self.mesh.deliver(now=now + 3)                          # the hop declines (clock) and says so
        self.mesh.deliver(now=now + 4)
        self.assertEqual([d[2] for d in self.mesh.delivered], [wire.RECEIPT, wire.DELIVERED])
        self.assertEqual(len(self.settled()), 0)
        self.assertIn(self.lines(lab.payer, "receipts.jsonl")[-1]["sig"], self.payer.declined)
        self.payer.tick(now + 20)
        self.assertEqual(self.mesh.queue, [])                   # the window is lost; nothing more goes on the air for it

    def test_strangers_and_noise_are_dropped(self):
        lab = self.lab
        lab.hour(1_000, 1_000)
        env = self.lines(lab.payer, "receipts.jsonl")[-1]
        stranger = self.mesh.radio("!stranger")
        bent = bytearray(wire.encode(env)); bent[-1] ^= 0x01                 # a corrupted signature byte (a flipped bit: always a change)
        stranger.send("!hop", bytes(bent))
        stranger.send("!hop", b"hello")                                      # not bwl/1
        bad = json.loads(json.dumps(env)); bad["receipt"]["bytes"] += 1      # re-encoded with the old signature
        stranger.send("!hop", wire.encode(bad))
        self.mesh.deliver(now=lab.t + 1)
        self.assertEqual(self.lines(lab.hop, "acks.jsonl"), [])
        self.assertEqual(self.mesh.queue, [])
        # an ack for a receipt this payer never signed
        other = Lab()
        try:
            other.hour(500, 500)
            foreign = self.lines(other.hop, "acks.jsonl")[-1]
            lab.payer.peer_add("other-hop", other.hop.nym)
            lab.payer.peer_add("other-payer", other.payer.nym)
            stranger.send("!house", wire.encode(foreign))
            self.mesh.deliver(now=lab.t + 2)
            self.assertEqual(self.lines(lab.payer, "acks.jsonl"), [])
        finally:
            other.close()

    def test_beacons_are_heard_and_written_by_the_gateway(self):
        lab = self.lab
        self.hop.last_beacon = None
        self.hop.tick(lab.t)
        self.assertEqual([q[1][1] for q in self.mesh.queue], [wire.BEACON])
        self.mesh.deliver(now=lab.t + 1)
        heard = self.payer.heard
        self.assertIn("bw-forest", heard)
        self.assertEqual((heard["bw-forest"]["radio"], heard["bw-forest"]["serving"], heard["bw-forest"]["outbox"]), ("!hop", True, 0))
        with open(lab.payer.path("radio.json")) as f:
            self.assertEqual(json.load(f)["heard"]["bw-forest"]["nym"], lab.hop.nym)
        self.hop.tick(lab.t + 60)
        self.assertEqual(self.mesh.queue, [])                   # not again until BEACON_S
        self.hop.tick(lab.t + bwradio.BEACON_S)
        self.assertEqual(len(self.mesh.queue), 1)


class Relayed(Outage):
    """The same outage, with the hop's board gone: a fourth node on the hop's own network (bw-mint2 next door, a Mint
    with no peers) has the board, and its carrier relays the receipt to the hop's /ack and the hop's ack back."""

    def setUp(self):
        super().setUp()
        lab = self.lab
        self.mesh.radios.pop("!hop")                            # the hop has no board
        self.payer.radio_of[lab.hop.nym] = "!relay"            # the payer's packets for the hop go to the relay's board
        self.relay_node = bwkit.Node(os.path.join(lab.root, "mint2"), {"role": "mint", "name": "bw-mint2"})
        self.relay_node.keygen()                                # it knows no one; the carrier is told the names
        self.relay_node.config["radio"] = {"gateway": False, "gateway_id": "!house", "peers": {lab.payer.nym: "!house"},
                                           "relay": {lab.hop.nym: lab.hop_url},
                                           "names": {lab.payer.nym: "bw-house", lab.hop.nym: "bw-forest"}}
        self.relay = bwradio.Carrier(self.relay_node, self.mesh.radio("!relay"))
        self.relay.last_beacon = self.t0 + 10 ** 9

    def test_a_window_settles_by_radio_while_the_internet_is_down(self):
        lab = self.lab
        out = self.run_hour(1_000, 1_000)
        acks = self.lines(lab.payer, "acks.jsonl")
        self.assertEqual(len(acks), 1)
        self.assertEqual(acks[0]["receipt"]["prev_sig"], out["receipt"]["sig"])
        self.assertEqual(acks[0]["from"], "bw-forest")         # the hop's own ack, signed by the hop
        s = self.settled()
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0]["payer_bytes"], s[0]["hop_bytes"], s[0]["status"]), (1_000, 1_000, "settled"))
        self.assertEqual(self.payer.pending, {})
        self.assertEqual([d[:2] for d in self.mesh.delivered], [("!house", "!relay"), ("!relay", "!house"), ("!house", "!relay")])
        self.assertEqual([d[2] for d in self.mesh.delivered], [wire.RECEIPT, wire.ACK, wire.DELIVERED])
        self.assertEqual(len(self.lines(lab.hop, "acks.jsonl")), 1)
        self.assertFalse(os.path.exists(self.relay_node.path("acks.jsonl")))   # the relay kept nothing
        self.assertEqual(os.listdir(self.relay_node.path("outbox")), [])
        self.assertEqual(len(os.listdir(lab.hop.path("outbox"))), 1)           # the hop's own copy waits for its own path to the Mint

    def test_the_hops_own_copy_reaches_the_mint_through_the_gateway(self):
        pass                                                    # the hop has no radio here: its copy waits (asserted above)

    def test_a_doubled_receipt_gets_the_same_ack_once(self):
        lab = self.lab
        self.run_hour(1_000, 1_000, double=lambda data, dest: data[1] == wire.RECEIPT)
        self.assertEqual(len(self.lines(lab.hop, "acks.jsonl")), 1)      # the hop's held_ack, asked twice through the relay
        self.assertEqual(len(self.lines(lab.payer, "acks.jsonl")), 1)
        self.assertEqual(len(self.settled()), 1)

    def test_the_hop_unreachable_from_the_relay_is_asked_again(self):
        lab = self.lab
        self.relay.relay[lab.hop.nym] = "http://127.0.0.1:1"   # the hop's own network is down too
        lab.hour(1_000, 1_000)
        now = lab.t
        self.payer.tick(now)
        self.payer.tick(now + bwradio.RADIO_AFTER_S)
        self.mesh.deliver(now=now + 3)
        self.assertEqual(self.mesh.queue, [])                   # nothing came back
        self.assertEqual(len(self.payer.pending), 1)
        self.payer.tick(now + bwradio.RADIO_AFTER_S + bwradio.ASK_RETRY_S)
        self.assertEqual([q[1][1] for q in self.mesh.queue], [wire.RECEIPT])   # asked again
        self.relay.relay[lab.hop.nym] = lab.hop_url             # the hop's network is back
        self.mesh.deliver(now=now + 8)
        self.mesh.deliver(now=now + 10)
        self.assertEqual(len(self.lines(lab.payer, "acks.jsonl")), 1)

    def test_a_lost_receipt_is_asked_again_within_the_clock_rule(self):
        pass                                                    # the payer's side, unchanged: Outage covers it

    def test_a_declined_receipt_stops_the_asking(self):
        pass                                                    # likewise

    def test_strangers_and_noise_are_dropped(self):
        pass

    def test_beacons_are_heard_and_written_by_the_gateway(self):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=1)
