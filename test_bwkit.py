#!/usr/bin/env python3
"""Runs a payer, a hop and a Mint in one process with file-backed meters.

    python3 test_bwkit.py

Covers: signing and tamper detection, the agreeing window, the two kinds of
disagreement and the rule that settles them, the clock rule, unknown and
badly signed senders, the outbox when the Mint is down, offline resolution
with `settle`, the monthly invoice, the status report, the hourly commitment
(the Merkle root, the ledger entry, /commitments, the report, `prove` and
`verify-proof`), the rebuild of the ledger from the parties' copies, and
the 0.3.0 profile: link announcements, the Steward's fee, the margin from
the announcement, patience, `proto`, the report's proof, and the vectors.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bwkit  # noqa: E402


class Lab:
    """Three nodes in temp dirs. Meters are JSON files the test moves by hand."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="bwkit-test-")
        self.mint = bwkit.Node(os.path.join(self.root, "mint"),
                               {"role": "mint", "name": "bw-mint", "price_usd_per_gb": 0.10})
        self.mint.keygen()
        self.mint_srv = self.mint.serve("127.0.0.1", 0, block=False)
        self.mint_url = "http://127.0.0.1:%d" % self.mint_srv.server_address[1]
        self.hop_meter = os.path.join(self.root, "hop-meter.json")
        self.hop = bwkit.Node(os.path.join(self.root, "hop"),
                              {"role": "hop", "name": "bw-forest", "iface": "file:" + self.hop_meter, "mint": self.mint_url})
        self.hop.keygen()
        self.hop_srv = self.hop.serve("127.0.0.1", 0, block=False)
        self.hop_url = "http://127.0.0.1:%d" % self.hop_srv.server_address[1]
        self.payer_meter = os.path.join(self.root, "payer-meter.json")
        self.payer = bwkit.Node(os.path.join(self.root, "payer"),
                                {"role": "payer", "name": "bw-house", "iface": "file:" + self.payer_meter,
                                 "hop_name": "bw-forest", "hop_url": self.hop_url, "mint": self.mint_url})
        self.payer.keygen()
        # everyone knows everyone, as the install step does it
        for n in (self.mint, self.hop, self.payer):
            n.peer_add("bw-house", self.payer.nym)
            n.peer_add("bw-forest", self.hop.nym)
        self.meters(0, 0)
        self.t = 1_800_000_000  # a fixed clock, so windows are exact
        self.hop_now = self.t
        orig_ack = self.hop.ack
        self.hop.ack = lambda env, now=None: orig_ack(env, self.hop_now)   # the hop's clock is ours
        self.payer.sample(self.t)
        self.hop.sample(self.t)

    def meters(self, payer_bytes, hop_bytes):
        with open(self.payer_meter, "w") as f:
            json.dump({"rx": payer_bytes // 2, "tx": payer_bytes - payer_bytes // 2}, f)
        with open(self.hop_meter, "w") as f:
            json.dump({"rx": hop_bytes - hop_bytes // 2, "tx": hop_bytes // 2}, f)

    def hour(self, payer_total, hop_total, skew=0):
        """Move the meters to cumulative totals and run the payer's hourly job."""
        self.meters(payer_total, hop_total)
        self.t += 3600
        self._patch_hop_clock(self.t)
        return self.payer.receipt_run(self.t + skew)

    def step(self, seconds, payer_total, hop_total):
        """A window shorter than an hour, as the forced tests make them: several in one hour."""
        self.meters(payer_total, hop_total)
        self.t += seconds
        self._patch_hop_clock(self.t)
        return self.payer.receipt_run(self.t)

    def _patch_hop_clock(self, t):
        self.hop_now = t

    def close(self):
        for srv in (self.mint_srv, self.hop_srv):
            srv.shutdown()
            srv.server_close()
        shutil.rmtree(self.root)


class Signing(unittest.TestCase):
    def test_sign_verify_and_tamper(self):
        priv = bwkit.Ed25519PrivateKey.generate()
        nym = bwkit.nym_of_pub(priv.public_key())
        r = {"payer": nym, "hop": nym, "hop_index": 1, "merkle_root": "0" * 64,
             "bytes": 12345, "t_first": 1, "t_last": 3601, "prev_sig": ""}
        sig = bwkit.sign_receipt(priv, r)
        self.assertTrue(bwkit.verify_receipt(nym, r, sig))
        r2 = dict(r, bytes=12346)
        self.assertFalse(bwkit.verify_receipt(nym, r2, sig))
        bent = ("B" if sig[0] != "B" else "C") + sig[1:]      # one character changed, never the same bytes
        self.assertFalse(bwkit.verify_receipt(nym, r, bent))
        # the canonical form is the Figure 2 tuple in order, so field order in the dict is irrelevant
        shuffled = {k: r[k] for k in reversed(bwkit.FIELDS)}
        self.assertEqual(bwkit.canon(r), bwkit.canon(shuffled))
        self.assertTrue(bwkit.canon(r).startswith(bwkit.DOMAIN))

    def test_check_receipt_rejects_bad_shapes(self):
        priv = bwkit.Ed25519PrivateKey.generate()
        nym = bwkit.nym_of_pub(priv.public_key())
        good = {"payer": nym, "hop": nym, "hop_index": 1, "merkle_root": "a" * 64,
                "bytes": 0, "t_first": 5, "t_last": 5, "prev_sig": ""}
        bwkit.check_receipt(good)
        for bad in (dict(good, extra=1), dict(good, bytes=-1), dict(good, bytes=True), dict(good, t_first=6),
                    dict(good, merkle_root="zz"), dict(good, hop_index=0), dict(good, payer="rsa:abc")):
            with self.assertRaises(ValueError):
                bwkit.check_receipt(bad)


class Windows(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()

    def tearDown(self):
        self.lab.close()

    def test_agreeing_window_settles(self):
        out = self.lab.hour(1_000_000, 1_000_000)
        self.assertEqual(out["receipt"]["receipt"]["bytes"], 1_000_000)
        self.assertEqual(out["ack"]["receipt"]["bytes"], 1_000_000)
        self.assertEqual(out["ack"]["receipt"]["prev_sig"], out["receipt"]["sig"])
        self.assertEqual(out["left"], 0)
        s = [e for e in self.lab.mint.ledger_lines() if e["kind"] == "settlement"]
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["status"], "settled")
        self.assertEqual(s[0]["settled_bytes"], 1_000_000)
        self.assertAlmostEqual(s[0]["amount_usd"], 0.0001)
        kinds = [e["kind"] for e in self.lab.mint.ledger_lines()]
        self.assertEqual(kinds, ["receipt", "ack", "settlement", "commitment"])

    def test_small_skew_still_agrees(self):
        out = self.lab.hour(100_000_000, 100_000_000 - 500_000)   # half a megabyte of in-flight packets
        s = [e for e in self.lab.mint.ledger_lines() if e["kind"] == "settlement"][0]
        self.assertEqual(s["status"], "settled")
        self.assertEqual(s["settled_bytes"], 100_000_000 - 500_000)

    def test_hop_under_counts_is_disputed_and_settles_at_hop(self):
        out = self.lab.hour(50_000_000, 20_000_000)
        s = [e for e in self.lab.mint.ledger_lines() if e["kind"] == "settlement"][0]
        self.assertEqual(s["status"], "disputed")
        self.assertEqual(s["payer_bytes"], 50_000_000)
        self.assertEqual(s["hop_bytes"], 20_000_000)
        self.assertEqual(s["settled_bytes"], 20_000_000)

    def test_hop_over_counts_is_disputed_and_settles_at_payer(self):
        out = self.lab.hour(50_000_000, 80_000_000)
        s = [e for e in self.lab.mint.ledger_lines() if e["kind"] == "settlement"][0]
        self.assertEqual(s["status"], "disputed")
        self.assertEqual(s["settled_bytes"], 50_000_000)

    def test_counter_reset_on_hop(self):
        self.lab.hour(10_000_000, 10_000_000)
        # the hop's interface restarted mid-window: its counters start over
        self.lab.meters(30_000_000, 0)
        with open(self.lab.hop_meter, "w") as f:
            json.dump({"rx": 3_000_000, "tx": 3_000_000}, f)
        self.lab.t += 3600
        self.lab._patch_hop_clock(self.lab.t)
        out = self.lab.payer.receipt_run(self.lab.t)
        self.assertTrue(out["ack"]["meter"]["reset"])
        s = [e for e in self.lab.mint.ledger_lines() if e["kind"] == "settlement"][-1]
        self.assertEqual(s["payer_bytes"], 20_000_000)
        self.assertEqual(s["hop_bytes"], 6_000_000)
        self.assertEqual(s["status"], "disputed")
        self.assertEqual(s["settled_bytes"], 6_000_000)

    def test_clock_rule_hop_declines(self):
        out = self.lab.hour(1_000_000, 1_000_000, skew=12)
        self.assertIsNone(out["ack"])
        entries = self.lab.mint.ledger_lines()
        self.assertEqual([e["kind"] for e in entries], ["receipt"])
        no_ack, no_receipt = self.lab.mint.pending()
        self.assertEqual(len(no_ack), 1)
        # within tolerance it goes through
        out = self.lab.hour(2_000_000, 2_000_000, skew=4)
        self.assertIsNotNone(out["ack"])

    def test_unknown_and_badly_signed_senders(self):
        out = self.lab.hour(1_000, 1_000)
        env = out["receipt"]
        tampered = json.loads(json.dumps(env))
        tampered["receipt"]["bytes"] += 1
        status, body = bwkit.http_post(self.lab.mint_url + "/receipt", tampered)
        self.assertEqual(status, 400)
        self.assertIn("signature", body["error"])
        status, body = bwkit.http_post(self.lab.mint_url + "/receipt", env)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "duplicate")
        stranger = bwkit.Node(os.path.join(self.lab.root, "stranger"),
                              {"role": "payer", "name": "x", "iface": "file:" + self.lab.payer_meter,
                               "hop_name": "bw-forest", "hop_url": self.lab.hop_url, "mint": self.lab.mint_url})
        stranger.keygen()
        stranger.peer_add("bw-forest", self.lab.hop.nym)
        stranger.sample(self.lab.t)
        senv = stranger.make_receipt(self.lab.t + 3600)
        status, body = bwkit.http_post(self.lab.mint_url + "/receipt", senv)
        self.assertEqual(status, 403)
        status, body = bwkit.http_post(self.lab.hop_url + "/ack", senv)
        self.assertEqual(status, 403)

    def test_outbox_survives_mint_outage(self):
        self.lab.mint_srv.shutdown()
        self.lab.mint_srv.server_close()
        out = self.lab.hour(5_000_000, 5_000_000)
        self.assertEqual(out["left"], 2)   # receipt and ack wait on the payer
        self.assertEqual(len(os.listdir(self.lab.hop.path("outbox"))), 1)   # the hop's copy of its ack waits too
        self.lab.mint_srv = self.lab.mint.serve("127.0.0.1", self.lab.mint_srv.server_address[1], block=False)
        sent, left = self.lab.payer.flush()
        self.assertEqual((sent, left), (2, 0))
        sent, left = self.lab.hop.flush()
        self.assertEqual((sent, left), (1, 0))   # a duplicate for the Mint, moved to sent all the same
        s = [e for e in self.lab.mint.ledger_lines() if e["kind"] == "settlement"]
        self.assertEqual(len(s), 1)

    def test_ack_before_receipt_still_settles(self):
        # the hop's own copy reaches the Mint first; the Mint pairs them when the receipt arrives
        self.lab.hour(1_000, 1_000)
        self.lab.meters(3_000, 3_000)
        env = self.lab.payer.make_receipt(self.lab.t + 3600)
        self.lab._patch_hop_clock(self.lab.t + 3600)
        ack = self.lab.hop.ack(env)
        status, body = bwkit.http_post(self.lab.mint_url + "/receipt", ack)
        self.assertEqual(status, 200)
        self.assertEqual(body["settlements"], [])
        status, body = bwkit.http_post(self.lab.mint_url + "/receipt", env)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["settlements"]), 1)
        self.assertEqual(body["settlements"][0]["settled_bytes"], 2_000)

    def test_offline_resolution_matches_the_mint(self):
        out = self.lab.hour(50_000_000, 80_000_000)
        s_mint = [e for e in self.lab.mint.ledger_lines() if e["kind"] == "settlement"][0]
        s_here = bwkit.resolve(out["receipt"], out["ack"], 0.10, now=s_mint["t"])
        self.assertEqual(s_here, s_mint)
        # and the command line does the same from two files
        rp, ap = os.path.join(self.lab.root, "r.json"), os.path.join(self.lab.root, "a.json")
        with open(rp, "w") as f:
            json.dump(out["receipt"], f)
        with open(ap, "w") as f:
            json.dump(out["ack"], f)
        self.assertEqual(bwkit.main(["verify", rp, ap]), 0)
        self.assertEqual(bwkit.main(["settle", rp, ap]), 0)
        with open(ap) as f:
            forged = json.load(f)
        forged["receipt"]["bytes"] = 1
        with open(ap, "w") as f:
            json.dump(forged, f)
        self.assertEqual(bwkit.main(["verify", ap]), 1)

    def test_invoice_sums_a_month(self):
        self.lab.hour(1_000_000_000, 1_000_000_000)
        self.lab.hour(3_000_000_000, 2_000_000_000)
        import time
        month = time.strftime("%Y-%m", time.localtime(self.lab.t))
        inv = self.lab.invoice = self.lab.mint.invoice(month)
        o = inv[(self.lab.payer.nym, self.lab.hop.nym)]
        self.assertEqual(o["windows"], 2)
        self.assertEqual(o["disputed"], 1)
        self.assertEqual(o["settled_bytes"], 2_000_000_000)
        self.assertAlmostEqual(o["amount_usd"], 0.2)

    def test_mint_index_survives_restart(self):
        self.lab.hour(1_000, 1_000)
        fresh = bwkit.Node(self.lab.mint.dir, self.lab.mint.config)
        fresh.load_index()
        self.assertEqual(len(fresh.index["settled"]), 1)
        self.assertEqual(fresh.pending(), ([], []))


class StatusReport(unittest.TestCase):
    """The Mint's summary for the status page: names and roles, nothing about the network."""

    def setUp(self):
        self.lab = Lab()

    def tearDown(self):
        self.lab.close()

    def test_report_summarises_the_ledger_by_name(self):
        self.lab.hour(1_000_000, 1_000_000)                  # agreeing window
        self.lab.hour(3_000_000, 2_000_000)                  # the hop under-counts: disputed, settled at 1 MB
        rep = self.lab.mint.status_report(now=self.lab.t + 60, service=True)
        self.assertEqual(rep["generated_at"], self.lab.t + 60)
        self.assertEqual([n["name"] for n in rep["nodes"]], ["bw-house", "bw-forest", "bw-mint"])
        by = {n["name"]: n for n in rep["nodes"]}
        self.assertEqual(by["bw-house"]["role"], "payer")
        self.assertEqual(by["bw-forest"]["role"], "hop")
        self.assertEqual(by["bw-mint"]["role"], "mint")
        self.assertEqual(by["bw-house"]["receipts"], 2)
        self.assertEqual(by["bw-house"]["window_bytes"], 2_000_000)
        self.assertEqual((by["bw-house"]["window_t_first"], by["bw-house"]["window_t_last"]), (self.lab.t - 3600, self.lab.t))
        self.assertEqual(by["bw-forest"]["receipts"], 2)
        self.assertEqual(by["bw-forest"]["window_bytes"], 1_000_000)
        self.assertEqual(by["bw-mint"]["receipts"], 4)
        self.assertEqual(by["bw-mint"]["window_bytes"], 1_000_000)   # the settled count
        for name in ("bw-house", "bw-forest", "bw-mint"):
            self.assertEqual((by[name]["windows"], by[name]["disputed"]), (2, 1), name)
            self.assertIsNotNone(by[name]["last_receipt"])
        self.assertEqual(rep["ledger"], {"receipts": 2, "acks": 2, "settlements": 2, "disputed": 1,
                                         "receipts_without_ack": 0, "acks_without_receipt": 0})
        self.assertIs(rep["service"], True)
        self.assertIsNotNone(rep["since"])
        # the running totals: what each signed, counted or settled
        self.assertEqual((by["bw-house"]["total_bytes"], by["bw-forest"]["total_bytes"], by["bw-mint"]["total_bytes"]),
                         (3_000_000, 2_000_000, 2_000_000))
        # the history: both windows, oldest first, with the settlement on each
        h = by["bw-house"]["history"]
        self.assertEqual([(w["t_first"], w["t_last"], w["bytes"], w["settled_bytes"], w["disputed"]) for w in h],
                         [(self.lab.t - 7200, self.lab.t - 3600, 1_000_000, 1_000_000, False),
                          (self.lab.t - 3600, self.lab.t, 2_000_000, 1_000_000, True)])
        self.assertEqual([w["bytes"] for w in by["bw-forest"]["history"]], [1_000_000, 1_000_000])
        self.assertEqual([(w["bytes"], w["disputed"]) for w in by["bw-mint"]["history"]], [(1_000_000, False), (1_000_000, True)])
        text = json.dumps(rep)
        self.assertNotIn("127.0.0.1", text)                  # names, roles and signed records; never an address
        bwkit.assert_public(text)
        self.assertEqual(rep["commitment"]["mint"], self.lab.mint.nym)   # the Mint's NYM, so a browser can check the root

    def test_report_before_any_receipt_lists_the_peers(self):
        rep = self.lab.mint.status_report(now=self.lab.t)
        by = {n["name"]: n for n in rep["nodes"]}
        self.assertEqual(set(by), {"bw-house", "bw-forest", "bw-mint"})
        self.assertEqual(by["bw-house"]["role"], "peer")       # no receipt yet, so no role known
        self.assertIsNone(by["bw-house"]["last_receipt"])
        self.assertIsNone(by["bw-mint"]["window_bytes"])
        self.assertEqual((by["bw-mint"]["history"], by["bw-mint"]["total_bytes"]), ([], 0))
        self.assertEqual(rep["ledger"]["settlements"], 0)
        self.assertIsNone(rep["since"])

    def test_history_is_the_last_twelve_hours_and_an_unsettled_window_says_so(self):
        for i in range(14):
            self.lab.hour((i + 1) * 1000, (i + 1) * 1000)
        rep = self.lab.mint.status_report(now=self.lab.t + 5)
        by = {n["name"]: n for n in rep["nodes"]}
        h = by["bw-house"]["history"]
        self.assertEqual(len(h), bwkit.HISTORY_HOURS)
        cut = (self.lab.t // 3600 - (bwkit.HISTORY_HOURS - 1)) * 3600
        self.assertGreaterEqual(h[0]["t_last"], cut)
        self.assertLess(h[0]["t_last"] - 3600, cut)              # the one before it fell off
        self.assertEqual(h[-1]["t_last"], self.lab.t)
        self.assertEqual(by["bw-house"]["total_bytes"], 14 * 1000)
        self.assertEqual(len(by["bw-mint"]["history"]), bwkit.HISTORY_HOURS)
        # a receipt the hop never acked: on the ledger, in the history, unsettled
        self.lab.hop_srv.shutdown()
        self.lab.hop_srv.server_close()
        self.lab.hour(15 * 1000, 15 * 1000)
        rep = self.lab.mint.status_report(now=self.lab.t + 5)
        by = {n["name"]: n for n in rep["nodes"]}
        last = by["bw-house"]["history"][-1]
        self.assertEqual((last["bytes"], last["settled_bytes"], last["disputed"]), (1000, None, None))
        self.assertEqual(rep["ledger"]["receipts_without_ack"], 1)
        self.lab.hop_srv = self.lab.hop.serve("127.0.0.1", 0, block=False)   # so close() has something to shut down

    def test_report_skips_a_half_written_last_line(self):
        self.lab.hour(1_000, 1_000)
        with open(self.lab.mint.path("ledger.jsonl"), "a") as f:
            f.write('{"kind": "receipt", "from": "bw-ho')
        rep = self.lab.mint.status_report(now=self.lab.t)
        self.assertEqual(rep["ledger"]["settlements"], 1)
        with self.assertRaises(ValueError):
            bwkit.Node(self.lab.mint.dir, self.lab.mint.config).load_index()

    def test_report_posts_with_the_bearer_token(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                seen["auth"] = self.headers.get("Authorization")
                seen["agent"] = self.headers.get("User-Agent")
                seen["path"] = self.path
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                data = b'{"stored": true, "received_at": 1}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.lab.hour(1_000, 1_000)
            url = "http://127.0.0.1:%d/status" % srv.server_address[1]
            status, body = self.lab.mint.report(url, "tok-123", now=self.lab.t)
            self.assertEqual(status, 200)
            self.assertEqual(body, {"stored": True, "received_at": 1})
            self.assertEqual(seen["auth"], "Bearer tok-123")
            self.assertEqual(seen["agent"], "bwkit/" + bwkit.VERSION)   # Python's default is refused at the edge
            self.assertEqual(seen["path"], "/status")
            self.assertEqual(seen["body"]["generated_at"], self.lab.t)
            self.assertEqual(len(seen["body"]["nodes"]), 3)
            self.assertIn(seen["body"]["service"], (True, False))   # the test Mint serves on a port, so it was checked
        finally:
            srv.shutdown()
            srv.server_close()
        with self.assertRaises(SystemExit):
            self.lab.payer.report("http://127.0.0.1:1/status", "tok")   # only the Mint reports

    def test_assert_public_refuses_addresses(self):
        with self.assertRaises(ValueError):
            bwkit.assert_public('{"note": "bw-mint is at 192.168.0.61"}')
        with self.assertRaises(ValueError):
            bwkit.assert_public('{"note": "b8:27:eb:6b:1b:09"}')
        bwkit.assert_public('{"bwkit": "0.1.2", "amount_usd": 0.002195, "t": 1792186147, "name": "bw-house"}')


class Merkle(unittest.TestCase):
    """The hourly commitment's parts, with no node: leaves, roots, paths, signatures and proofs."""

    def settlement(self, i, **kw):
        s = {"kind": "settlement", "t": 1_800_000_000 + i, "payer": "ed25519:p", "hop": "ed25519:h", "hop_index": 1,
             "t_first": 1_800_000_000 + 60 * i, "t_last": 1_800_000_000 + 60 * i + 60,
             "payer_bytes": 1000 * i, "hop_bytes": 1000 * i, "settled_bytes": 1000 * i, "status": "settled",
             "rule": "r", "receipt_sig": "sig%d" % i, "ack_sig": "ack%d" % i, "price_usd_per_gb": 0.1, "amount_usd": 0.0}
        s.update(kw)
        return s

    def test_one_leaf_root_is_its_own_hash(self):
        s = self.settlement(1)
        leaf = bwkit.leaf_of(s)
        self.assertEqual(len(leaf), 32)
        self.assertEqual(bwkit.merkle_root([leaf]), leaf)
        self.assertEqual(bwkit.merkle_path([leaf], 0), [])
        self.assertTrue(bwkit.merkle_check(leaf, [], leaf.hex()))
        # the leaf is over the entry without t (the Mint's own clock), whatever the field order
        self.assertEqual(bwkit.leaf_of(dict(s, t=5)), leaf)
        self.assertEqual(bwkit.leaf_of({k: s[k] for k in reversed(list(s))}), leaf)
        self.assertNotEqual(bwkit.leaf_of(dict(s, settled_bytes=1001)), leaf)
        with self.assertRaises(ValueError):
            bwkit.merkle_root([])

    def test_several_leaves_and_their_paths(self):
        for n in (2, 3, 4, 5, 7, 8):
            ss, leaves = bwkit.hour_leaves([self.settlement(i) for i in reversed(range(n))])
            self.assertEqual([s["t_first"] for s in ss], sorted(s["t_first"] for s in ss))   # leaves go by window start
            root = bwkit.merkle_root(leaves).hex()
            for i, leaf in enumerate(leaves):
                path = bwkit.merkle_path(leaves, i)
                self.assertTrue(path)
                self.assertTrue(bwkit.merkle_check(leaf, path, root), (n, i))
                # a byte changed in the settlement, a byte changed in the path, the sides swapped
                changed = bwkit.leaf_of(dict(ss[i], settled_bytes=ss[i]["settled_bytes"] + 1))
                self.assertFalse(bwkit.merkle_check(changed, path, root), (n, i))
                bent = [[side, ("1" if h[0] == "0" else "0") + h[1:]] for side, h in path]
                self.assertFalse(bwkit.merkle_check(leaf, bent, root), (n, i))
                swapped = [["L" if side == "R" else "R", h] for side, h in path]
                self.assertFalse(bwkit.merkle_check(leaf, swapped, root), (n, i))
            # the root depends on the set and the order
            self.assertNotEqual(bwkit.merkle_root(leaves[:-1] + [bwkit.leaf_of(self.settlement(99))]).hex(), root)
            self.assertNotEqual(bwkit.merkle_root(list(reversed(leaves))).hex(), root)
            self.assertNotEqual(bwkit.merkle_root(leaves[:-1]).hex() if n > 2 else None, root)

    def test_commitment_signature(self):
        priv = bwkit.Ed25519PrivateKey.generate()
        ss = [self.settlement(1), self.settlement(2)]
        c = bwkit.make_commitment(priv, ss, 1_800_000_000, now=7)
        self.assertEqual((c["kind"], c["window"], c["count"], c["t"]), ("commitment", 1_800_000_000, 2, 7))
        self.assertEqual(c["mint"], bwkit.nym_of_pub(priv.public_key()))
        self.assertEqual(c["root"], bwkit.merkle_root([bwkit.leaf_of(s) for s in ss]).hex())
        self.assertTrue(bwkit.verify_commitment(c))
        other = bwkit.nym_of_pub(bwkit.Ed25519PrivateKey.generate().public_key())
        for bad in (dict(c, count=1), dict(c, window=c["window"] + 3600), dict(c, root="0" * 64),
                    dict(c, signature=c["signature"][:-2] + "AA"), dict(c, mint=other), dict(c, signature=None)):
            self.assertFalse(bwkit.verify_commitment(bad))
        unsigned = bwkit.make_commitment(None, ss, 1_800_000_000, now=7)    # what a rebuild makes
        self.assertEqual(unsigned["root"], c["root"])
        self.assertEqual((unsigned["mint"], unsigned["signature"]), (None, None))
        self.assertFalse(bwkit.verify_commitment(unsigned))

    def test_proof_verifies_and_fails_after_a_changed_byte(self):
        priv = bwkit.Ed25519PrivateKey.generate()
        ss = [self.settlement(i) for i in range(3)]
        c = bwkit.make_commitment(priv, ss, 1_800_000_000, now=7)
        doc = bwkit.make_proof(c, ss)
        self.assertEqual((doc["kind"], doc["window"], doc["count"], len(doc["settlements"])), ("proof", 1_800_000_000, 3, 3))
        ok, lines = bwkit.check_proof(doc)
        self.assertTrue(ok, lines)
        self.assertEqual(len(lines), 4)
        self.assertIn("signature ok", lines[0])
        self.assertTrue(all("included" in line for line in lines[1:]), lines)
        self.assertTrue(bwkit.check_proof(doc, mint=c["mint"])[0])
        doc2 = json.loads(json.dumps(doc))                   # through a file and back
        self.assertTrue(bwkit.check_proof(doc2)[0])
        doc2["settlements"][1]["settlement"]["settled_bytes"] += 1
        ok, lines = bwkit.check_proof(doc2)
        self.assertFalse(ok)
        self.assertIn("included", lines[1])
        self.assertIn("NOT in the commitment", lines[2])
        doc3 = json.loads(json.dumps(doc))
        doc3["root"] = "f" * 64                               # a root the Mint never signed
        ok, lines = bwkit.check_proof(doc3)
        self.assertFalse(ok)
        self.assertIn("signature BAD", lines[0])
        ok, lines = bwkit.check_proof(doc, mint="ed25519:" + "A" * 43)
        self.assertFalse(ok)
        self.assertIn("not ed25519:AAAA", lines[0])
        self.assertFalse(bwkit.check_proof({"kind": "receipt"})[0])
        with self.assertRaises(ValueError):
            bwkit.make_proof(c, ss[:2])                       # not the settlements the commitment is over


class Commitments(unittest.TestCase):
    """The Mint's hourly commitment: on the ledger, at /commitments, in the report, proved and verified."""

    def setUp(self):
        self.lab = Lab()

    def tearDown(self):
        self.lab.close()

    def test_each_settlement_appends_a_signed_commitment(self):
        self.lab.hour(1_000_000, 1_000_000)
        self.lab.step(600, 3_000_000, 2_000_000)
        entries = self.lab.mint.ledger_lines()
        self.assertEqual([e["kind"] for e in entries], ["receipt", "ack", "settlement", "commitment"] * 2)
        s1, s2 = [e for e in entries if e["kind"] == "settlement"]
        c1, c2 = [e for e in entries if e["kind"] == "commitment"]
        self.assertEqual((c1["window"], c2["window"]), (s1["t_first"], s2["t_first"]))   # the hour the window started in
        self.assertEqual((c1["window"] % 3600, c2["window"] - c1["window"]), (0, 3600))
        self.assertEqual((c1["count"], c2["count"]), (1, 1))
        self.assertEqual((c1["mint"], c1["t"]), (self.lab.mint.nym, s1["t"]))
        self.assertTrue(bwkit.verify_commitment(c1) and bwkit.verify_commitment(c2))
        self.assertEqual(c1["root"], bwkit.leaf_of(s1).hex())          # one leaf: its own hash
        # the endpoint: the current commitment of every hour, oldest first
        status, body = bwkit.http_get(self.lab.mint_url + "/commitments")
        self.assertEqual(status, 200)
        self.assertEqual([(c["window"], c["root"], c["signature"], c["count"]) for c in json.loads(body)],
                         [(c["window"], c["root"], c["signature"], c["count"]) for c in (c1, c2)])
        # a second window in the same hour, as the forced tests make them: a new commitment over both
        self.lab.step(600, 3_500_000, 2_500_000)
        entries = self.lab.mint.ledger_lines()
        s3, c3 = entries[-2], entries[-1]
        self.assertEqual((s3["kind"], c3["kind"], c3["window"], c3["count"]), ("settlement", "commitment", c2["window"], 2))
        self.assertEqual(c3["root"], bwkit.merkle_root([bwkit.leaf_of(s2), bwkit.leaf_of(s3)]).hex())
        self.assertTrue(bwkit.verify_commitment(c3))
        got = json.loads(bwkit.http_get(self.lab.mint_url + "/commitments")[1])
        self.assertEqual([(c["window"], c["count"], c["signature"]) for c in got],
                         [(c1["window"], 1, c1["signature"]), (c2["window"], 2, c3["signature"])])
        st = json.loads(bwkit.http_get(self.lab.mint_url + "/status")[1])
        self.assertEqual((st["commitments"], st["settlements"], st["ledger_lines"]), (2, 3, 12))
        # the payer's own record of the Mint's answers carries the Mint's signed commitment
        with open(self.lab.payer.path("sent.jsonl")) as f:
            sent = [json.loads(line) for line in f]
        self.assertEqual(sent[-1]["mint"]["commitments"], [c3])
        # the ledger listing names it
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, self.mint_env()):
            self.assertEqual(bwkit.main(["ledger"]), 0)
        self.assertEqual(sum(1 for line in out.getvalue().splitlines() if "COMMIT" in line), 3)

    def test_report_carries_the_latest_commitment_with_its_proof(self):
        rep = self.lab.mint.status_report(now=self.lab.t)
        self.assertEqual((rep["commitment"], rep["proof"]), (None, None))
        self.lab.hour(1_000_000, 1_000_000)
        self.lab.hour(2_000_000, 2_000_000)
        self.lab.step(600, 2_500_000, 2_500_000)             # a second window in the latest hour
        rep = self.lab.mint.status_report(now=self.lab.t + 60, service=True)
        self.assertEqual(set(rep), {"v", "generated_at", "bwkit", "proto", "phase", "window_s", "since", "service", "ledger", "nodes",
                                    "commitment", "proof"})
        c = self.lab.mint.ledger_lines()[-1]
        self.assertEqual(rep["commitment"], {"t": c["t"], "window": self.lab.t - 600, "root": c["root"], "count": 1,
                                             "signature": c["signature"], "mint": self.lab.mint.nym})
        self.assertTrue(bwkit.verify_commitment(rep["commitment"]))
        # the proof: the same document `bwkit prove` prints for that hour, verifiable with nothing else
        self.assertEqual(rep["proof"], self.lab.mint.proof(rep["commitment"]["window"]))
        ok, lines = bwkit.check_proof(rep["proof"], mint=self.lab.mint.nym)
        self.assertTrue(ok, lines)
        self.assertEqual(len(rep["proof"]["settlements"]), 1)
        for n in rep["nodes"]:
            self.assertEqual(set(n), {"name", "role", "last_receipt", "window_bytes", "window_t_first", "window_t_last",
                                      "receipts", "windows", "disputed", "total_bytes", "history"})
        text = json.dumps(rep)
        bwkit.assert_public(text)                             # NYMs and signatures may appear; addresses may not
        self.assertLess(len(text), 64 * 1024)                 # the Worker's limit
        # GET /proof gives the same document for any moment in the hour; an hour with nothing is 404
        status, body = bwkit.http_get(self.lab.mint_url + "/proof?window=%d" % (rep["commitment"]["window"] + 1234))
        self.assertEqual((status, json.loads(body)), (200, rep["proof"]))
        with self.assertRaises(bwkit.urlerror.HTTPError) as cm:
            bwkit.http_get(self.lab.mint_url + "/proof?window=%d" % (self.lab.t + 7200))
        self.assertEqual(cm.exception.code, 404)

    def test_a_ledger_from_before_commitments_starts_with_the_next_window(self):
        self.lab.hour(1_000_000, 1_000_000)
        self.lab.step(600, 2_000_000, 2_000_000)
        hour_a, hour_b = self.lab.t - 3600 - 600, self.lab.t - 600
        with open(self.lab.mint.path("ledger.jsonl")) as f:
            lines = [line for line in f if '"kind": "commitment"' not in line]
        with open(self.lab.mint.path("ledger.jsonl"), "w") as f:
            f.writelines(lines)
        self.lab.mint.load_index()                           # a Mint upgraded with two settled windows and no commitment
        self.assertEqual(json.loads(bwkit.http_get(self.lab.mint_url + "/commitments")[1]), [])
        self.assertIsNone(self.lab.mint.status_report(now=self.lab.t)["commitment"])
        with self.assertRaises(SystemExit):
            self.lab.mint.proof(hour_a)
        self.lab.step(600, 3_000_000, 3_000_000)             # the next settled window, in hour B
        got = json.loads(bwkit.http_get(self.lab.mint_url + "/commitments")[1])
        self.assertEqual([(c["window"], c["count"]) for c in got], [(hour_b, 2)])   # over both of hour B's, none for hour A
        with self.assertRaises(SystemExit):
            self.lab.mint.proof(hour_a)
        doc = self.lab.mint.proof(hour_b)
        self.assertEqual(doc["count"], 2)
        self.assertTrue(bwkit.check_proof(doc, mint=self.lab.mint.nym)[0])
        self.assertEqual(self.lab.mint.status_report(now=self.lab.t)["commitment"]["window"], hour_b)

    def mint_env(self):
        cfg = os.path.join(self.lab.root, "mint-config.json")
        with open(cfg, "w") as f:
            json.dump(self.lab.mint.config, f)
        return {"BWKIT_DIR": self.lab.mint.dir, "BWKIT_CONFIG": cfg}

    def test_prove_and_verify_proof_from_the_command_line(self):
        import time
        self.lab.hour(1_000_000, 1_000_000)
        self.lab.step(600, 3_000_000, 2_000_000)
        self.lab.step(600, 4_000_000, 4_000_000)
        hour = self.lab.t - 1200
        doc = self.lab.mint.proof(hour)
        self.assertEqual((doc["window"], doc["count"], doc["mint"]), (hour, 2, self.lab.mint.nym))
        self.assertTrue(bwkit.check_proof(doc, mint=self.lab.mint.nym)[0])
        self.assertEqual(bwkit.parse_hour(str(hour + 1799)), hour)
        self.assertEqual(bwkit.parse_hour(time.strftime("%Y-%m-%dT%H", time.localtime(hour))), hour)
        self.assertEqual(bwkit.parse_hour(time.strftime("%Y-%m-%dT%H:%M", time.localtime(hour + 60))), hour)
        with self.assertRaises(SystemExit):
            bwkit.parse_hour("tomorrow")
        # the command line, with the Mint's directory and config in the environment as the service has them
        with mock.patch.dict(os.environ, self.mint_env()):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(bwkit.main(["prove", str(hour + 1800)]), 0)    # any moment in the hour
            printed = json.loads(out.getvalue())
            self.assertEqual(printed, doc)
            with self.assertRaises(SystemExit):
                bwkit.main(["prove", str(hour + 7200)])                          # nothing settled in that hour
        path = os.path.join(self.lab.root, "proof.json")
        with open(path, "w") as f:
            json.dump(printed, f)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(bwkit.main(["verify-proof", path]), 0)
            self.assertEqual(bwkit.main(["verify-proof", path, "--mint", self.lab.mint.nym]), 0)
            self.assertEqual(bwkit.main(["verify-proof", path, "--mint", self.lab.payer.nym]), 1)
            printed["settlements"][0]["settlement"]["hop_bytes"] += 1
            with open(path, "w") as f:
                json.dump(printed, f)
            self.assertEqual(bwkit.main(["verify-proof", path]), 1)
        self.assertIn("NOT in the commitment", out.getvalue())


class Rebuild(unittest.TestCase):
    """The ledger from the parties' signed copies alone, compared with the Mint's."""

    def setUp(self):
        self.lab = Lab()
        self.lab.hour(1_000_000, 1_000_000)                  # agreeing
        self.lab.hour(3_000_000, 2_000_000)                  # the hop under-counts: disputed, settled at 1 MB
        self.lab.hour(4_000_000, 3_000_000, skew=12)         # the hop declines: a receipt without ack
        self.lab.step(600, 5_000_000, 4_000_000)             # the hop's count covers both windows: disputed
        self.lab.step(600, 5_500_000, 4_500_000)             # a second window in that hour, agreeing
        self.receipts = os.path.join(self.lab.root, "receipts.jsonl")
        self.acks = os.path.join(self.lab.root, "acks.jsonl")
        shutil.copy(self.lab.payer.path("receipts.jsonl"), self.receipts)   # bw-house's copy
        shutil.copy(self.lab.hop.path("acks.jsonl"), self.acks)             # bw-forest's copy
        self.ledger = self.lab.mint.path("ledger.jsonl")

    def tearDown(self):
        self.lab.close()

    def rebuild(self):
        return bwkit.rebuild(bwkit.read_jsonl(self.receipts)[0], bwkit.read_jsonl(self.acks)[0], now=self.lab.t + 9)

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = bwkit.main(["rebuild", self.receipts, self.acks] + list(args))
        return rc, out.getvalue().splitlines()

    def test_rebuild_agrees_with_the_mint(self):
        entries, rep = self.rebuild()
        self.assertEqual(rep, {"receipts": 5, "acks": 4, "links": 0, "settlements": 4, "disputed": 2, "hours": 3,
                               "receipts_without_ack": 1, "acks_without_receipt": 0, "bad": []})
        self.assertEqual([e["kind"] for e in entries],
                         ["receipt", "ack", "settlement", "commitment"] * 2 + ["receipt"] + ["receipt", "ack", "settlement"] * 2 + ["commitment"])
        mint = self.lab.mint.ledger_lines()
        mine = {e["receipt_sig"]: e for e in entries if e["kind"] == "settlement"}
        theirs = {e["receipt_sig"]: e for e in mint if e["kind"] == "settlement"}
        self.assertEqual(set(mine), set(theirs))
        for sig in mine:                                      # the same settlement but for t, the Mint's clock
            self.assertEqual({k: v for k, v in mine[sig].items() if k != "t"}, {k: v for k, v in theirs[sig].items() if k != "t"})
        my_c = {e["window"]: e for e in entries if e["kind"] == "commitment"}
        their_c = {}
        for e in mint:
            if e["kind"] == "commitment":
                their_c[e["window"]] = e
        self.assertEqual({w: (c["root"], c["count"]) for w, c in my_c.items()}, {w: (c["root"], c["count"]) for w, c in their_c.items()})
        self.assertEqual({c["signature"] for c in my_c.values()}, {None})   # only the Mint can sign
        agree, verdict, lines = bwkit.compare(entries, mint)
        self.assertTrue(agree, (verdict, lines))
        self.assertEqual(verdict, "AGREE: 4 settlements in 3 hours rebuilt from the parties' copies match the Mint's ledger and its 3 commitments")
        self.assertEqual(lines, [])
        # the command line: the comparison, and a ledger a fresh Mint can load
        rc, out = self.run_cli("--compare", self.ledger)
        self.assertEqual(rc, 0)
        self.assertTrue(out[0].startswith("rebuilt 5 receipts and 4 acks under 0 announcements into 4 settlements (2 disputed) in 3 hours"), out)
        self.assertTrue(out[1].startswith("AGREE"), out)
        path = os.path.join(self.lab.root, "rebuilt.jsonl")
        rc, out = self.run_cli("--ledger", path)
        self.assertEqual(rc, 0)
        fresh_dir = os.path.join(self.lab.root, "mint2")
        os.makedirs(fresh_dir)
        shutil.copy(path, os.path.join(fresh_dir, "ledger.jsonl"))
        shutil.copy(self.lab.mint.path("peers.json"), os.path.join(fresh_dir, "peers.json"))
        fresh = bwkit.Node(fresh_dir, self.lab.mint.config)
        fresh.load_index()
        self.assertEqual((len(fresh.index["settled"]), len(fresh.index["commitments"])), (4, 3))
        self.assertIsNone(fresh.index["last_commitment"]["signature"])
        no_ack, no_receipt = fresh.pending()
        self.assertEqual((len(no_ack), len(no_receipt)), (1, 0))
        rep2 = fresh.status_report(now=self.lab.t)
        self.assertEqual((rep2["ledger"]["settlements"], rep2["ledger"]["receipts_without_ack"]), (4, 1))
        self.assertEqual(bwkit.compare(fresh.ledger_lines(), mint)[0], True)

    def test_rebuild_disagrees_when_the_mint_alters_a_settlement(self):
        mint = self.lab.mint.ledger_lines()
        s2 = [e for e in mint if e["kind"] == "settlement"][1]
        altered = [dict(e, settled_bytes=2_000_000, amount_usd=0.0002) if e is s2 else e for e in mint]
        with open(self.ledger, "w") as f:
            for e in altered:
                f.write(json.dumps(e, sort_keys=True) + "\n")
        entries, rep = self.rebuild()
        agree, verdict, lines = bwkit.compare(entries, altered)
        self.assertFalse(agree)
        self.assertEqual(verdict, "DIFFER in window %d..%d and hour %d" % (s2["t_first"], s2["t_last"], s2["t_first"]))
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("window %d..%d: amount_usd 0.0001 here, 0.0002 on the Mint; settled_bytes 1000000 here, 2000000 on the Mint" % (
            s2["t_first"], s2["t_last"]), lines[0])
        self.assertEqual(lines[1], "hour %d: the Mint's own ledger no longer matches the commitment it signed" % s2["t_first"])
        rc, out = self.run_cli("--compare", self.ledger)
        self.assertEqual(rc, 1)
        self.assertTrue(out[1].startswith("DIFFER in window"), out)
        self.assertEqual(len(out), 4)

    def test_rebuild_sees_an_omitted_window_and_a_forged_copy(self):
        mint = self.lab.mint.ledger_lines()
        s1, s2 = [e for e in mint if e["kind"] == "settlement"][:2]
        # the Mint drops a settlement and its commitment, as if the window had never settled
        without = [e for e in mint if not (e["kind"] == "settlement" and e["receipt_sig"] == s2["receipt_sig"])
                   and not (e["kind"] == "commitment" and e["window"] == s2["t_first"])]
        entries, rep = self.rebuild()
        agree, verdict, lines = bwkit.compare(entries, without)
        self.assertFalse(agree)
        self.assertEqual(verdict, "DIFFER in window %d..%d" % (s2["t_first"], s2["t_last"]))
        self.assertEqual(lines, ["window %d..%d: settled from the parties' copies, not on the Mint's ledger" % (s2["t_first"], s2["t_last"]),
                                 "hour %d: no commitment on the Mint's ledger (1 settlement rebuilt)" % s2["t_first"]])
        # the hop's copy carries an ack that was changed after signing: left out, so that window is the Mint's word alone
        with open(self.acks) as f:
            acks = [json.loads(line) for line in f]
        acks[0]["receipt"]["bytes"] += 1
        with open(self.acks, "w") as f:
            for a in acks:
                f.write(json.dumps(a) + "\n")
        entries, rep = self.rebuild()
        self.assertEqual(rep["bad"], ["acks.jsonl line 1: bad signature"])
        self.assertEqual((rep["settlements"], rep["receipts_without_ack"]), (3, 2))
        agree, verdict, lines = bwkit.compare(entries, mint)
        self.assertFalse(agree)
        self.assertEqual(verdict, "DIFFER in window %d..%d and hour %d" % (s1["t_first"], s1["t_last"], s1["t_first"]))
        self.assertEqual(lines, ["window %d..%d: on the Mint's ledger, not in the parties' copies" % (s1["t_first"], s1["t_last"]),
                                 "hour %d: the Mint committed to 1 settlement; the parties' copies have none in that hour" % s1["t_first"]])
        rc, out = self.run_cli("--compare", self.ledger)
        self.assertEqual(rc, 1)
        self.assertIn("1 lines left out", out[0])
        # a ledger the Mint never had: the rebuild alone, printed
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)                               # the forged line
        self.assertEqual(sum(1 for line in out if '"kind": "settlement"' in line), 3)



def mint_env(lab):
    """The Mint's directory and config in the environment, as the service has them, for the command line."""
    cfg = os.path.join(lab.root, "mint-config.json")
    with open(cfg, "w") as f:
        json.dump(lab.mint.config, f)
    return {"BWKIT_DIR": lab.mint.dir, "BWKIT_CONFIG": cfg}


def hop_env(lab):
    cfg = os.path.join(lab.root, "hop-config.json")
    with open(cfg, "w") as f:
        json.dump(lab.hop.config, f)
    return {"BWKIT_DIR": lab.hop.dir, "BWKIT_CONFIG": cfg}


def cli(args, env):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, env):
        rc = bwkit.main(args)
    return rc, out.getvalue()


class Links(unittest.TestCase):
    """Link announcements (spec, Section 7): signed by the hop, registered with the Mint, in force from `since`."""

    def setUp(self):
        self.lab = Lab()
        self.lab.hop.config["link"] = {"price": 0.10, "margin": 0.10, "min_bytes": 65536, "patience_windows": 0,
                                       "medium": "wifi", "range_class": "building"}

    def tearDown(self):
        self.lab.close()

    def settlements(self):
        return [e for e in self.lab.mint.ledger_lines() if e["kind"] == "settlement"]

    def resign(self, priv, rec, **changes):
        body = {k: v for k, v in rec.items() if k != "sig"}
        body.update(changes)
        return dict(body, sig=bwkit.sign_link(priv, body))

    def test_announcement_governs_the_windows_that_start_after_it(self):
        self.lab.hour(100_000_000, 105_000_000)               # a 5% gap under the pilot's 1%: disputed, no announcement
        s1 = self.settlements()[-1]
        self.assertEqual((s1["proto"], s1["status"], s1["link"], s1["margin"], s1["min_bytes"], s1["price_usd_per_gb"]),
                         (1, "disputed", None, 0.01, 65536, 0.1))
        rec, h = self.lab.hop.announce(now=self.lab.t + 1)    # since = now; the window running now started before it
        self.assertEqual((rec["kind"], rec["proto"], rec["relay"], rec["payer"], rec["domain"], rec["since"]),
                         ("link", 1, self.lab.hop.nym, "*", self.lab.mint.nym, self.lab.t + 1))
        self.assertEqual((rec["price"], rec["margin"], rec["min_bytes"], rec["medium"], rec["range_class"], rec["unit"], rec["carry"]),
                         (0.1, 0.1, 65536, "wifi", "building", "USD/GB", 0))
        self.assertTrue(bwkit.verify_link(rec))
        bwkit.check_link(rec)
        self.assertEqual(bwkit.link_hash(rec), h)
        self.assertEqual(len(h), 64)
        self.assertEqual(self.lab.mint.load_links(), [rec])                   # the Mint's registry
        self.assertEqual(self.lab.hop.load_links(), [rec])                    # the hop's own copy
        self.assertEqual(json.loads(bwkit.http_get(self.lab.mint_url + "/links")[1]), [rec])
        st = json.loads(bwkit.http_get(self.lab.mint_url + "/status")[1])
        self.assertEqual((st["links"], st["proto"], st["impl"]), ([rec], [1], "bwkit/" + bwkit.VERSION))
        hs = json.loads(bwkit.http_get(self.lab.hop_url + "/status")[1])
        self.assertEqual((hs["link"]["margin"], hs["link"]["medium"], hs["patience"][self.lab.payer.nym]["short"], hs["proto"]), (0.1, "wifi", 0, [1]))
        self.lab.hour(200_000_000, 210_000_000)               # started at self.lab.t, before since: still the defaults (5% gap)
        s2 = self.settlements()[-1]
        self.assertEqual((s2["status"], s2["link"], s2["margin"]), ("disputed", None, 0.01))
        out = self.lab.hour(300_000_000, 320_000_000)         # starts after since: a 10% gap is within the 10% margin
        s3 = self.settlements()[-1]
        self.assertEqual((s3["status"], s3["link"], s3["margin"], s3["min_bytes"], s3["price_usd_per_gb"], s3["amount_usd"], s3["fee_usd"]),
                         ("settled", h, 0.1, 65536, 0.1, 0.01, 0.0005))
        # offline, with the announcement, the same record; without it, the defaults
        self.assertEqual(bwkit.resolve(out["receipt"], out["ack"], 0.1, now=s3["t"], link=rec), s3)
        self.assertEqual(bwkit.resolve(out["receipt"], out["ack"], 0.1, now=s3["t"])["status"], "disputed")
        rp, ap, lp = [os.path.join(self.lab.root, n) for n in ("r.json", "a.json", "l.json")]
        for path, obj in ((rp, out["receipt"]), (ap, out["ack"]), (lp, rec)):
            with open(path, "w") as f:
                json.dump(obj, f)
        rc, text = cli(["settle", rp, ap, "--link", lp], {})
        self.assertEqual(rc, 0)
        self.assertEqual({k: v for k, v in json.loads(text).items() if k != "t"}, {k: v for k, v in s3.items() if k != "t"})
        rc, text = cli(["verify", lp], {})
        self.assertEqual((rc, "link by %s" % self.lab.hop.nym in text), (0, True))
        # a later announcement supersedes for the windows that start from its since; the earlier keeps its own
        self.lab.hop.config["link"]["price"] = 0.2
        rec2, h2 = self.lab.hop.announce(now=self.lab.t, since=self.lab.t)
        self.assertNotEqual(h2, h)
        self.lab.hour(400_000_000, 420_000_000)
        s4 = self.settlements()[-1]
        self.assertEqual((s4["link"], s4["price_usd_per_gb"], s4["amount_usd"], s4["fee_usd"]), (h2, 0.2, 0.02, 0.001))
        links = self.lab.mint.load_links()
        self.assertEqual(bwkit.link_in_force(links, self.lab.payer.nym, self.lab.hop.nym, s3["t_first"]), rec)
        self.assertEqual(bwkit.link_in_force(links, self.lab.payer.nym, self.lab.hop.nym, s4["t_first"]), rec2)
        self.assertIsNone(bwkit.link_in_force(links, self.lab.payer.nym, self.lab.hop.nym, s1["t_first"]))
        rc, text = cli(["links"], mint_env(self.lab))
        self.assertEqual((rc, text.count("bw-forest for any payer")), (0, 2))

    def test_which_announcement_is_in_force(self):
        a = {"relay": "h", "payer": "*", "since": 100}
        b = {"relay": "h", "payer": "p", "since": 100}
        c = {"relay": "h", "payer": "*", "since": 200}
        d = {"relay": "other", "payer": "*", "since": 50}
        e = {"relay": "h", "payer": "q", "since": 150}
        self.assertIsNone(bwkit.link_in_force([a, b, c, d, e], "p", "h", 99))
        self.assertIs(bwkit.link_in_force([a, b, c, d, e], "p", "h", 100), b)      # the same since: the payer's own
        self.assertIs(bwkit.link_in_force([a, c, d], "p", "h", 150), a)
        self.assertIs(bwkit.link_in_force([a, b, c, d, e], "p", "h", 200), c)      # the newest since
        self.assertIs(bwkit.link_in_force([a, b, c, d, e], "q", "h", 160), e)
        self.assertIs(bwkit.link_in_force([a, b, c, d, e], "q", "h", 300), c)
        self.assertIsNone(bwkit.link_in_force([a, b, c, e], "p", "other", 300))
        f, g = dict(a, note=1), dict(a, note=2)                                      # still tied: the greater hash
        pick = bwkit.link_in_force([f, g], "p", "h", 100)
        self.assertEqual(bwkit.link_hash(pick), max(bwkit.link_hash(f), bwkit.link_hash(g)))
        self.assertEqual(bwkit.link_params(None)["margin"], 0.01)
        self.assertEqual(bwkit.link_params(dict(bwkit.PILOT_LINK, margin=0.3, relay="h"))["margin"], 0.3)

    def test_mint_refuses_what_it_must(self):
        import time
        rec, h = self.lab.hop.announce(now=self.lab.t, post=False)
        self.assertEqual(self.lab.hop.load_links(), [])                       # not posted, not kept
        post = lambda r: bwkit.http_post(self.lab.mint_url + "/link", r)   # noqa: E731
        self.assertEqual(post(rec), (200, {"stored": h}))
        status, body = post(rec)
        self.assertEqual((status, body["error"]), (409, "duplicate"))
        status, body = post(dict(rec, margin=0.5))
        self.assertEqual((status, body["error"]), (400, "bad signature"))
        status, body = post(dict(rec, extra=1))
        self.assertEqual(status, 400)
        hop = self.lab.hop
        for changes, code, word in ((dict(domain=self.lab.payer.nym), 409, "domain"),
                                    (dict(carry=1), 409, "profile"), (dict(light_time_s=10), 409, "profile"),
                                    (dict(unit="EUR/GB"), 409, "profile"), (dict(contact_plan={"at": 1}), 409, "profile"),
                                    (dict(payer=self.lab.mint.nym), 403, None),            # not in the peer list
                                    (dict(since=int(time.time()) - 60), 409, "since")):   # backdated
            status, body = post(self.resign(hop.priv, rec, **changes))
            self.assertEqual(status, code, (changes, body))
            if word:
                self.assertEqual(body["error"], word, (changes, body))
        stranger = bwkit.Ed25519PrivateKey.generate()
        status, body = post(self.resign(stranger, rec, relay=bwkit.nym_of_pub(stranger.public_key())))
        self.assertEqual(status, 403)
        status, body = post(self.resign(hop.priv, rec, payer=self.lab.payer.nym, since=self.lab.t + 5))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.lab.mint.load_links()), 2)
        # an announcement that would change a window the Mint already holds is refused
        self.lab.hour(1000, 1000)
        status, body = post(self.resign(hop.priv, rec, since=self.lab.t - 3600, margin=0.2))
        self.assertEqual((status, body["error"]), (409, "since"))
        self.assertIn("already held", body["detail"])
        for bad in (dict(rec, margin=2), dict(rec, min_bytes=-1), dict(rec, medium="Wi Fi"), dict(rec, since="now"),
                    dict(rec, proto=2), dict(rec, price=True), dict(rec, receipt_lifetime_s=-1)):
            with self.assertRaises(ValueError):
                bwkit.check_link(bad)
        hop.config["link"]["carry"] = 1                                        # the hop cannot announce outside profile 1
        with self.assertRaises(SystemExit):
            hop.announce(now=self.lab.t, post=False)


class Fee(unittest.TestCase):
    """The Steward's fee (spec, Section 8): f of every settlement, shown apart on the invoice."""

    def test_every_settlement_carries_the_stewards_fee(self):
        import time
        lab = Lab()
        try:
            lab.hour(1_000_000_000, 1_000_000_000)
            lab.hour(3_000_000_000, 2_000_000_000)
            ss = [e for e in lab.mint.ledger_lines() if e["kind"] == "settlement"]
            self.assertEqual([(s["amount_usd"], s["steward_fee"], s["fee_usd"]) for s in ss], [(0.1, 0.05, 0.005)] * 2)
            month = time.strftime("%Y-%m", time.localtime(lab.t))
            o = lab.mint.invoice(month)[(lab.payer.nym, lab.hop.nym)]
            self.assertEqual((o["amount_usd"], o["fee_usd"], o["relay_usd"]), (0.2, 0.01, 0.19))
            # a settlement from before profile 1 carries no fee, and none is taken from it
            legacy = bwkit.legacy_shape(dict(ss[0], receipt_sig="legacy", t_last=ss[0]["t_last"] + 1))
            self.assertEqual(set(ss[0]) - set(legacy), set(bwkit.SETTLEMENT_PROFILE_KEYS))
            lab.mint.append("ledger.jsonl", legacy)
            o = lab.mint.invoice(month)[(lab.payer.nym, lab.hop.nym)]
            self.assertEqual((o["windows"], o["amount_usd"], o["fee_usd"], o["relay_usd"]), (3, 0.3, 0.01, 0.29))
            # f is the Mint's configuration
            lab.mint.config["steward_fee"] = 0.1
            lab.hour(4_000_000_000, 4_000_000_000)
            s = [e for e in lab.mint.ledger_lines() if e["kind"] == "settlement"][-1]
            self.assertEqual((s["steward_fee"], s["fee_usd"]), (0.1, 0.01))
            rc, text = cli(["invoice", month], mint_env(lab))
            self.assertEqual(rc, 0)
            self.assertIn("4 windows (2 disputed), 4.00 GB, $0.4000: the relay's $0.3800, the Steward's $0.0200", text)
            self.assertIn("the Network Steward's share: $0.0200", text)
            rc, text = cli(["ledger"], mint_env(lab))
            self.assertIn("fee $0.005000  margin 0.01 (default)", text)
        finally:
            lab.close()


class Patience(unittest.TestCase):
    """The hop's patience (spec, Section 12): short windows counted per payer, the decline, the hook, the reset."""

    def setUp(self):
        self.lab = Lab()
        self.hook_out = os.path.join(self.lab.root, "declined.txt")
        hook = os.path.join(self.lab.root, "on-decline.sh")
        with open(hook, "w") as f:
            f.write('#!/bin/sh\necho "$1" >> "%s"\n' % self.hook_out)
        os.chmod(hook, 0o755)
        self.lab.hop.config["link"] = {"patience_windows": 2, "margin": 0.01, "min_bytes": 65536}
        self.lab.hop.config["on_decline"] = hook

    def tearDown(self):
        self.lab.close()

    def test_hop_declines_after_patience_windows_and_runs_the_hook(self):
        import time
        nym = self.lab.payer.nym
        self.lab.hour(1_000_000, 1_000_000)                   # agrees: not short
        self.lab.hour(3_000_000, 1_500_000)                   # the payer's count is the larger: not short
        self.assertEqual(self.lab.hop.patience_state()[nym]["short"], 0)
        self.lab.hour(4_000_000, 4_000_000)                   # payer 1 MB, hop 2.5 MB: short, 1 (a hop over-count looks the same)
        self.lab.hour(4_500_000, 6_000_000)                   # short, 2: patience spent
        self.assertEqual(self.lab.hop.patience_state()[nym], {"short": 2, "declined": False, "declined_at": None})
        out = self.lab.hour(5_000_000, 7_000_000)             # the next receipt is declined
        self.assertIsNone(out["ack"])
        ps = self.lab.hop.patience_state()[nym]
        self.assertEqual((ps["short"], ps["declined"], ps["declined_at"]), (2, True, self.lab.t))
        for _ in range(30):                                    # the hook ran with the payer's NYM as its argument
            if os.path.exists(self.hook_out):
                break
            time.sleep(0.1)
        with open(self.hook_out) as f:
            self.assertEqual(f.read().split(), [nym])
        st = json.loads(bwkit.http_get(self.lab.hop_url + "/status")[1])
        self.assertTrue(st["patience"][nym]["declined"])
        self.assertEqual(st["link"]["patience_windows"], 2)
        out = self.lab.hour(5_500_000, 8_000_000)             # and keeps declining, the hook not run again
        self.assertIsNone(out["ack"])
        with open(self.hook_out) as f:
            self.assertEqual(len(f.read().split()), 1)
        self.assertEqual(self.lab.mint.pending()[0].__len__(), 2)   # two receipts without ack on the Mint
        # the operator clears it; the next window is acked and covers the lost ones at the payer's count
        rc, text = cli(["patience"], hop_env(self.lab))
        self.assertIn("2 short, declined since", text)
        rc, text = cli(["patience", "--reset", nym], hop_env(self.lab))
        self.assertEqual((rc, self.lab.hop.patience_state()), (0, {}))
        out = self.lab.hour(6_000_000, 9_000_000)
        self.assertIsNotNone(out["ack"])
        self.assertEqual(out["ack"]["receipt"]["bytes"], 3_000_000)   # the hop's count since its last ack
        s = [e for e in self.lab.mint.ledger_lines() if e["kind"] == "settlement"][-1]
        self.assertEqual((s["settled_bytes"], s["status"]), (500_000, "disputed"))

    def test_patience_zero_never_declines_and_the_tolerance_is_the_links(self):
        self.lab.hop.config["link"] = {"patience_windows": 0, "clock_tolerance_s": 30}
        for i in range(4):
            out = self.lab.hour((i + 1) * 100_000, (i + 1) * 1_000_000, skew=12)   # short every time, and 12 s off
            self.assertIsNotNone(out["ack"])
        self.assertEqual(self.lab.hop.patience_state()[self.lab.payer.nym]["short"], 4)
        self.lab.hop.config["link"]["clock_tolerance_s"] = 5
        self.assertIsNone(self.lab.hour(500_000, 5_000_000, skew=12)["ack"])


class Profile(unittest.TestCase):
    """`proto: 1` in every record the kit writes; readers take a record without it as profile 1 and refuse other profiles."""

    def setUp(self):
        self.lab = Lab()

    def tearDown(self):
        self.lab.close()

    def test_proto_in_every_envelope_and_record(self):
        out = self.lab.hour(1000, 1000)
        self.assertEqual((out["receipt"]["proto"], out["receipt"]["impl"]), (1, "bwkit/" + bwkit.VERSION))
        self.assertEqual((out["ack"]["proto"], out["ack"]["impl"]), (1, "bwkit/" + bwkit.VERSION))
        self.assertNotIn("bwkit", out["receipt"])
        for e in self.lab.mint.ledger_lines():
            if e["kind"] in ("settlement", "commitment"):
                self.assertEqual(e["proto"], 1, e["kind"])
        self.assertEqual(self.lab.mint.proof(self.lab.t - 3600)["proto"], 1)
        # a 0.2.0 envelope: no proto, `bwkit` where `impl` is now: the hop acks it and the Mint stores it
        env = self.lab.payer.make_receipt(self.lab.t + 3600)
        del env["proto"], env["impl"]
        env["bwkit"] = "0.2.0"
        self.lab._patch_hop_clock(self.lab.t + 3600)
        ack = self.lab.hop.ack(env)
        self.assertEqual(ack["proto"], 1)
        status, body = bwkit.http_post(self.lab.mint_url + "/receipt", [env, ack])
        self.assertEqual((status, len(body["settlements"])), (200, 1))
        # another profile is refused everywhere
        env2 = dict(self.lab.payer.make_receipt(self.lab.t + 7200), proto=2)
        with self.assertRaises(ValueError):
            bwkit.check_envelope(env2)
        self.assertEqual(bwkit.http_post(self.lab.mint_url + "/receipt", env2)[0], 400)
        self.assertEqual(bwkit.http_post(self.lab.hop_url + "/ack", env2)[0], 400)
        doc = dict(self.lab.mint.proof(self.lab.t - 3600), proto=2)
        self.assertFalse(bwkit.check_proof(doc)[0])
        self.assertFalse(bwkit.check_proof(dict(doc, proto=True))[0])

    def test_mint_refuses_a_second_record_for_a_window_it_holds(self):
        out = self.lab.hour(1000, 1000)
        r2 = dict(out["receipt"]["receipt"], bytes=999)             # the same window, another count, re-signed
        env2 = dict(out["receipt"], receipt=r2, sig=bwkit.sign_receipt(self.lab.payer.priv, r2))
        status, body = bwkit.http_post(self.lab.mint_url + "/receipt", env2)
        self.assertEqual((status, body["error"]), (409, "replay"))
        a2 = dict(out["ack"]["receipt"], bytes=2000)                 # a second ack for the same receipt
        ack2 = dict(out["ack"], receipt=a2, sig=bwkit.sign_receipt(self.lab.hop.priv, a2))
        status, body = bwkit.http_post(self.lab.mint_url + "/receipt", ack2)
        self.assertEqual((status, body["error"]), (409, "replay"))
        self.assertEqual(len(self.lab.mint.ledger_lines()), 4)       # nothing stored
        # the rebuild keeps the first and names the second
        rows = [(1, out["receipt"]), (2, env2)]
        entries, rep = bwkit.rebuild(rows, [(1, out["ack"]), (2, ack2)], now=self.lab.t)
        self.assertEqual((rep["receipts"], rep["acks"], rep["settlements"]), (1, 1, 1))
        self.assertEqual(len(rep["bad"]), 2)
        self.assertTrue(all("replay" in b for b in rep["bad"]), rep["bad"])
        self.assertEqual(bwkit.compare(entries, self.lab.mint.ledger_lines())[0], True)


class RebuildWithLinks(unittest.TestCase):
    """The rebuild's third input (spec, Section 11): the announcements; and a ledger from before profile 1."""

    def setUp(self):
        self.lab = Lab()
        self.lab.hop.config["link"] = {"margin": 0.10}
        self.lab.hour(100_000_000, 105_000_000)               # the defaults: disputed
        self.rec, self.h = self.lab.hop.announce(now=self.lab.t, since=self.lab.t)
        self.lab.hour(200_000_000, 210_000_000)               # under the announcement: settled
        self.lab.hour(300_000_000, 340_000_000)               # under it: disputed
        self.files = {}
        for name, src in (("receipts.jsonl", self.lab.payer.path("receipts.jsonl")), ("acks.jsonl", self.lab.hop.path("acks.jsonl")),
                          ("links.jsonl", self.lab.hop.path("links.jsonl"))):
            self.files[name] = os.path.join(self.lab.root, name)
            shutil.copy(src, self.files[name])
        self.ledger = self.lab.mint.path("ledger.jsonl")

    def tearDown(self):
        self.lab.close()

    def rebuild(self, with_links=True):
        read = lambda n: bwkit.read_jsonl(self.files[n])[0]   # noqa: E731
        return bwkit.rebuild(read("receipts.jsonl"), read("acks.jsonl"), read("links.jsonl") if with_links else (), now=self.lab.t + 9)

    def test_rebuild_with_the_announcements_matches_and_without_them_differs(self):
        entries, rep = self.rebuild()
        self.assertEqual((rep["links"], rep["settlements"], rep["disputed"], rep["bad"]), (1, 3, 2, []))
        mine = [e for e in entries if e["kind"] == "settlement"]
        self.assertEqual([s["link"] for s in mine], [None, self.h, self.h])
        agree, verdict, lines = bwkit.compare(entries, self.lab.mint.ledger_lines())
        self.assertTrue(agree, (verdict, lines))
        self.assertEqual(verdict, "AGREE: 3 settlements in 3 hours rebuilt from the parties' copies match the Mint's ledger and its 3 commitments")
        entries2, rep2 = self.rebuild(with_links=False)
        agree, verdict, lines = bwkit.compare(entries2, self.lab.mint.ledger_lines())
        self.assertFalse(agree)
        self.assertTrue(verdict.startswith("DIFFER in windows "), verdict)
        self.assertEqual(len(lines), 4, lines)                 # two windows, and their two hours' roots
        self.assertIn("link None here, '%s' on the Mint" % self.h, lines[0])
        self.assertIn("margin 0.01 here, 0.1 on the Mint", lines[0])
        self.assertIn("status 'disputed' here, 'settled' on the Mint", lines[0])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = bwkit.main(["rebuild", self.files["receipts.jsonl"], self.files["acks.jsonl"], self.files["links.jsonl"], "--compare", self.ledger])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertTrue(out.getvalue().startswith("rebuilt 3 receipts and 3 acks under 1 announcements into 3 settlements (2 disputed) in 3 hours"), out.getvalue())
        # the Mint's copy of the registry serves as well as the hop's
        shutil.copy(self.lab.mint.path("links.jsonl"), self.files["links.jsonl"])
        self.assertTrue(bwkit.compare(self.rebuild()[0], self.lab.mint.ledger_lines())[0])
        # a forged announcement in the file is left out and named
        with open(self.files["links.jsonl"], "a") as f:
            f.write(json.dumps(dict(self.rec, margin=0.5)) + "\n")
        entries3, rep3 = self.rebuild()
        self.assertEqual(rep3["bad"], ["links.jsonl line 2: bad signature"])
        self.assertTrue(bwkit.compare(entries3, self.lab.mint.ledger_lines())[0])

    def legacy_ledger(self, entries, until=None):
        """The Mint's ledger as 0.2.0 wrote it, up to the hour `until`: settlements without the profile-1 members, commitments over those."""
        out, by_hour = [], {}
        for e in entries:
            if e["kind"] == "settlement" and (until is None or bwkit.commit_hour(e) < until):
                e = bwkit.legacy_shape(e)
            if e["kind"] == "settlement":
                by_hour.setdefault(bwkit.commit_hour(e), []).append(e)
            elif e["kind"] == "commitment":
                e = bwkit.make_commitment(self.lab.mint.priv, by_hour[e["window"]], e["window"], e["t"])
                if until is None or e["window"] < until:
                    del e["proto"]
            out.append(e)
        return out

    def test_a_ledger_from_before_profile_1_compares_in_its_own_form(self):
        lab = Lab()                                            # a lab with no announcement, as the 0.2.0 nodes ran
        try:
            lab.hour(1_000_000, 1_000_000)
            lab.hour(3_000_000, 2_000_000)
            lab.step(600, 3_500_000, 2_500_000)
            self.lab, keep = lab, self.lab
            try:
                legacy = self.legacy_ledger(lab.mint.ledger_lines())
            finally:
                self.lab = keep
            self.assertTrue(all("proto" not in e for e in legacy if e["kind"] in ("settlement", "commitment")))
            self.assertTrue(all(bwkit.verify_commitment(e) for e in legacy if e["kind"] == "commitment"))
            entries, rep = bwkit.rebuild(bwkit.read_jsonl(lab.payer.path("receipts.jsonl"))[0],
                                         bwkit.read_jsonl(lab.hop.path("acks.jsonl"))[0], now=lab.t + 9)
            self.assertTrue(all(s["proto"] == 1 for s in entries if s["kind"] == "settlement"))
            agree, verdict, lines = bwkit.compare(entries, legacy)
            self.assertTrue(agree, (verdict, lines))
            self.assertEqual(verdict, "AGREE: 3 settlements in 3 hours rebuilt from the parties' copies match the Mint's ledger and its 3 commitments "
                                      "(3 settled before profile 1, compared in that form)")
            # a mixed ledger: the first two hours before the upgrade, the last after
            self.lab, keep = lab, self.lab
            try:
                mixed = self.legacy_ledger(lab.mint.ledger_lines(), until=lab.t - 600 - (lab.t - 600) % 3600)
            finally:
                self.lab = keep
            agree, verdict, lines = bwkit.compare(entries, mixed)
            self.assertTrue(agree, (verdict, lines))
            self.assertIn("(2 settled before profile 1, compared in that form)", verdict)
            # and a legacy record differing in a shared field is still a difference
            legacy[2]["settled_bytes"] += 1
            agree, verdict, lines = bwkit.compare(entries, legacy)
            self.assertFalse(agree)
            self.assertIn("settled_bytes 1000000 here, 1000001 on the Mint", lines[0])
        finally:
            lab.close()

    def test_a_pre_profile_record_under_an_announcement_is_a_difference(self):
        # the Mint's ledger stripped to the 0.2.0 shape although an announcement was in force: the rebuild says so
        legacy = self.legacy_ledger(self.lab.mint.ledger_lines())
        entries, rep = self.rebuild()
        agree, verdict, lines = bwkit.compare(entries, legacy)
        self.assertFalse(agree)
        self.assertEqual(sum(1 for line in lines if "settled under announcement %s here; the Mint settled it before profile 1" % self.h[:16] in line), 2)


class Vectors(unittest.TestCase):
    """The kit against its own vectors (spec/CONFORMANCE.md): what it writes now is what is committed."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
        import conform
        import vectors
        cls.vectors, cls.conform = vectors, conform

    def test_the_committed_vectors_are_what_the_kit_writes(self):
        self.assertEqual(self.vectors.check(), [])
        files = self.vectors.generate(self.vectors.inputs())
        self.assertEqual(set(files), {"keys.json", "receipt.json", "ack.json", "link.json", "settlement-default.json",
                                      "settlement-disputed.json", "settlement-settled.json", "commitment.json", "proof.json",
                                      "rebuild/receipts.jsonl", "rebuild/acks.jsonl", "rebuild/links.jsonl", "rebuild/ledger.jsonl"})
        self.assertEqual((files["settlement-default.json"]["link"], files["settlement-default.json"]["status"]), (None, "settled"))
        self.assertEqual((files["settlement-disputed.json"]["link"], files["settlement-disputed.json"]["status"]),
                         (bwkit.link_hash(files["link.json"]), "disputed"))
        self.assertEqual((files["settlement-settled.json"]["price_usd_per_gb"], files["settlement-settled.json"]["margin"]), (0.2, 0.05))
        self.assertEqual((files["commitment.json"]["count"], len(files["proof.json"]["settlements"])), (3, 3))
        self.assertTrue(bwkit.check_proof(files["proof.json"], files["keys.json"]["mint"]["nym"])[0])

    def test_conform_passes_on_the_vectors_and_fails_on_a_changed_byte(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.conform.main([self.vectors.VECTORS_DIR]), 0)
        self.assertIn("conformance: PASS", out.getvalue())
        tmp = os.path.join(tempfile.mkdtemp(prefix="bwkit-conform-"), "v")
        shutil.copytree(self.vectors.VECTORS_DIR, tmp)
        try:
            p = os.path.join(tmp, "settlement-settled.json")
            with open(p) as f:
                s = json.load(f)
            s["settled_bytes"] += 1
            with open(p, "w") as f:
                json.dump(s, f)
            p = os.path.join(tmp, "proof.json")
            with open(p) as f:
                doc = json.load(f)
            doc["settlements"][1]["path"][0][0] = "L" if doc["settlements"][1]["path"][0][0] == "R" else "R"
            with open(p, "w") as f:
                json.dump(doc, f)
            os.remove(os.path.join(tmp, "ack.json"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(self.conform.main([tmp]), 1)
            text = out.getvalue()
            self.assertIn("FAIL settlement-settled.json: $.settled_bytes: 20000001 here, 20000000 expected", text)
            self.assertIn("FAIL proof.json: $.settlements[1].path[0][0]", text)
            self.assertIn("FAIL ack.json:", text)
            self.assertIn("FAIL proof.json verifies against the Mint's NYM", text)
            self.assertIn("ok   commitment.json", text)
            self.assertIn("conformance: FAIL", text)
        finally:
            shutil.rmtree(os.path.dirname(tmp))

    def test_the_vectors_pin_the_number_encodings(self):
        files = self.vectors.generate(self.vectors.inputs())
        ss = [e for e in files["rebuild/ledger.jsonl"] if e["kind"] == "settlement"]
        amounts = {json.dumps(s["amount_usd"]) for s in ss}
        fees = {json.dumps(s["fee_usd"]) for s in ss}
        self.assertTrue({"0.0", "5e-05", "0.01"} <= amounts, amounts)   # positional, exponent, and the zero with its point
        self.assertIn("3e-06", fees)
        tiny = [s for s in ss if s["settled_bytes"] == 1712][0]
        self.assertIn('"amount_usd":0.0,', bwkit.canon_json({k: v for k, v in tiny.items() if k != "t"}))
        small = [s for s in ss if s["settled_bytes"] == 500_000][0]
        self.assertIn('"amount_usd":5e-05,"fee_usd":3e-06,', bwkit.canon_json(small).replace('"hop"', "").replace("", ""))


class Rules(unittest.TestCase):
    """steward_rules.py with the floats the Mint's Steward feeds it; the simulator holds the same functions to exact figures."""

    def test_the_rules_with_floats(self):
        import steward_rules as rules
        self.assertAlmostEqual(rules.allowance(0.05, 0.01, 4.194, 0.0), 0.16776)
        self.assertEqual(rules.allowance(0.05, 0.01, 1.0, 0.05), 0)
        self.assertEqual(rules.draw(0.01, 0.004, 0.1, 0.002), (0.004, 0.002, 0.004))
        self.assertEqual(rules.draw(0.01, 0.1, 0.0, 0.0), (0.01, 0, 0))
        self.assertAlmostEqual(rules.match_size(0.04, 1.0, 0.0021, 0.0, 0.0021), 0.000084)   # m of the Steward's whole income
        self.assertEqual(rules.match_size(0.04, 0.0, 1.0, 0.0, 1.0), 0)
        self.assertAlmostEqual(rules.bounty_budget(0.0021, 0.04, 0.0021, 0.0), 0.0021 * 0.96)
        self.assertAlmostEqual(rules.second_bridge_size(0.05, 0.03752, 1.0), 0.001876)
        self.assertAlmostEqual(rules.second_bridge_premium(0.001876, 375_190_000), 0.005, places=4)
        self.assertEqual(rules.second_bridge_premium(0.1, 0), 0)
        self.assertAlmostEqual(rules.first_link_size(1.0, 0.00012, 0.0), 0.00012)
        self.assertAlmostEqual(rules.first_link_size(1.0, 5.0, 0.4), 0.6)
        paid, back = rules.premium_split(21_990_000, 50_000_000, 0.02)
        self.assertAlmostEqual(paid, 0.0004398)
        self.assertAlmostEqual(back, 0.0005602)
        paid, back = rules.premium_split(117_430_000, 50_000_000, 0.02)
        self.assertAlmostEqual(paid, 0.0023486)
        self.assertEqual(back, 0)
        self.assertEqual(rules.premium_split(0, 50_000_000, 0.02), (0, 0.001))


if __name__ == "__main__":
    unittest.main(verbosity=2)
