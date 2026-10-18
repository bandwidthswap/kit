"""Mints: bookkeepers (Section 4), one per settlement domain, reconciling across domains (A.5).

A Mint collects the fee on every settlement it writes and remits it to its
domain's Steward. It publishes what it settles; a relay bonded on both sides
of a link carries a copy to the Mint on the other side. When a link between
two domains exists at a reconciliation window, each Mint checks the other's
publications since they last met: a Mint that has remitted less than f of
the fees it settled is shown to have done so from its own publications, and
is excluded, not slashed. A bridge relay bonded on the honest side either
stops settling with the condemned Mint or is slashed when it next does.
"""

from fractions import Fraction

from . import routing
from .money import fmt, pct


class Mint:
    def __init__(self, run, domain):
        self.run = run
        self.domain = domain
        self.unit = run.unit_of(domain)
        node = run.domains[domain].mint
        self.account = node if node is not None else "mint:" + domain
        self.collected = Fraction(0)
        self.remitted = Fraction(0)
        self.kept = Fraction(0)
        self.under = None                # (from_window, to_window, keep fraction)
        self.records = []                # settlements this Mint wrote
        self.mirrors = []                # settlements carried here by a bridge relay
        self.last_reconciled = {}        # other domain -> window
        self.excluded = set()            # domains this Mint has excluded
        self.excluded_by = set()
        self.reconciliations = []        # (window, other domain)

    def under_remit(self, a, b, keep):
        self.under = (a, b, keep)

    def keep_fraction(self, w):
        if self.under and self.under[0] <= w <= self.under[1]:
            return self.under[2]
        return Fraction(0)

    def collect_fee(self, from_acct, fee_due, w, reason, link=None, demand=None, commitment=None, bundle=None):
        """The payer's side pays the fee to the Mint; the Mint remits to the Steward, less what a cheat keeps."""
        if fee_due <= 0:
            return Fraction(0)
        run = self.run
        run.pay(from_acct, self.account, fee_due, self.unit, w, reason, kind="fee", link=link, demand=demand,
                commitment=commitment, bundle=bundle)
        kept = fee_due * self.keep_fraction(w)
        remitted = fee_due - kept
        if remitted > 0:
            run.pay(self.account, run.stewards[self.domain].account, remitted, self.unit, w,
                    "fee remitted to the Steward", kind="remit", link=link, demand=demand, commitment=commitment, bundle=bundle)
            run.stewards[self.domain].receive(remitted)
        self.collected += fee_due
        self.remitted += remitted
        self.kept += kept
        return remitted

    def publish(self, settlement):
        self.records.append(settlement)
        for d in settlement.mirror_domains:
            if d != self.domain:
                self.run.mints[d].mirrors.append(settlement)

    def check(self, other, w):
        """Audit the other Mint's publications since the last reconciliation (A.5: reconciliation is a check)."""
        since = self.last_reconciled.get(other.domain, -1)
        due = Fraction(0)
        remitted = Fraction(0)
        for r in other.records:
            if since < r.window <= w:
                due += r.fee_due
                remitted += r.fee_remitted
        self.last_reconciled[other.domain] = w
        self.reconciliations.append((w, other.domain))
        shortfall = due - remitted
        if shortfall > 0:
            self.exclude(other, w, shortfall, since)
        return shortfall

    def exclude(self, other, w, shortfall, since):
        run = self.run
        run.excluded[(self.domain, other.domain)] = (w, shortfall)
        self.excluded.add(other.domain)
        other.excluded_by.add(self.domain)
        run.log(w, "Mint %s finds that Mint %s remitted %s less than %s of its fees since window %d; it publishes the proof, stops reconciling with it and excludes it" % (
            self.account, other.account, fmt(shortfall, other.unit, run.symbol(other.unit)), pct(run.params["f"]), since + 1))
        for node in sorted(run.nodes.values(), key=lambda n: n.id):
            if self.domain in node.bonds and other.domain in node.bonds:
                if run.params["bridge_after_exclusion"] == "stop":
                    for link in run.links.values():
                        if link.owner == node.id and link.domain == other.domain and link.up:
                            link.up = False
                            run.log(w, "bridge %s, bonded with both, stops settling with %s: link %s declines windows" % (
                                node.name, other.account, link.id))
                else:
                    run.watch_bridges.setdefault(node.id, set()).add(self.domain)
                    run.log(w, "bridge %s, bonded with both, keeps settling with %s; its bond on %s is forfeit at the next window it settles there" % (
                        node.name, other.account, self.account))


def reconcile_all(run, w):
    ids = sorted(run.domains)
    for i, d1 in enumerate(ids):
        for d2 in ids[i + 1:]:
            m1, m2 = run.mints[d1], run.mints[d2]
            if d2 in m1.excluded or d1 in m2.excluded:
                continue
            if not routing.domains_connected(run, d1, d2, w):
                run.log(w, "no link between %s and %s this window: no reconciliation" % (d1, d2))
                run.missed_reconciliations.append((w, d1, d2))
                continue
            s1 = m1.check(m2, w)
            s2 = m2.check(m1, w)
            if s1 <= 0 and s2 <= 0:
                run.log(w, "Mints %s and %s reconcile: commitments agree, each deposits the other's" % (m1.account, m2.account))
