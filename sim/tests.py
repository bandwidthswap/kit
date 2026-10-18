"""Every scenario and attack, with the field note's headline numbers asserted.

    python3 -m sim.tests          or    python3 -m unittest sim.tests
"""

import csv
import os
import sys
import tempfile
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.engine import Run                      # noqa: E402
from sim.money import GB, money                 # noqa: E402
from sim.receipts import settle_counts          # noqa: E402
from sim.report import render, write_csv        # noqa: E402
from sim.scenario import load                   # noqa: E402
from sim.attacks import mint, monopolist, wash  # noqa: E402

F = Fraction
RUNS = {}


def run(name):
    if name not in RUNS:
        RUNS[name] = Run(load(name)).go()
    return RUNS[name]


def cents(x):
    """Round an exact amount to the cent, as the field note prints it."""
    return round(F(x), 2)


def assert_cap_held(tc, run):
    """The Steward never paid out, on any one payer's receipts, more than (f − margin) of the fees that payer paid in."""
    bound = run.params["f"] - run.params["steward_margin"]
    for st in run.stewards.values():
        for payer, paid in st.paid_on.items():
            tc.assertLessEqual(paid, bound * st.fees_in.get(payer, F(0)), "%s on %s" % (st.account, payer))


class Receipts(unittest.TestCase):
    def test_the_labs_three_tests(self):
        self.assertEqual(settle_counts(21938052, 21939460), (21938052, "settled"))
        self.assertEqual(settle_counts(21945428, 1712), (1712, "disputed"))
        self.assertEqual(settle_counts(272, 21947428), (272, "disputed"))

    def test_against_bwkit_when_it_imports(self):
        try:
            import bwkit
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            self.skipTest("bwkit needs the cryptography package")
        payer, hop = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
        pn, hn = bwkit.nym_of_pub(payer.public_key()), bwkit.nym_of_pub(hop.public_key())
        for x, y in ((21938052, 21939460), (21945428, 1712), (272, 21947428)):
            r = {"payer": pn, "hop": hn, "hop_index": 1, "merkle_root": "0" * 64, "bytes": x,
                 "t_first": 1000, "t_last": 4599, "prev_sig": ""}
            rsig = bwkit.sign_receipt(payer, r)
            a = dict(r, bytes=y, prev_sig=rsig)
            asig = bwkit.sign_receipt(hop, a)
            s = bwkit.resolve({"kind": "receipt", "receipt": r, "sig": rsig}, {"kind": "ack", "receipt": a, "sig": asig})
            self.assertEqual((s["settled_bytes"], s["status"]), settle_counts(x, y))


class Bench(unittest.TestCase):
    def test_forced_tests(self):
        r = run("bench")
        by_w = {s.window: s for s in r.settlements}
        self.assertEqual((by_w[0].settled_bytes, by_w[0].status), (21938052, "settled"))
        self.assertEqual((by_w[1].settled_bytes, by_w[1].status), (1712, "disputed"))
        self.assertEqual((by_w[2].settled_bytes, by_w[2].status), (272, "disputed"))
        self.assertEqual(by_w[3].status, "declined")
        self.assertEqual(by_w[4].status, "settled")
        # about two tenths of a cent for the agreeing window, 5% of it to the Steward
        self.assertEqual(by_w[0].amount, F(21938052, GB) * F(1, 10))
        self.assertEqual(r.received("bw-forest", 0, 0) + r.received("bw-mint", 0, 0, kinds=("fee",)), by_w[0].amount)
        self.assertEqual(r.steward_take("lab", 0, 0), by_w[0].amount * F(1, 20))


class Street(unittest.TestCase):
    def test_working_day(self):
        r = run("street")
        self.assertEqual(r.received("h1", 0, 23), F("2.85"))
        self.assertEqual(r.received("h2", 0, 23), F("2.85"))
        self.assertEqual(r.received("h3", 0, 23), F("0.95"))
        self.assertEqual(r.steward_take("street", 0, 23), F("0.35"))
        self.assertEqual(r.paid("h1a", 0, 23) + r.paid("h2a", 0, 23) + r.paid("h3a", 0, 23), F("7.00"))
        self.assertFalse([c for c in r.commitments.values() if c.posted_at <= 24])

    def test_house_ones_dish_down(self):
        r = run("street")
        self.assertEqual(r.received("h2", 24, 47), F("7.60"))
        self.assertEqual(r.received("h1", 24, 47), F("0.57"))
        self.assertEqual(r.paid("h1", 24, 47), F("2.00"))
        self.assertEqual(r.received("h3", 24, 47), F("0.95"))
        self.assertEqual(r.steward_take("street", 24, 47), F("0.48"))
        payers = ("h1a", "h2a", "h3a", "h1")
        self.assertEqual(sum(r.paid(p, 24, 47) for p in payers), F("9.60"))
        bounties = [c for c in r.commitments.values() if c.kind == "second_bridge"]
        # the Steward's take of the Wi-Fi's 60¢, not the 60¢ itself
        self.assertEqual([(c.avoid_link, c.escrow) for c in bounties], [("wifi", F("0.03"))])
        self.assertEqual(bounties[0].premium, F("0.001"))


class Cabin(unittest.TestCase):
    def rows(self, r, w, commitment="cabin-bounty"):
        return [p for p in r.rows(w, w) if p.commitment == commitment and p.from_ == "escrow:" + commitment]

    def test_full_window_pays_thirty_cents(self):
        r = run("cabin")
        rows = self.rows(r, 48)
        self.assertEqual(sum(p.amount for p in rows), F("0.30"))
        self.assertEqual(sum(p.amount for p in rows if p.to == "ridge"), F("0.114"))
        self.assertEqual(sum(p.amount for p in rows if p.to == "h3"), F("0.171"))
        self.assertEqual(sum(p.amount for p in rows if p.to == "mint"), F("0.015"))

    def test_rain_prorates_the_premium(self):
        r = run("cabin")
        rows = self.rows(r, 60)
        paid = sum(p.amount for p in rows if p.to != "cabin")
        back = sum(p.amount for p in rows if p.to == "cabin")
        self.assertEqual(paid, F("0.125"))
        self.assertEqual(back, F("0.05"))
        self.assertEqual(sum(p.amount for p in rows if p.to == "mint"), F("0.00625"))

    def test_hiker_is_reimbursed(self):
        r = run("cabin")
        self.assertEqual(r.paid("hiker", 72, 72), F("0.045"))
        self.assertEqual(r.received("hiker", 72, 72, kinds=("reimburse",)), F("0.045"))
        hiker = [p for p in r.rows(72, 72) if p.demand == "hiker" and p.kind == "payment"]
        self.assertEqual(sum(p.amount for p in hiker if p.to == "ridge"), F("0.01425"))
        self.assertEqual(sum(p.amount for p in hiker if p.to == "h3"), F("0.0285"))

    def test_dark_relay_moves_nothing(self):
        r = run("cabin")
        self.assertFalse([p for p in r.rows(100, 100) if p.demand == "cabin"])
        self.assertTrue([u for u in r.unserved if u[0] == 100 and u[1] == "cabin"])

    def test_match_and_deadline(self):
        r = run("cabin")
        cm = r.commitments["cabin-bounty"]
        self.assertEqual(cm.contributions["steward:street"], F("4.50"))
        self.assertEqual(cm.windows, 738)
        back_cabin = r.received("cabin", 1440, 1440, kinds=("refund",))
        back_steward = sum(p.amount for p in r.rows(1440, 1440) if p.to == "steward:street" and p.commitment == "cabin-bounty")
        self.assertGreater(back_cabin, 0)
        # the cabin's own money is drawn first; the ridge went dark before the matched windows, so the whole match returns
        self.assertEqual(back_steward, F("4.50"))
        self.assertEqual(back_cabin + back_steward + cm.paid_out + r.received("cabin", 0, 1439, kinds=("refund",)), F("184.50"))
        self.assertEqual(cm.status, "expired")
        self.assertEqual(cm.pool, 0)

    def test_without_the_match(self):
        r = run("cabin-nomatch")
        cm = r.commitments["cabin-bounty"]
        self.assertNotIn("steward:street", cm.contributions)
        self.assertEqual(cm.windows, 720)
        self.assertEqual(sum(p.amount for p in self.rows(r, 48)), F("0.30"))


class Moon(unittest.TestCase):
    def test_a_day_home(self):
        r = run("earth-moon")
        self.assertEqual(r.received("moon-relay", 0, 23), F("1140.00"))
        self.assertEqual(r.steward_take("earth", 0, 23), F("60.00"))
        self.assertEqual(r.steward_take("moon", 0, 23), 0)
        self.assertEqual(r.paid_per_gb("home", "earth-inst", 0, 23), F(1))
        self.assertEqual(r.kept_per_gb("home", "moon-relay", 0, 23), F("0.95"))

    def test_widened_tolerance(self):
        r = run("earth-moon")
        by_w = {s.window: s for s in r.settlements if s.link == "surface"}
        self.assertEqual(by_w[30].status, "settled")
        self.assertEqual(by_w[31].status, "declined")
        self.assertGreater(by_w[30].tolerance_s, F("7.5"))

    def test_two_mints_agree(self):
        r = run("earth-moon")
        self.assertTrue(r.mints["moon"].mirrors)
        self.assertEqual([w for (w, d) in r.mints["earth"].reconciliations], [23, 47, 71])
        self.assertFalse(r.excluded)


class Planets(unittest.TestCase):
    def test_clear_line(self):
        r = run("earth-mars-jupiter")
        self.assertEqual(r.paid_per_gb("europa", "earth-inst", 0, 99, "USD"), F(50))
        self.assertEqual(r.kept_per_gb("europa", "jup-orbiter", 0, 99, "USD"), F("47.50"))
        self.assertEqual(r.kept_per_gb("europa", "lander", 0, 99, "JUP"), F("1.90"))

    def test_around_mars(self):
        r = run("earth-mars-jupiter")
        w0, w1 = 101, 139
        self.assertEqual(r.paid_per_gb("europa", "earth-inst", w0, w1, "USD"), F(63))
        self.assertEqual(r.kept_per_gb("europa", "mars-bridge", w0, w1, "USD"), F("21.85"))
        self.assertEqual(r.kept_per_gb("europa", "jup-orbiter", w0, w1, "MRS"), F("38.00"))
        g = r.gb_delivered("europa", w0, w1)
        rows = r.bundle_rows("europa", w0, w1)
        earth = sum(p.amount for p in rows if p.to == "steward:earth" and p.kind == "remit") / g
        mars = sum(p.amount for p in rows if p.to == "steward:mars" and p.kind == "remit") / g
        self.assertEqual((earth, mars), (F("1.15"), F("2.00")))

    def test_mars_behind_the_sun(self):
        r = run("earth-mars-jupiter")
        self.assertFalse([x for x in r.delivered if 140 <= x[0] <= 147])
        self.assertEqual(len([u for u in r.unserved if 140 <= u[0] <= 147]), 8)
        self.assertIn((143, "earth", "jupiter"), r.missed_reconciliations)
        self.assertFalse(r.excluded)


class Wash(unittest.TestCase):
    def test_paper_defaults_lose(self):
        net, _ = wash.run_wash(F("0.05"), F("0.025"))
        self.assertEqual(net, F("0.025") * F("0.95") - F("0.05"))
        self.assertLess(net, 0)

    def test_never_positive_with_the_cap(self):
        """Beyond f − margin the per-payer cap holds the match back, so no m makes the trader whole."""
        nets = {}
        for m in (F("0.05"), F("0.055"), F("0.10")):
            net, run = wash.run_wash(F("0.05"), m)
            self.assertLess(net, 0)
            assert_cap_held(self, run)
            nets[m] = net
        # the curve is flat once the cap binds: more match buys nothing
        self.assertLess(abs(nets[F("0.10")] - nets[F("0.055")]), F("0.002"))

    def test_without_the_cap_it_turned_positive_above_f(self):
        self.assertGreater(wash.run_wash(F("0.05"), F("0.055"), margin=F("-1"))[0], 0)

    def test_second_bridge_with_ownership_seen(self):
        net, _ = wash.run_wash(F("0.05"), F("0.025"), every_program=True, sees_ownership=True)
        self.assertLess(net, 0)

    def test_second_bridge_with_ownership_unseen(self):
        """NOTES.md item 14: with the bounty sized at f of the fees and the per-payer cap, the trader loses either way."""
        deep, run = wash.run_wash(F("0.05"), F("0.025"), every_program=True, sees_ownership=False)
        own, _ = wash.run_wash(F("0.05"), F("0.025"), every_program=True, sees_ownership=False, pool=F(0))
        self.assertLess(deep, 0)
        self.assertLess(own, 0)
        assert_cap_held(self, run)
        # the bounty is the Steward's take of the sole link's fees
        bounty = [c for c in run.commitments.values() if c.kind == "second_bridge"][0]
        self.assertEqual(bounty.escrow, F("0.05") * 24)

    def test_splitting_across_payer_nyms_gains_nothing(self):
        nets = [wash.run_wash(F("0.05"), F("0.025"), every_program=True, sees_ownership=False, payers=k)[0] for k in (1, 2, 4)]
        self.assertLess(max(nets), 0)
        self.assertLess(max(nets) - min(nets), F("0.0005"))


class Monopolist(unittest.TestCase):
    def test_same_hill_earns_nothing(self):
        base = monopolist.run_monopolist(None)
        same = monopolist.run_monopolist("same-hill")
        other = monopolist.run_monopolist("other-hill")
        self.assertEqual(base["earned"], F("912.00"))
        self.assertEqual(same["earned"], F("820.80"))
        self.assertEqual(same["bounty"], 0)
        self.assertEqual(same["moved"], F(960))
        self.assertGreater(other["bounty"], 0)
        self.assertGreater(same["open"], 0)


class UnderRemit(unittest.TestCase):
    def test_found_at_first_reconciliation_after(self):
        r = mint.run_mint("stop")
        self.assertEqual(r["kept"], r["due"])
        self.assertEqual(r["kept"], F(420))
        self.assertEqual(r["found"], {"earth": 187, "mars": 187})
        self.assertFalse(r["run"].links["jup-local"].up)
        self.assertFalse(r["slashed"])

    def test_bridge_that_keeps_settling_is_slashed(self):
        r = mint.run_mint("keep")
        self.assertEqual(sorted((d, b) for (_, _, d, b) in r["slashed"]), [("earth", F(50000)), ("mars", F(50000))])


class Rules(unittest.TestCase):
    """steward_rules.py, the one file both the simulator and the kit's Steward import, held to the field note's figures."""

    def test_the_rules_give_the_field_notes_numbers(self):
        import steward_rules as rules
        # the street's second bridge: the Steward's take of the Wi-Fi's 60 cents, premium a tenth of a cent on 30 GB
        self.assertEqual(rules.second_bridge_size(F("0.05"), F("0.60"), F(100)), F("0.03"))
        self.assertEqual(rules.second_bridge_premium(F("0.03"), 30 * GB), F("0.001"))
        # the cabin: a full window pays the premium on the bytes moved; the rain pays in proportion and returns the rest
        self.assertEqual(rules.premium_split(F(1_200_000_000), 1_000_000_000, F("0.25")), (F("0.30"), 0))
        self.assertEqual(rules.premium_split(500_000_000, 700_000_000, F("0.25")), (F("0.125"), F("0.05")))
        # the match: m of the escrow, within m of the income less matches made, within the escrow in hand
        self.assertEqual(rules.match_size(F("0.025"), F(180), F(200), F(0), F(200)), F("4.50"))
        self.assertEqual(rules.match_size(F("0.025"), F(180), F(10), F(0), F(200)), F("0.25"))
        self.assertEqual(rules.match_size(F("0.025"), F(180), F(200), F(5), F(200)), 0)
        # the cap: (f - margin) of the fees paid in, less what was paid out; the draw takes the poster's money first
        self.assertEqual(rules.allowance(F("0.05"), F("0.005"), F(100), F(1)), F("3.5"))
        self.assertEqual(rules.draw(F(10), F(4), F(10), F("3.5")), (F(4), F("3.5"), F("2.5")))
        self.assertEqual(rules.draw(F(3), F(4), F(10), F(0)), (F(3), 0, 0))
        self.assertEqual(rules.bounty_budget(F(10), F("0.025"), F(100), F(1)), F("8.5"))
        self.assertEqual(rules.first_link_size(F(2), F(10), F("0.5")), F("1.5"))
        self.assertEqual(rules.first_link_size(F(2), F("0.3"), F(0)), F("0.3"))

    def one_place(self, windows):
        return {
            "scenario": {"name": "one place", "description": "a link inside one place, and a mapped place with none",
                         "windows": windows, "window_seconds": 3600},
            "params": {"f": "0.05", "m": "0", "program_window": 1, "first_link_size": "1.00", "first_link_premium": "0.05",
                       "bounty_deadline": 168},
            "domains": [{"id": "lab", "name": "the lab", "unit": "USD", "symbol": "$", "mint": "bw-mint"}],
            "places": [{"id": "house", "name": "the house", "domain": "lab", "medium": "wire", "range": "building"},
                       {"id": "cabin", "name": "the cabin", "domain": "lab", "medium": "lora", "range": "hill"}],
            "nodes": [{"id": "bw-house", "name": "bw-house", "place": "house", "roles": ["payer"]},
                      {"id": "bw-forest", "name": "bw-forest", "place": "house", "roles": ["relay"]},
                      {"id": "bw-mint", "name": "bw-mint", "place": "house", "roles": ["mint"]}],
            "links": [{"id": "wg0", "src": "bw-house", "dst": "bw-forest", "owner": "bw-forest", "price": 0.10,
                       "medium": "wire", "range": "building", "clock_tolerance_s": 5, "window_kind": "hour"}],
            "demands": [{"id": "tunnel", "payer": "bw-house", "src": "bw-house", "dst": "bw-forest", "bytes_per_window": GB}],
        }

    def test_a_link_inside_one_place_has_no_second_bridge_and_the_first_link_is_topped_up(self):
        from sim.attacks import run_dict
        r = run_dict(self.one_place(3), "one-place")
        self.assertFalse([c for c in r.commitments.values() if c.kind == "second_bridge"])
        self.assertTrue(any("inside one place" in text for _, text in r.events_log), r.events_log[-5:])
        fl = [c for c in r.commitments.values() if c.kind == "first_link"]
        self.assertEqual(len(fl), 1)                              # one standing offer, not one per run
        self.assertEqual(fl[0].to_place, "cabin")
        self.assertEqual(fl[0].escrow, F("0.015"))                # posted at half a cent, topped up twice toward $1.00
        self.assertEqual(r.stewards["lab"].posted, F("0.015"))
        self.assertEqual(r.stewards["lab"].escrow, 0)
        self.assertEqual(sum(1 for p in r.payments if p.kind == "post" and p.commitment == fl[0].id), 3)


class Output(unittest.TestCase):
    def test_render_and_csv(self):
        for name in ("bench", "street", "cabin", "cabin-nomatch", "earth-moon", "earth-mars-jupiter"):
            r = run(name)
            text = render(r)
            self.assertIn("Over the run", text)
            with tempfile.TemporaryDirectory() as d:
                path = write_csv(r, os.path.join(d, name + ".csv"))
                with open(path) as fh:
                    rows = list(csv.reader(fh))
            self.assertEqual(rows[0], ["from", "to", "amount", "unit", "window", "reason"])
            self.assertEqual(len(rows) - 1, len(r.payments))

    def test_cap_holds_in_every_scenario(self):
        for name in ("street", "cabin", "cabin-nomatch", "earth-moon", "earth-mars-jupiter"):
            assert_cap_held(self, run(name))
        for r in (monopolist.run_monopolist("other-hill")["run"], mint.run_mint("stop")["run"]):
            assert_cap_held(self, r)

    def test_books_balance(self):
        """Every unit that enters as a payment leaves as a payment: the accounts sum to zero."""
        for name in ("street", "cabin", "earth-moon", "earth-mars-jupiter"):
            r = run(name)
            for unit in {p.unit for p in r.payments}:
                total = sum((p.amount for p in r.payments if p.unit == unit), F(0))
                paid = sum((p.amount for p in r.payments if p.unit == unit), F(0))
                self.assertEqual(total, paid)
                accounts = {p.from_ for p in r.payments} | {p.to for p in r.payments}
                self.assertEqual(sum((r.net(a, unit=unit) for a in accounts), F(0)), 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
