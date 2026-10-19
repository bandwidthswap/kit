#!/usr/bin/env python3
"""bwradio: the kit's records over LoRa, a sidecar beside bwkit (radio/PLAN.md, Section 3).

    bwradio run       carry: watch the node's receipts and outbox, send what the IP path did not, answer what arrives
    bwradio status    what the radio has heard and what waits

It runs on a node with a T-Beam on USB (the `radio` object in /etc/bwkit/config.json, below), as the bwkit user,
from its own virtualenv with the Meshtastic Python API; bwkit.py itself stays one file with no dependencies. The
sidecar's contract with the kit is the node's directory (receipts.jsonl, acks.jsonl, the outbox and sent/), the
loopback HTTP the node serves, and the envelope format; it signs nothing.

By role (every rule is in the plan):
  payer      a receipt without an ack goes to the hop's radio RADIO_AFTER_S after it is signed, so the HTTP path gets
             the first try and the hop's clock rule is still met; the ack that comes back is checked and kept as if
             it had come over HTTP
  hop        a receipt arriving by radio is acknowledged through the node's own /ack (the ack the node already holds,
             if it was asked before) and the ack goes back by radio
  gateway    (the node that reaches the Mint over the LAN, bw-house) a record arriving by radio for the Mint is posted
             to the Mint and answered with a `delivered`
  everyone   records to the Mint still in the outbox after GRACE_S go to the gateway by radio and are moved to sent/
             on its `delivered`; a beacon every BEACON_S

config.json:  "radio": {"port": "/dev/bwradio", "gateway": false, "gateway_id": "!a1b2c3d4",
                        "peers": {"<NYM>": "!a1b2c3d4", ...}, "self_url": "http://127.0.0.1:8710"}
"""

import importlib.machinery
import importlib.util
import json
import os
import queue
import socket
import sys
import time
from urllib import error as urlerror

# wire.py: beside this file in the repo, in /usr/local/lib/bwradio where provision.sh radio installs it, or BWRADIO_LIB
for _d in (os.path.dirname(os.path.abspath(__file__)), "/usr/local/lib/bwradio", os.environ.get("BWRADIO_LIB")):
    if _d and _d not in sys.path:
        sys.path.insert(0, _d)
import wire  # noqa: E402


def load_bwkit():
    """bwkit as a module: importable beside this file in the repo, or the installed script (BWKIT_PATH)."""
    try:
        import bwkit as m
        return m
    except ImportError:
        pass
    path = os.environ.get("BWKIT_PATH", "/usr/local/bin/bwkit")
    spec = importlib.util.spec_from_loader("bwkit", importlib.machinery.SourceFileLoader("bwkit", path))
    m = importlib.util.module_from_spec(spec)
    sys.modules["bwkit"] = m
    spec.loader.exec_module(m)
    return m


bwkit = load_bwkit()
log = bwkit.log

VERSION = "0.1.0"
RADIO_AFTER_S = 2        # a payer's receipt goes to the hop by radio this long after it is signed, unless an ack is held
ASK_RETRY_S = 5          # and again this often while no ack and no decline has come back
ASK_GIVE_UP_S = 90       # until this long after t_last: beyond the widest clock tolerance a link would announce, the window is lost
GRACE_S = 60             # a record for the Mint still in the outbox after this goes by radio: the IP path had its chance
RETRY_S = 300            # and again this often until the gateway says delivered
BEACON_S = 600
SWEEP_S = 15             # how often the outbox is looked at
RECENT_LINES = 400       # how far back receipts.jsonl and acks.jsonl are read
REASONS = {"duplicate": 0, "replay": 1, "clock": 2, "unknown": 3, "patience": 4, "since": 5, "domain": 6, "profile": 7, "route": 8}


def tail(path, n=RECENT_LINES):
    """The last n JSON lines of a .jsonl file, oldest first; an empty list when there is none."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        lines = f.read().splitlines()[-n:]
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class Carrier:
    def __init__(self, node, radio, now=None):
        cfg = node.config.get("radio") or {}
        self.node, self.radio = node, radio
        self.gateway = bool(cfg.get("gateway"))
        self.gateway_id = cfg.get("gateway_id")
        self.radio_of = dict(cfg.get("peers") or {})           # NYM -> radio id
        self.self_url = (cfg.get("self_url") or "http://127.0.0.1:%d" % int(node.config.get("port") or bwkit.DEFAULT_PORT)).rstrip("/")
        self.mint_url = (node.config.get("mint") or "").rstrip("/") or None
        self.pending = {}                                       # (sig, dest) -> {"file": outbox name or None, "sent": t, "tries": n}
        self.declined = set()                                   # receipt sigs the hop declined: the window is lost, stop asking
        self.heard = {}                                         # name or radio id -> the last beacon
        self.first_seen = {}                                    # receipt sig -> when the payer's carrier first saw it
        self.last_beacon = None
        self.last_sweep = None
        self.sent_log = []                                      # what went on the air, for status and tests
        radio.on_receive(self.on_packet)

    # ---------------------------------------------------------------- helpers

    def peers(self):
        return self.node.peers()

    def dest_for(self, nym):
        return self.radio_of.get(nym)

    def send(self, dest, packet, key=None, file=None, now=None):
        now = now if now is not None else time.time()
        self.radio.send(dest, packet)
        self.sent_log.append((now, dest, packet[1], len(packet)))
        if key is not None:
            p = self.pending.get(key) or {"file": file, "sent": now, "tries": 0}
            p["sent"], p["tries"] = now, p["tries"] + 1
            self.pending[key] = p

    def held_acks(self):
        """prev_sig -> ack, from this node's acks.jsonl."""
        return {a["receipt"]["prev_sig"]: a for a in tail(self.node.path("acks.jsonl")) if a.get("kind") == "ack"}

    def recent_receipts(self):
        return [e for e in tail(self.node.path("receipts.jsonl")) if e.get("kind") == "receipt"]

    # ---------------------------------------------------------------- the payer: receipts awaiting the hop's ack

    def ask_hop(self, now):
        acks = self.held_acks()
        for env in self.recent_receipts():
            r, sig = env["receipt"], env["sig"]
            if sig in acks or sig in self.declined or r.get("payer") != self.node.nym:
                continue
            if now - r["t_last"] > ASK_GIVE_UP_S:
                continue
            first = self.first_seen.setdefault(sig, now)
            if now - first < RADIO_AFTER_S:
                continue
            dest = self.dest_for(r["hop"])
            if not dest:
                continue
            key = (sig, dest)
            p = self.pending.get(key)
            if p and now - p["sent"] < ASK_RETRY_S:
                continue
            try:
                packet = wire.encode(env)
            except wire.WireError as e:
                log("radio: cannot encode receipt %s: %s" % (sig[:12], e))
                self.declined.add(sig)
                continue
            self.send(dest, packet, key, now=now)
            log("radio: receipt %s for %d..%d to the hop %s%s" % (sig[:12], r["t_first"], r["t_last"], dest, " (again)" if p else ""))

    # ---------------------------------------------------------------- everyone: the outbox to the Mint

    def sweep(self, now):
        names = sorted(os.listdir(self.node.path("outbox")))
        if not names:
            return
        if self.gateway:
            self.node.flush_quietly()                           # the LAN to the Mint is this node's path
            return
        if not self.gateway_id:
            return
        for name in names:
            p = self.node.path("outbox", name)
            try:
                age = now - os.path.getmtime(p)
                with open(p) as f:
                    env = json.load(f)
            except (OSError, ValueError):
                continue
            if age < GRACE_S:
                continue
            key = (env["sig"], self.gateway_id)
            pend = self.pending.get(key)
            if pend and now - pend["sent"] < RETRY_S:
                continue
            try:
                packet = wire.encode(env)
            except wire.WireError as e:
                log("radio: cannot encode %s: %s" % (name, e))
                os.replace(p, self.node.path("sent", name + ".refused"))
                continue
            self.send(self.gateway_id, packet, key, file=name, now=now)
            log("radio: %s %s to the Mint's gateway%s" % (env["kind"], env["sig"][:12], " (again)" if pend else ""))

    def beacon(self, now):
        outbox = len(os.listdir(self.node.path("outbox")))
        last = max((e["receipt"]["t_last"] for e in self.recent_receipts()), default=0)
        serving = True
        try:
            packet = wire.beacon(self.node.nym, int(now), serving, outbox, last)
        except wire.WireError:
            return
        self.send(None, packet, now=now)                        # None: broadcast on the channel
        self.last_beacon = now

    def tick(self, now=None):
        now = now if now is not None else time.time()
        if self.node.role == "payer":
            self.ask_hop(now)
        if self.last_sweep is None or now - self.last_sweep >= SWEEP_S:
            self.last_sweep = now
            self.sweep(now)
        if self.last_beacon is None or now - self.last_beacon >= BEACON_S:
            self.beacon(now)

    # ---------------------------------------------------------------- what arrives

    def on_packet(self, data, from_id, now=None):
        now = now if now is not None else time.time()
        try:
            kind = wire.kind_of(data)
        except wire.WireError as e:
            log("radio: dropped %d bytes from %s: %s" % (len(data), from_id, e))
            return
        if kind == wire.DELIVERED:
            return self.on_delivered(data, from_id)
        if kind == wire.BEACON:
            return self.on_beacon(data, from_id, now)
        try:
            env = wire.decode(data, self.peers())
            bwkit.check_envelope(env)
        except (wire.WireError, ValueError) as e:
            log("radio: refused a %s from %s: %s" % ("receipt" if kind == wire.RECEIPT else "ack", from_id, e))
            return
        r = env["receipt"]
        if kind == wire.RECEIPT and self.node.role == "hop" and r["hop"] == self.node.nym:
            self.ack_by_radio(env, from_id, now)
        if kind == wire.ACK and self.node.role == "payer" and r["payer"] == self.node.nym:
            self.take_ack(env, from_id, now)
        if self.gateway:
            self.deliver_to_mint(env, from_id)

    def ack_by_radio(self, env, from_id, now):
        """The hop: acknowledge through the node's own /ack, or hand back the ack it already holds, and send it to the payer."""
        r, sig = env["receipt"], env["sig"]
        ack = self.held_acks().get(sig)
        if not ack:
            try:
                status, body = bwkit.http_post(self.self_url + "/ack", env)
            except (urlerror.URLError, socket.timeout, OSError) as e:
                log("radio: the node's own /ack did not answer: %s" % e)
                return
            if status != 200:
                word = (body or {}).get("error", "") if isinstance(body, dict) else ""
                reason = REASONS.get(word, 9)
                log("radio: declined the receipt %s from %s: %s %s" % (sig[:12], env.get("from"), status, body))
                self.send(from_id, wire.delivered(sig, wire.REFUSED, reason), now=now)
                return
            ack = body
        dest = self.dest_for(r["payer"]) or from_id
        try:
            packet = wire.encode(ack)
        except wire.WireError as e:
            log("radio: cannot encode the ack: %s" % e)
            return
        self.send(dest, packet, now=now)
        log("radio: ack %s for %s's receipt %s back to %s" % (ack["sig"][:12], env.get("from"), sig[:12], dest))

    def take_ack(self, env, from_id, now):
        """The payer: an ack that came by radio, kept as receipt_run keeps one that came over HTTP."""
        r, prev = env["receipt"], env["receipt"]["prev_sig"]
        if prev in self.held_acks():
            return                                              # already held (the same ack twice, or HTTP got there first)
        mine = {e["sig"]: e for e in self.recent_receipts()}
        ours = mine.get(prev)
        if not ours:
            log("radio: an ack from %s for a receipt %s this node did not sign; dropped" % (from_id, prev[:12]))
            return
        if r["hop"] != ours["receipt"]["hop"] or any(r[k] != ours["receipt"][k] for k in ("payer", "hop_index", "merkle_root", "t_first", "t_last")):
            log("radio: an ack from %s that does not match our receipt %s; dropped" % (from_id, prev[:12]))
            return
        self.node.append("acks.jsonl", env)
        self.node.outbox_put(env)
        self.pending.pop((prev, self.dest_for(r["hop"])), None)
        log("radio: ack from %s for %d..%d: %d bytes%s" % (env.get("from"), r["t_first"], r["t_last"], r["bytes"],
            "" if r["bytes"] == ours["receipt"]["bytes"] else " (differs from ours: %d)" % ours["receipt"]["bytes"]))
        if self.gateway:
            self.node.flush_quietly()

    def deliver_to_mint(self, env, from_id):
        """The gateway: a record for the Mint, posted over the LAN; the answer goes back as a delivered packet."""
        if not self.mint_url:
            return
        try:
            status, body = bwkit.http_post(self.mint_url + "/receipt", env)
        except (urlerror.URLError, socket.timeout, OSError) as e:
            log("radio: the Mint did not answer (%s); %s stays with its sender" % (e, env["sig"][:12]))
            return
        word = body.get("error", "") if isinstance(body, dict) else ""
        if 200 <= status < 300:
            st, reason = wire.STORED, 0
        elif status == 409 and word == "duplicate":
            st, reason = wire.DUPLICATE, 0
        else:
            st, reason = wire.REFUSED, REASONS.get(word, 9)
        self.send(from_id, wire.delivered(env["sig"], st, reason))
        log("radio: %s %s from %s to the Mint: %s" % (env["kind"], env["sig"][:12], from_id, ("stored", "duplicate", "refused %s %s" % (status, body))[st]))

    def on_delivered(self, data, from_id):
        try:
            d = wire.read_delivered(data)
        except wire.WireError:
            return
        matched = False
        for (sig, dest), p in list(self.pending.items()):
            if dest != from_id or wire.b64u_dec(sig)[:8] != d["sig_prefix"]:
                continue
            matched = True
            self.pending.pop((sig, dest))
            if p.get("file"):                                   # a record for the Mint: the gateway's answer
                self.settle_outbox(p["file"], d, from_id)
            elif d["status"] == wire.REFUSED:                   # the hop declined our receipt: the window is lost (spec, Section 6)
                self.declined.add(sig)
                log("radio: the hop declined receipt %s (reason %d); the window is lost" % (sig[:12], d["reason"]))
        if not matched and from_id == self.gateway_id and d["status"] in (wire.STORED, wire.DUPLICATE):
            # the gateway heard this record another way (the ack we sent the payer, say) and the Mint has it: our own copy need not fly
            for name in sorted(os.listdir(self.node.path("outbox"))):
                try:
                    with open(self.node.path("outbox", name)) as f:
                        env = json.load(f)
                    if wire.b64u_dec(env["sig"])[:8] == d["sig_prefix"]:
                        self.settle_outbox(name, d, from_id)
                except (OSError, ValueError, KeyError, wire.WireError):
                    continue

    def settle_outbox(self, name, d, from_id):
        """The gateway's word on an outbox record: stored or duplicate moves it to sent/, refused to sent/<name>.refused."""
        src = self.node.path("outbox", name)
        if not os.path.exists(src):
            return
        if d["status"] in (wire.STORED, wire.DUPLICATE):
            os.replace(src, self.node.path("sent", name))
            self.node.append("sent.jsonl", {"t": int(time.time()), "file": name, "radio": from_id, "status": ("stored", "duplicate")[d["status"]]})
        else:
            os.replace(src, self.node.path("sent", name + ".refused"))
            log("radio: the Mint refused %s (reason %d)" % (name, d["reason"]))

    def on_beacon(self, data, from_id, now):
        try:
            b = wire.read_beacon(data, self.peers())
        except wire.WireError:
            return
        b["radio"], b["heard_at"] = from_id, int(now)
        self.heard[b["name"] or from_id] = b
        try:
            self.node.write_json("radio.json", {"heard": self.heard, "written": int(now)})
        except OSError:
            pass


class MeshtasticRadio:
    """A T-Beam on its serial port through the Meshtastic Python API: data packets on the private port number, to a
    node id or broadcast. Not yet run against a board: radio/PLAN.md, step 6, is where it is."""
    PORTNUM = 256   # PRIVATE_APP

    def __init__(self, port):
        import meshtastic.serial_interface   # the venv at /opt/bwradio
        from pubsub import pub
        self.iface = meshtastic.serial_interface.SerialInterface(port)
        self.my_id = self.iface.getMyUser()["id"]
        self.q = queue.Queue()
        self.cb = None
        pub.subscribe(self._rx, "meshtastic.receive")

    def _rx(self, packet, interface):
        d = packet.get("decoded") or {}
        if d.get("portnum") not in ("PRIVATE_APP", self.PORTNUM):
            return
        self.q.put((bytes(d.get("payload") or b""), packet.get("fromId")))

    def on_receive(self, cb):
        self.cb = cb

    def send(self, dest, data):
        self.iface.sendData(data, destinationId=dest or "^all", portNum=self.PORTNUM, wantAck=dest is not None)

    def poll(self):
        while True:
            try:
                data, from_id = self.q.get_nowait()
            except queue.Empty:
                return
            if self.cb:
                self.cb(data, from_id)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"
    node = bwkit.Node.from_env()
    cfg = node.config.get("radio") or {}
    if cmd == "status":
        try:
            with open(node.path("radio.json")) as f:
                print(json.dumps(json.load(f), indent=1, sort_keys=True))
        except OSError:
            print("nothing heard yet")
        print("outbox: %d" % len(os.listdir(node.path("outbox"))))
        return 0
    if cmd != "run":
        print(__doc__)
        return 2
    port = cfg.get("port") or "/dev/bwradio"
    radio = MeshtasticRadio(port)
    carrier = Carrier(node, radio)
    log("bwradio %s: %s %s on %s as %s%s" % (VERSION, node.role, node.name, port, radio.my_id, ", the Mint's gateway" if carrier.gateway else ""))
    while True:
        radio.poll()
        carrier.tick()
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
