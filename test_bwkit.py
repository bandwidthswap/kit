#!/usr/bin/env python3
"""Runs a payer, a hop and a Mint in one process with file-backed meters.

    python3 test_bwkit.py

Covers: signing and tamper detection, the agreeing window, the two kinds of
disagreement and the rule that settles them, the clock rule, unknown and
badly signed senders, the outbox when the Mint is down, offline resolution
with `settle`, and the monthly invoice.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

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
        self.assertFalse(bwkit.verify_receipt(nym, r, sig[:-2] + "AA"))
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
        self.assertEqual(kinds, ["receipt", "ack", "settlement"])

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
        self.assertNotIn("ed25519:", text)                   # names and roles only
        self.assertNotIn("127.0.0.1", text)
        bwkit.assert_public(text)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
