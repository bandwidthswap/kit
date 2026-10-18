"""The Network Steward: a rule, not a party (Section 7), one per settlement domain.

Income is the fraction f of every fee (Section 3.3). Output is purchase
commitments only (Section 5.1), under three programs:

1. Matching: when a bonded, aged payer escrows a commitment, add up to m of
   the payer's escrow, m strictly below f, and never more than m of the
   Steward's income in all.
2. The second bridge: at the end of each program window, wherever a link
   carried settled fees and the map shows no second path between its ends
   through a different mapped place, post a commitment for one, sized from
   the fees the sole link carried, with the sole link's effective price per
   gigabyte as the premium. Second bridges spend before first links.
3. The first link: every mapped place with no settled receipt across a link
   gets a standing commitment, sized by a parameter, renewed until a receipt
   settles across it. Funded from what is left.

Everything a commitment returns comes back to the escrow and is posted again.

The sizes and the cap are the pure functions of steward_rules.py at the kit's
root, which the Mint's Steward in bwkit.py imports too, so the simulator that
found the wash trader's second hill and the Steward on the Mint are one rule.
"""

import os
import sys
from fractions import Fraction

from . import routing
from .model import Commitment
from .money import GB, fmt, pct

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import steward_rules as rules  # noqa: E402


class Steward:
    def __init__(self, run, domain):
        self.run = run
        self.domain = domain
        self.unit = run.unit_of(domain)
        self.account = "steward:" + domain
        opening = run.scn.opening_escrow.get(domain, Fraction(0))
        self.escrow = opening
        self.income = opening            # fees received, the opening balance counted as earlier income
        self.received = Fraction(0)      # fees received during the run
        self.matched = Fraction(0)
        self.posted = Fraction(0)        # bounties posted (second bridge and first link)
        self.refunds = Fraction(0)       # escrow that came back
        self.declined = []               # (window, what, why) postings the rule could not make
        self.fees_in = {}                # payer NYM -> gross fees it has paid for carriage in this domain
        self.paid_on = {}                # payer NYM -> what the Steward has paid out on that payer's receipts
        self.paid_out = Fraction(0)
        self.capped = Fraction(0)        # what the cap held back

    # -------------------------------------------------------------- income

    def receive(self, amount):
        self.escrow += amount
        self.income += amount
        self.received += amount

    def refund(self, amount):
        self.escrow += amount
        self.refunds += amount

    # -------------------------------------------------------------- the per-payer cap (Section 3.3)

    def fee_in(self, payer, gross):
        """A receipt with this payer settled for `gross`; the Steward took f of it."""
        self.fees_in[payer] = self.fees_in.get(payer, Fraction(0)) + gross

    def allowance(self, payer):
        """What the Steward may still pay out on this payer's receipts: (f - margin) of its fees in, less what it has."""
        p = self.run.params
        return Fraction(rules.allowance(p["f"], p["steward_margin"], self.fees_in.get(payer, Fraction(0)),
                                        self.paid_on.get(payer, Fraction(0))))

    def draw(self, cm, amount, payer):
        """Take `amount` from a commitment's pools for a payout on `payer`'s receipt: the poster's money first,
        then the Steward's, which the per-payer cap limits. Returns (from the poster, from the Steward)."""
        own, match, held = rules.draw(amount, cm.pool_own, cm.pool_match, self.allowance(payer))
        own, match, held = Fraction(own), Fraction(match), Fraction(held)
        cm.pool_own -= own
        if held > 0:
            self.capped += held
        if match > 0:
            cm.pool_match -= match
            self.paid_on[payer] = self.paid_on.get(payer, Fraction(0)) + match
            self.paid_out += match
        return own, match

    # -------------------------------------------------------------- program 1

    def match(self, cm, w):
        p = self.run.params
        m = p["m"]
        if cm.kind != "payer" or m <= 0:
            return Fraction(0)
        poster = self.run.nodes.get(cm.poster)
        if poster is None:
            return Fraction(0)
        age = w - poster.bond_since
        if age < p["min_bond_age"]:
            self.declined.append((w, "match " + cm.id, "the poster's bond is %d windows old, the program needs %d" % (age, p["min_bond_age"])))
            self.run.log(w, "Steward (%s): no match for %s, the poster's bond is too young" % (self.domain, cm.label or cm.id))
            return Fraction(0)
        amt = Fraction(rules.match_size(m, cm.escrow, self.income, self.matched, self.escrow))
        if amt <= 0:
            why = "no escrow" if self.escrow <= 0 else "the match allowance (m of income) is spent"
            self.declined.append((w, "match " + cm.id, why))
            self.run.log(w, "Steward (%s): no match for %s, %s" % (self.domain, cm.label or cm.id, why))
            return Fraction(0)
        self.escrow -= amt
        self.matched += amt
        per_window = cm.escrow / cm.windows if cm.windows else None
        cm.pool_match += amt
        cm.escrow += amt
        cm.contributions[self.account] = cm.contributions.get(self.account, Fraction(0)) + amt
        if per_window:
            extra = int(amt / per_window)
            cm.windows += extra      # the match buys more windows at the poster's own terms
        self.run.pay(self.account, "escrow:" + cm.id, amt, self.unit, w,
                     "Steward matches %s of the poster's escrow (m = %s)" % (fmt(amt, self.unit, self.run.symbol(self.unit)), pct(m)),
                     kind="match", commitment=cm.id)
        self.run.log(w, "Steward (%s): matches %s with %s (m = %s of %s), %d more windows" % (
            self.domain, cm.label or cm.id, fmt(amt, self.unit, self.run.symbol(self.unit)), pct(m),
            fmt(cm.escrow - amt, self.unit, self.run.symbol(self.unit)), extra if per_window else 0))
        return amt

    # -------------------------------------------------------------- programs 2 and 3

    def bounty_budget(self):
        """What the second-bridge and first-link programs may spend: the escrow less the unspent match allowance."""
        p = self.run.params
        return Fraction(rules.bounty_budget(self.escrow, p["m"], self.income, self.matched))

    def run_programs(self, w):
        run = self.run
        p = run.params
        # 2. the second bridge
        stats = [(st["fees"], lid, st) for lid, st in run.link_stats.items()
                 if run.links[lid].domain == self.domain and st["fees"] > 0]
        stats.sort(key=lambda t: (-t[0], t[1]))
        for fees, lid, st in stats:
            link = run.links[lid]
            ends = (set(st["origin_places"]) | set(st["dest_places"])) - {None}
            if len(ends) <= 1:
                # a link whose two ends are at one place, like the bench wire: there is no second place to go
                # through, so there is no second bridge to post for (PLAN.md 2.4, NOTES.md 36)
                run.log(w, "Steward (%s): %s carried %s inside one place; no second bridge to post for" % (
                    self.domain, lid, fmt(fees, self.unit, run.symbol(self.unit))))
                continue
            if not routing.is_sole(run, link, w, st["origins"], st["dests"], st["places"]):
                continue
            if any(c.open and c.kind == "second_bridge" and c.avoid_link == lid for c in run.commitments.values()):
                continue
            budget = self.bounty_budget()
            if budget <= 0:
                self.declined.append((w, "second bridge for " + lid, "no escrow left"))
                run.log(w, "Steward (%s): %s is a sole bridge carrying %s, but the escrow is spent" % (
                    self.domain, lid, fmt(fees, self.unit, run.symbol(self.unit))))
                break
            take = p["f"] * fees                       # the Steward's income from the sole link
            value = Fraction(rules.second_bridge_size(p["f"], fees, budget))
            windows = p["program_window"]
            premium = Fraction(rules.second_bridge_premium(value, st["bytes"]))
            cm = Commitment(id=run.new_id(self.domain + "-bridge"), kind="second_bridge", poster=self.account,
                            domain=self.domain, unit=self.unit, min_bytes=st["bytes"] // windows, premium=premium,
                            windows=windows, deadline=w + 1 + p["bounty_deadline"], posted_at=w + 1,
                            escrow=value, contributions={self.account: value}, pool_match=value,
                            from_places=frozenset(st["origin_places"]) - {None},
                            to_places=frozenset(st["dest_places"] | {routing.place_of(run, link.dst)}) - {None},
                            avoid_link=lid, existing_places=frozenset(st["places"]),
                            label="second path around %s" % lid)
            self._post(cm, w, "sole bridge %s carried %s in %d windows, %s of it the Steward's; bounty for a second path from %s to %s through a mapped place other than %s, premium %s/GB" % (
                lid, fmt(fees, self.unit, run.symbol(self.unit)), windows, fmt(take, self.unit, run.symbol(self.unit)),
                ", ".join(run.places[x].name for x in sorted(cm.from_places)),
                ", ".join(run.places[x].name for x in sorted(cm.to_places)),
                ", ".join(run.places[x].name for x in sorted(cm.existing_places)),
                fmt(premium, self.unit, run.symbol(self.unit))))
        # 3. the first link
        if p["first_link_size"] > 0:
            for place in sorted(run.places.values(), key=lambda x: x.id):
                if place.domain != self.domain or not place.mapped or place.since > w:
                    continue
                if place.id in run.place_linked:
                    continue
                standing = [c for c in run.commitments.values() if c.open and c.kind == "first_link" and c.to_place == place.id]
                if standing:
                    # the offer stands: top it up toward the program's size from what the budget allows (PLAN.md 2.4, NOTES.md 37)
                    cm = standing[0]
                    value = Fraction(rules.first_link_size(p["first_link_size"], self.bounty_budget(), cm.escrow))
                    if value > 0:
                        self.escrow -= value
                        self.posted += value
                        cm.escrow += value
                        cm.pool_match += value
                        cm.contributions[self.account] = cm.contributions.get(self.account, Fraction(0)) + value
                        run.pay(self.account, "escrow:" + cm.id, value, self.unit, w, "the Steward tops up %s" % cm.label,
                                kind="post", commitment=cm.id)
                        run.log(w, "Steward (%s): tops up %s by %s to %s (toward %s)" % (
                            self.domain, cm.label, fmt(value, self.unit, run.symbol(self.unit)),
                            fmt(cm.escrow, self.unit, run.symbol(self.unit)), fmt(p["first_link_size"], self.unit, run.symbol(self.unit))))
                    continue
                budget = self.bounty_budget()
                value = Fraction(rules.first_link_size(p["first_link_size"], budget, Fraction(0)))
                if value <= 0:
                    self.declined.append((w, "first link to " + place.id, "no escrow left"))
                    break
                cm = Commitment(id=run.new_id(self.domain + "-first"), kind="first_link", poster=self.account,
                                domain=self.domain, unit=self.unit, min_bytes=0, premium=p["first_link_premium"],
                                windows=p["bounty_deadline"], deadline=w + 1 + p["bounty_deadline"], posted_at=w + 1,
                                escrow=value, contributions={self.account: value}, pool_match=value,
                                to_place=place.id, label="first link to %s" % place.name)
                self._post(cm, w, "standing offer for the first link to %s, premium %s/GB" % (
                    place.name, fmt(p["first_link_premium"], self.unit, run.symbol(self.unit))))

    def _post(self, cm, w, why):
        self.escrow -= cm.escrow
        self.posted += cm.escrow
        self.run.commitments[cm.id] = cm
        self.run.pay(self.account, "escrow:" + cm.id, cm.escrow, self.unit, w, "the Steward posts %s" % cm.label,
                     kind="post", commitment=cm.id)
        self.run.log(w, "Steward (%s): posts %s of %s: %s" % (
            self.domain, fmt(cm.escrow, self.unit, self.run.symbol(self.unit)), cm.label, why))
