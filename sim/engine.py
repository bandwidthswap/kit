"""The run: a clock in windows, bundles moving hop by hop, every hop settled when the next takes custody.

For each window: apply the events due; make a bundle for each demand's bytes
and route it; advance every bundle in flight as far as open links allow,
settling each hop it crosses (Section 3.6, the smaller count) and paying for
it (the fee to the Mint, the Mint's remittance to the Steward, the relay's
net; a bridge's conversion billed back toward the payer; a commitment's
premium, divided by opportunity cost; a proving reimbursement); then refund
commitments past their deadline, run the Steward's programs at the end of a
program window, and reconcile Mints at the end of a reconciliation window.
Every payment is one row: from, to, amount, unit, window, reason.
"""

import copy
from fractions import Fraction

from . import routing
from .mints import Mint, reconcile_all
from .model import Commitment, Payment, Settlement
from .money import GB, fmt, gb, money, pct
from .receipts import make_receipt, merkle_root, settle_counts, sign
from .scenario import demand_from_dict, links_from_dict, node_from_dict
from .steward import Steward


class Bundle:
    __slots__ = ("demand", "payer", "path", "chain", "shares", "hop", "nbytes", "payer_count", "arrived",
                 "dispatched", "own", "bounties", "proving", "prev_sig", "holding", "paid_posted", "key")

    def __init__(self, demand, payer, path, chain, shares, nbytes, w, own, bounties, proving):
        self.demand = demand
        self.payer = payer
        self.path = path
        self.chain = chain
        self.shares = shares
        self.hop = 0
        self.nbytes = nbytes
        self.payer_count = nbytes
        self.arrived = w
        self.dispatched = w
        self.own = own
        self.bounties = bounties
        self.proving = proving
        self.prev_sig = ""
        self.holding = 0
        self.paid_posted = Fraction(0)
        self.key = "%s@%d" % (demand, w)


class Run:
    """Everything a run of a scenario produces. `Run(scenario).go()`."""

    def __init__(self, scenario):
        self.scn = scenario
        s = copy.deepcopy(scenario)
        self.name = s.name
        self.params = s.params
        self.windows = s.windows
        self.window_seconds = s.window_seconds
        self.window_hours = Fraction(s.window_seconds, 3600)
        self.domains = s.domains
        self.places = s.places
        self.nodes = s.nodes
        self.links = s.links
        self.link_groups = s.link_groups
        self.demands = s.demands
        self.pending_events = sorted(s.events, key=lambda e: e["at"])
        self.pending_commitments = sorted(s.commitments, key=lambda c: c["at"])
        self.payments = []
        self.settlements = []
        self.declined = []           # (window, demand, link, diff_s, tolerance_s)
        self.unserved = []           # (window, demand, bytes, why)
        self.delivered = []          # (window, demand, bytes, path ids, dispatched)
        self.events_log = []         # (window, text)
        self.commitments = {}
        self.stewards = {d: Steward(self, d) for d in self.domains}
        self.mints = {d: Mint(self, d) for d in self.domains}
        self.bundles = []
        self.count_overrides = {}
        self.link_stats = {}
        self.place_linked = set()
        self.excluded = {}
        self.missed_reconciliations = []
        self.watch_bridges = {}
        self.slashed = []            # (window, node, domain, bond)
        self.last_contact = {}
        self.in_custody_at_end = []
        self.max_holding = 0
        self._cid = 0
        self.finished = False

    # ------------------------------------------------------------ helpers

    def unit_of(self, domain):
        return self.domains[domain].unit

    def symbol(self, unit):
        for d in self.domains.values():
            if d.unit == unit:
                return d.symbol
        return ""

    def log(self, w, text):
        self.events_log.append((w, text))

    def new_id(self, prefix):
        self._cid += 1
        return "%s-%d" % (prefix, self._cid)

    def pay(self, from_, to, amount, unit, w, reason, kind="payment", nbytes=0, link=None, demand=None, commitment=None,
            bundle=None):
        if amount == 0:
            return
        self.payments.append(Payment(from_, to, amount, unit, w, reason, kind, nbytes, link, demand, commitment, bundle))

    def link_ids(self, ref):
        if ref in self.link_groups:
            return list(self.link_groups[ref])
        if ref in self.links:
            return [ref]
        raise KeyError("no link %s" % ref)

    def name_of(self, acct):
        if acct in self.nodes:
            return self.nodes[acct].name
        if acct.startswith("steward:"):
            return "Steward of %s" % self.domains[acct[8:]].name
        if acct.startswith("escrow:"):
            cm = self.commitments.get(acct[7:])
            return "escrow: %s" % (cm.label or cm.id) if cm else acct
        if acct.startswith("mint:"):
            return self.domains[acct[5:]].name
        return acct

    # ------------------------------------------------------------ the loop

    def go(self):
        for w in range(self.windows):
            self.step(w)
        self.finish()
        return self

    run = go

    def step(self, w):
        self.apply_events(w)
        for dem in sorted(self.demands.values(), key=lambda d: d.id):
            n = dem.bytes_at(w)
            if n > 0 and routing.node_up(self, dem.src, w):
                self.dispatch(dem, n, w)
            elif n > 0:
                self.unserved.append((w, dem.id, n, "%s is down" % dem.src))
        self.advance(w)
        self.commitment_deadlines(w)
        if (w + 1) % self.params["program_window"] == 0:
            for d in sorted(self.stewards):
                self.stewards[d].run_programs(w)
            self.link_stats = {}
        if (w + 1) % self.params["reconcile_every"] == 0 and len(self.domains) > 1:
            reconcile_all(self, w)

    def finish(self):
        for b in self.bundles:
            self.in_custody_at_end.append((b.demand, b.path[b.hop].src if b.hop < len(b.path) else b.path[-1].dst,
                                           b.nbytes, b.arrived))
        self.finished = True

    # ------------------------------------------------------------ events

    def apply_events(self, w):
        while self.pending_events and self.pending_events[0]["at"] <= w:
            self.apply_event(self.pending_events.pop(0), w)
        while self.pending_commitments and self.pending_commitments[0]["at"] <= w:
            self.post_payer_commitment(self.pending_commitments.pop(0), w)

    def apply_event(self, e, w):
        kind = e["kind"]
        note = e.get("note", "")
        if kind in ("link_down", "link_up"):
            for lid in self.link_ids(e["link"]):
                self.links[lid].up = kind == "link_up"
            text = "%s %s" % (e["link"], "down" if kind == "link_down" else "up")
        elif kind in ("node_down", "node_up"):
            self.nodes[e["node"]].up = kind == "node_up"
            text = "%s %s" % (self.nodes[e["node"]].name, "dark" if kind == "node_down" else "back")
        elif kind == "price":
            for lid in self.link_ids(e["link"]):
                old = self.links[lid].price
                self.links[lid].price = money(e["price"])
            unit = self.unit_of(self.links[lid].domain)
            text = "%s posts %s/GB (was %s)" % (e["link"], fmt(money(e["price"]), unit, self.symbol(unit)), fmt(old, unit, self.symbol(unit)))
        elif kind == "add_node":
            node = node_from_dict(e["node"])
            node.since = w
            if node.bond_since == -10 ** 6 and e["node"].get("bond_since") is None:
                node.bond_since = w
            self.nodes[node.id] = node
            text = "new node %s at %s" % (node.name, node.place)
        elif kind == "add_link":
            ids = []
            for link in links_from_dict(e["link"], self):
                link.since = w
                self.links[link.id] = link
                self.link_groups.setdefault(e["link"]["id"], []).append(link.id)
                ids.append(link.id)
            text = "new link %s" % ", ".join(ids)
        elif kind == "add_demand":
            dem = demand_from_dict(e["demand"], self)
            dem.start = max(dem.start, w)
            self.demands[dem.id] = dem
            text = "new demand %s" % dem.id
        elif kind == "post":
            self.post_payer_commitment(e["commitment"], w)
            return
        elif kind == "count":
            self.count_overrides[(e["node"], int(e.get("window", w)))] = int(e["bytes"])
            text = "%s's counter reads %d bytes this window" % (self.nodes[e["node"]].name, int(e["bytes"]))
        elif kind == "clock":
            self.nodes[e["node"]].clock_offset_s = money(e["offset_s"])
            text = "%s's clock steps to %+g s" % (self.nodes[e["node"]].name, float(money(e["offset_s"])))
        elif kind == "under_remit":
            dom = e["mint"] if e["mint"] in self.domains else next(d for d, dd in self.domains.items() if dd.mint == e["mint"])
            self.mints[dom].under_remit(int(e["from"]), int(e["to"]), money(e.get("keep", 1)))
            text = "Mint %s keeps %s of the fee it owes its Steward through window %d" % (
                self.mints[dom].account, pct(money(e.get("keep", 1))), int(e["to"]))
        elif kind == "map_place":
            p = e["place"]
            if isinstance(p, str):
                self.places[p].mapped = True
                self.places[p].since = w
                text = "the map accepts %s" % self.places[p].name
            else:
                from .model import Place
                self.places[p["id"]] = Place(id=p["id"], name=p.get("name", p["id"]), domain=p["domain"],
                                             medium=p.get("medium", "radio"), range_class=p.get("range", "point"),
                                             mapped=True, since=w)
                text = "the map accepts %s" % self.places[p["id"]].name
        else:
            raise ValueError("unknown event kind %s" % kind)
        self.log(w, (note + ": " if note else "") + text)

    # ------------------------------------------------------------ commitments

    def post_payer_commitment(self, c, w):
        poster = c["poster"]
        domain = c.get("domain") or routing.domain_of_node(self, poster)
        unit = self.unit_of(domain)
        if "min_bytes_per_window" in c:
            min_bytes = int(c["min_bytes_per_window"])
        else:
            min_bytes = int(money(c.get("min_gb_per_window", 0)) * GB)
        premium = money(c.get("premium", 0))
        windows = int(c["windows"])
        if "deadline" in c:
            deadline = int(c["deadline"])
        else:
            deadline = w + int(c.get("deadline_in", 2 * windows))
        if "escrow" in c:
            escrow = money(c["escrow"])
        else:
            posted = money(c.get("posted", 0))
            escrow = windows * Fraction(min_bytes, GB) * (posted + premium)
        cid = c.get("id") or self.new_id("commitment")
        cm = Commitment(id=cid, kind="payer", poster=poster, domain=domain, unit=unit, min_bytes=min_bytes,
                        premium=premium, windows=windows, deadline=deadline, posted_at=w, escrow=escrow,
                        contributions={poster: escrow}, pool=escrow, src=c.get("src"), dst=c.get("dst"),
                        via=c.get("via"), proving_windows=int(c.get("proving_windows", 0)),
                        proving_period=int(c.get("proving_period", 168)), label=c.get("label", cid))
        self.commitments[cid] = cm
        self.pay(poster, "escrow:" + cid, escrow, unit, w, "%s escrows its commitment with %s" % (
            self.nodes[poster].name, self.mints[domain].account), kind="escrow", commitment=cid)
        self.log(w, "%s posts %s: route %s to %s%s, at least %s a window, premium %s/GB, %d windows, deadline window %d%s, %s escrowed" % (
            self.nodes[poster].name, cm.label, cm.src, cm.dst, (" via %s" % cm.via) if cm.via else "",
            self._gb(min_bytes), fmt(premium, unit, self.symbol(unit)), windows, deadline,
            (", proving allowance %d windows per %d" % (cm.proving_windows, cm.proving_period)) if cm.proving_windows else "",
            fmt(escrow, unit, self.symbol(unit))))
        self.stewards[domain].match(cm, w)
        return cm

    @staticmethod
    def _gb(n):
        from .money import fmt_gb
        return fmt_gb(n)

    def route_matches(self, cm, path):
        first, last = path[0].src, path[-1].dst

        def at(x, nid):
            return x is None or x == nid or self.nodes[nid].place == x
        on = {l.src for l in path} | {l.dst for l in path} | {l.owner for l in path}
        return at(cm.src, first) and at(cm.dst, last) and (cm.via is None or cm.via in on)

    def bounty_matches(self, cm, path, w):
        pl = lambda nid: routing.place_of(self, nid)
        if cm.kind == "second_bridge":
            if any(l.id == cm.avoid_link for l in path):
                return False
            seq = [pl(path[0].src)] + [pl(l.dst) for l in path]
            starts = [i for i, p in enumerate(seq) if p in cm.from_places]
            if not starts:
                return False
            if not any(p in cm.to_places for p in seq[starts[0] + 1:]):
                return False
            for p in routing.intermediate_places(self, path):
                place = self.places[p]
                if place.mapped and place.since <= w and p not in cm.existing_places:
                    return True
            return False
        if cm.kind == "first_link":
            return any(pl(l.src) == cm.to_place or pl(l.dst) == cm.to_place for l in path)
        return False

    def eligible(self, payer, cm, path):
        """A payer other than the poster, bonded before the commitment, not owned by a relay on the path (5.1)."""
        if payer == cm.poster:
            return False
        node = self.nodes[payer]
        if node.bond_since >= cm.posted_at:
            return False
        if self.params["sees_ownership"]:
            owners = {self.nodes[l.owner].owner for l in path if l.owner != payer}
            if node.owner in owners:
                return False
        return True

    def own_commitment_for(self, dem, path):
        for cm in sorted(self.commitments.values(), key=lambda c: c.posted_at):
            if cm.kind == "payer" and cm.open and cm.poster == dem.payer and cm.pool > 0 and self.route_matches(cm, path):
                return cm
        return None

    def bounties_for(self, dem, path, w):
        out = []
        for cm in sorted(self.commitments.values(), key=lambda c: (c.posted_at, c.id)):
            if cm.kind in ("second_bridge", "first_link") and cm.open and cm.pool > 0 and cm.posted_at <= w:
                if self.bounty_matches(cm, path, w) and self.eligible(dem.payer, cm, path):
                    out.append(cm)
        return out

    def proving_for(self, dem, path):
        out = []
        for cm in sorted(self.commitments.values(), key=lambda c: (c.posted_at, c.id)):
            if cm.kind == "payer" and cm.open and cm.pool > 0 and cm.proving_windows > 0 and cm.poster != dem.payer:
                if self.route_matches(cm, path) and self.eligible(dem.payer, cm, path):
                    out.append(cm)
        return out

    def commitment_deadlines(self, w):
        for cm in sorted(self.commitments.values(), key=lambda c: c.id):
            if not cm.open:
                continue
            if cm.pool <= 0:
                cm.status = "exhausted"
                self.log(w, "%s is exhausted: %d windows served of %d" % (cm.label, len(cm.served), cm.windows))
                continue
            done = cm.windows and len(cm.served) >= cm.windows
            if done or w >= cm.deadline:
                cm.status = "complete" if done else "expired"
                self.log(w, "%s %s: %d windows served of %d, %s returned" % (
                    cm.label, "is complete" if done else "reaches its deadline", len(cm.served), cm.windows,
                    fmt(cm.pool, cm.unit, self.symbol(cm.unit))))
                self.refund_pool(cm, w)

    def refund_pool(self, cm, w):
        total = cm.pool
        if total <= 0:
            return
        whole = sum(cm.contributions.values())
        for acct, c in sorted(cm.contributions.items()):
            share = total * c / whole
            if share <= 0:
                continue
            if acct.startswith("steward:"):
                self.stewards[acct[8:]].refund(share)
                self.pay("escrow:" + cm.id, acct, share, cm.unit, w, "unspent escrow back to the Steward", kind="refund", commitment=cm.id)
            else:
                self.pay("escrow:" + cm.id, acct, share, cm.unit, w, "unspent escrow returned to the poster", kind="refund", commitment=cm.id)
        cm.refunded += total
        cm.pool = Fraction(0)

    # ------------------------------------------------------------ bundles

    def dispatch(self, dem, nbytes, w):
        path = routing.best_path(self, dem.src, dem.dst, dem.payer, w)
        if path is None:
            self.unserved.append((w, dem.id, nbytes, "no route"))
            return None
        chain = routing.payment_chain(self, path, dem.payer)
        shares = routing.opportunity_shares(self, path, dem.payer, w)
        own = self.own_commitment_for(dem, path)
        bounties = self.bounties_for(dem, path, w)
        proving = self.proving_for(dem, path)
        b = Bundle(dem.id, dem.payer, path, chain, shares, nbytes, w, own, bounties, proving)
        self.bundles.append(b)
        return b

    def advance(self, w):
        still = []
        for b in self.bundles:
            if not self.move(b, w):
                still.append(b)
        self.bundles = still

    def move(self, b, w):
        """Carry a bundle as far as it goes this window. True when it is delivered or dropped."""
        while b.hop < len(b.path):
            link = b.path[b.hop]
            if not routing.link_open(self, link, w):
                return False
            if link.store_and_forward and b.hop > 0 and b.arrived == w:
                return False
            if not self.forward(b, link, w):
                return True
            b.hop += 1
        self.deliver(b, w)
        return True

    def tolerance(self, link, w, light_to_payer=None):
        """A.3: the link's base tolerance, plus the light time both ways between the payer and the hop,
        plus the drift expected since the link's last contact."""
        base = link.clock_tolerance_s if link.clock_tolerance_s is not None else self.params["clock_base_s"]
        last = self.last_contact.get(link.id)
        days = Fraction(0) if last is None else (w - last) * self.window_hours / 24
        light = link.light_time_s if light_to_payer is None else light_to_payer
        return base + 2 * light + self.params["drift_s_per_day"] * days

    @staticmethod
    def light_to_payer(b, hop_i):
        """Light time from the payer's end of the path to the hop that owns link hop_i."""
        path = b.path
        if path[-1].dst == b.payer and path[0].src != b.payer:
            return sum((l.light_time_s for l in path[hop_i:]), Fraction(0))
        return sum((l.light_time_s for l in path[:hop_i]), Fraction(0))

    def forward(self, b, link, w):
        """Cross one link: the receipt, the countersignature, the settlement and the money. False when declined."""
        payer, owner = b.payer, link.owner
        hop_i = b.hop
        holding = w - b.arrived
        self.max_holding = max(self.max_holding, holding)
        cap = link.cap_bytes(self.window_seconds)
        forwarded = b.nbytes if cap is None else min(b.nbytes, cap)
        if link.loss:
            forwarded = int(forwarded * (1 - link.loss))
        payer_count = self.count_overrides.get((payer, w), b.payer_count)
        hop_count = self.count_overrides.get((owner, w), forwarded)
        tol = self.tolerance(link, w, self.light_to_payer(b, hop_i))
        self.last_contact[link.id] = w
        diff = abs(self.nodes[payer].clock_offset_s - self.nodes[owner].clock_offset_s)
        unit = self.unit_of(link.domain)
        if owner != payer and diff > tol:
            self.declined.append((w, b.demand, link.id, diff, tol))
            self.log(w, "%s declines %s's receipt on %s: clocks differ by %g s, tolerance %g s; the window is lost" % (
                self.nodes[owner].name, self.nodes[payer].name, link.id, float(diff), float(tol)))
            self.settlements.append(Settlement(w, link.id, link.domain, payer, owner, hop_i + 1, payer_count, 0, 0,
                                               "declined", Fraction(0), Fraction(0), Fraction(0), Fraction(0), "", "",
                                               b.demand, payer, holding, tol))
            return False
        settled, status = settle_counts(payer_count, hop_count)
        t_first = self.params["epoch"] + w * self.window_seconds
        t_last = t_first + self.window_seconds - 1
        root = merkle_root(payer, w, payer_count)
        receipt = make_receipt(payer, owner, hop_i + 1, root, payer_count, t_first, t_last, b.prev_sig)
        rsig = sign(payer, receipt)
        ack = make_receipt(payer, owner, hop_i + 1, root, hop_count, t_first, t_last, rsig)
        asig = sign(owner, ack)
        b.prev_sig = asig

        g = gb(settled)
        hours = holding * self.window_hours
        amount = g * link.price
        carry = link.carry * g * hours
        gross = amount + carry
        info = b.chain[hop_i]
        por = info["payer_of_record"]
        f = self.params["f"]
        fee_due = f * gross
        fee_remitted = Fraction(0)
        cm_used = None
        if owner != payer and gross > 0:
            counts = "" if payer_count == hop_count else " [payer signed %s, hop %s bytes: %s at the smaller]" % (
                format(payer_count, ","), format(hop_count, ","), status)
            what = "%s carried %s on %s at %s/GB%s%s" % (
                self.nodes[owner].name, self._gb(settled), link.id, fmt(link.price, unit, self.symbol(unit)),
                (" + carry %s for %d window%s held" % (fmt(carry, unit, self.symbol(unit)), holding, "" if holding == 1 else "s")) if carry else "",
                counts)
            from_pool = Fraction(0)
            cm = b.own
            if cm is not None and cm.open and cm.pool > 0 and por == payer:
                from_pool = min(gross, cm.pool)
                cm.pool -= from_pool
                cm.paid_out += from_pool
                cm.served.add(w)
                cm_used = cm.id
                fee_remitted += self.pay_gross("escrow:" + cm.id, owner, from_pool, link, w,
                                               what + " (posted price from the commitment's escrow)", b, cm.id, "commitment", settled)
                if from_pool < gross:
                    cm.status = "exhausted"
                    self.log(w, "%s is exhausted mid-window; %s pays the rest of the posted price itself" % (cm.label, self.nodes[payer].name))
            rest = gross - from_pool
            if rest > 0:
                fee_remitted += self.pay_gross(por, owner, rest, link, w, what, b, None, "payment", settled)
                if por == payer:
                    b.paid_posted += rest
            # a bridge bills what it paid on the far side back toward the payer (A.5)
            billed = gross
            for bill in reversed(info["bills"]):
                if not bill["passes"]:
                    break
                converted = billed * bill["rate"]
                self.pay(bill["from_"], bill["to"], converted, bill["from_unit"], w,
                         "%s converts %s for the %s hop at %s %s per %s" % (
                             self.nodes[bill["to"]].name, fmt(billed, bill["to_unit"], self.symbol(bill["to_unit"])),
                             link.id, fmt(bill["rate"], "", ""), bill["from_unit"], bill["to_unit"]),
                         kind="convert", nbytes=settled, link=link.id, demand=b.demand, bundle=b.key)
                if bill["from_"] == payer:
                    b.paid_posted += converted
                billed = converted
            if por == payer:
                self.reimburse(b, rest if from_pool == 0 else Fraction(0), w, link)
        if owner != payer and settled > 0:
            self.pay_premiums(b, link, settled, w, owner)
            # the Steward's statistics for this program window
            st = self.link_stats.setdefault(link.id, {"fees": Fraction(0), "bytes": 0, "places": set(), "dest_places": set(),
                                                      "origin_places": set(), "origins": set(), "dests": set(), "payers": set()})
            st["fees"] += gross
            st["bytes"] += settled
            st["places"] |= routing.places_on(self, b.path)
            dp = routing.place_of(self, b.path[-1].dst)
            if dp is not None:
                st["dest_places"].add(dp)
            op = routing.place_of(self, b.path[0].src)
            if op is not None:
                st["origin_places"].add(op)
            st["origins"].add(b.path[0].src)
            st["dests"].add(b.path[-1].dst)
            st["payers"].add(payer)
            # slashing a bridge that kept settling with an excluded Mint
            for honest in sorted(self.watch_bridges.get(owner, ())):
                if link.domain in self.mints[honest].excluded:
                    node = self.nodes[owner]
                    bond = node.bonds.get(honest, Fraction(0))
                    if bond > 0:
                        node.bonds[honest] = Fraction(0)
                        self.pay(owner, self.mints[honest].account, bond, self.unit_of(honest), w,
                                 "bond forfeit: kept settling with the excluded Mint %s" % self.mints[link.domain].account, kind="slash")
                        self.slashed.append((w, owner, honest, bond))
                        self.log(w, "%s forfeits its %s bond of %s for settling on %s with the excluded %s" % (
                            node.name, honest, fmt(bond, self.unit_of(honest), self.symbol(self.unit_of(honest))), link.id,
                            self.mints[link.domain].account))
        for nid in (link.src, link.dst):
            p = routing.place_of(self, nid)
            if p is not None and settled > 0:
                self.place_linked.add(p)
        mirrors = tuple(sorted(d for d in self.nodes[owner].bonds if d != link.domain and d in self.domains))
        s = Settlement(w, link.id, link.domain, payer, owner, hop_i + 1, payer_count, hop_count, settled, status,
                       gross, carry, fee_due if owner != payer else Fraction(0), fee_remitted, rsig, asig, b.demand,
                       por, holding, tol, cm_used, mirrors)
        self.settlements.append(s)
        self.mints[link.domain].publish(s)
        b.nbytes = forwarded
        b.arrived = w
        b.holding += holding
        return True

    def pay_gross(self, from_acct, owner, gross, link, w, what, b, commitment, kind, settled=0):
        """Pay a relay: the fee to the Mint (remitted to the Steward), the Mint's share, the rest to the relay."""
        unit = self.unit_of(link.domain)
        f = self.params["f"]
        fee = f * gross
        share = self.params["mint_share"] * gross
        net = gross - fee - share
        self.pay(from_acct, owner, net, unit, w, what + (" less %s fee" % pct(f) if fee else ""), kind=kind,
                 nbytes=settled, link=link.id, demand=b.demand, commitment=commitment, bundle=b.key)
        remitted = self.mints[link.domain].collect_fee(from_acct, fee, w, "fee of %s on %s" % (pct(f), link.id),
                                                       link=link.id, demand=b.demand, commitment=commitment, bundle=b.key)
        if share > 0:
            self.pay(from_acct, self.mints[link.domain].account, share, unit, w, "the Mint's share", kind="mint_share",
                     link=link.id, demand=b.demand, commitment=commitment, bundle=b.key)
        return remitted

    def pay_premiums(self, b, link, settled, w, owner):
        """A commitment's premium on this hop, the owner's share by opportunity cost, from the pool, less the fee."""
        share = b.shares.get(owner, Fraction(0))
        if share <= 0 or settled <= 0:
            return
        n_hops = sum(1 for l in b.path if l.owner == owner)
        part = share / n_hops
        g = gb(settled)
        unit = self.unit_of(link.domain)
        todo = []
        if b.own is not None and b.own.open and b.own.pool > 0 and b.own.premium > 0:
            todo.append((b.own, "premium"))
        for cm in b.bounties:
            if cm.open and cm.pool > 0:
                todo.append((cm, "bounty"))
        for cm, kind in todo:
            if cm.unit != unit:
                continue
            prem = min(cm.premium * g * part, cm.pool)
            if prem <= 0:
                continue
            cm.pool -= prem
            cm.paid_out += prem
            cm.served.add(w)
            label = "premium %s/GB on %s, %s's share %s" % (fmt(cm.premium, unit, self.symbol(unit)), cm.label,
                                                            self.nodes[owner].name, pct(part))
            self.pay_gross("escrow:" + cm.id, owner, prem, link, w, label, b, cm.id, kind, settled)
            if cm.pool <= 0:
                cm.status = "exhausted"
                self.log(w, "%s is spent: %d windows served of %d" % (cm.label, len(cm.served), cm.windows))

    def reimburse(self, b, paid, w, link):
        """A proving allowance: the escrow gives a tester its posted price back (Section 5.1)."""
        if paid <= 0:
            return
        for cm in b.proving:
            if not (cm.open and cm.pool > 0):
                continue
            period = (w - cm.posted_at) // cm.proving_period
            used = cm.proving_used.setdefault(period, set())
            if w not in used and len(used) >= cm.proving_windows:
                continue
            amt = min(paid, cm.pool)
            used.add(w)
            cm.pool -= amt
            cm.paid_out += amt
            self.pay("escrow:" + cm.id, b.payer, amt, cm.unit, w, "proving allowance of %s: %s's posted price on %s reimbursed" % (
                cm.label, self.nodes[b.payer].name, link.id), kind="reimburse", nbytes=b.nbytes, link=link.id,
                demand=b.demand, commitment=cm.id, bundle=b.key)
            return

    def deliver(self, b, w):
        self.delivered.append((w, b.demand, b.nbytes, [l.id for l in b.path], b.dispatched, b.key))
        cm = b.own
        if cm is not None and cm.open and cm.min_bytes and b.nbytes < cm.min_bytes:
            back = min(cm.premium * gb(cm.min_bytes - b.nbytes), cm.pool)
            if back > 0:
                cm.pool -= back
                cm.refunded += back
                self.pay("escrow:" + cm.id, cm.poster, back, cm.unit, w,
                         "shortfall: %s delivered against a minimum of %s, the rest of the window's premium returns" % (
                             self._gb(b.nbytes), self._gb(cm.min_bytes)), kind="refund", nbytes=b.nbytes,
                         demand=b.demand, commitment=cm.id, bundle=b.key)

    # ------------------------------------------------------------ queries

    def rows(self, w0=0, w1=None):
        w1 = self.windows - 1 if w1 is None else w1
        return [p for p in self.payments if w0 <= p.window <= w1]

    def received(self, acct, w0=0, w1=None, unit=None, kinds=None):
        return sum((p.amount for p in self.rows(w0, w1)
                    if p.to == acct and (unit is None or p.unit == unit) and (kinds is None or p.kind in kinds)), Fraction(0))

    def paid(self, acct, w0=0, w1=None, unit=None, kinds=None):
        return sum((p.amount for p in self.rows(w0, w1)
                    if p.from_ == acct and (unit is None or p.unit == unit) and (kinds is None or p.kind in kinds)), Fraction(0))

    def net(self, acct, w0=0, w1=None, unit=None):
        return self.received(acct, w0, w1, unit) - self.paid(acct, w0, w1, unit)

    def owner_accounts(self, owner):
        return [n.id for n in self.nodes.values() if n.owner == owner]

    def net_by_owner(self, owner, w0=0, w1=None, unit=None):
        return sum((self.net(a, w0, w1, unit) for a in self.owner_accounts(owner)), Fraction(0))

    def gb_delivered(self, demand=None, w0=0, w1=None):
        w1 = self.windows - 1 if w1 is None else w1
        n = sum(nb for (w, d, nb, _, _, _) in self.delivered if w0 <= w <= w1 and (demand is None or d == demand))
        return gb(n)

    def bundle_rows(self, demand, w0=0, w1=None):
        """The rows of the bundles of a demand delivered in the period, wherever their windows fell."""
        w1 = self.windows - 1 if w1 is None else w1
        keys = {x[5] for x in self.delivered if x[1] == demand and w0 <= x[0] <= w1}
        return [p for p in self.payments if p.bundle in keys]

    def paid_per_gb(self, demand, payer, w0=0, w1=None, unit=None):
        g = self.gb_delivered(demand, w0, w1)
        if g == 0:
            return None
        paid = sum((p.amount for p in self.bundle_rows(demand, w0, w1)
                    if p.from_ == payer and (unit is None or p.unit == unit) and p.kind in ("payment", "fee", "convert", "mint_share")),
                   Fraction(0))
        return paid / g

    def kept_per_gb(self, demand, acct, w0=0, w1=None, unit=None):
        g = self.gb_delivered(demand, w0, w1)
        if g == 0:
            return None
        rows = [p for p in self.bundle_rows(demand, w0, w1) if p.kind != "convert" and (unit is None or p.unit == unit)]
        return (sum((p.amount for p in rows if p.to == acct), Fraction(0)) - sum((p.amount for p in rows if p.from_ == acct), Fraction(0))) / g

    def gb_settled_on(self, link, w0=0, w1=None):
        w1 = self.windows - 1 if w1 is None else w1
        return gb(sum(s.settled_bytes for s in self.settlements if s.link == link and w0 <= s.window <= w1))

    def steward_take(self, domain, w0=0, w1=None):
        return self.received("steward:" + domain, w0, w1, kinds=("remit",))


def simulate(scenario):
    return Run(scenario).go()
