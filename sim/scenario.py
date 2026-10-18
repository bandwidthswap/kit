"""Scenario files: TOML or JSON, the map, the links, the actors, the prices, the traffic and the events.

    [scenario]      name, description, windows, window_seconds, report (phase | day | window),
                    detail_windows (windows to print row by row), per_gb (print a per-gigabyte view)
    [params]        f, m, steward_margin, mint_share, program_window, reconcile_every, first_link_size, first_link_premium,
                    bounty_deadline, min_bond_age, clock_base_s, drift_s_per_day, sees_ownership, max_wait,
                    bridge_after_exclusion, epoch
    [[domains]]     id, name, unit, symbol, mint
    [[places]]      id, name, domain, medium, range, mapped
    [[nodes]]       id, name, place, owner, roles, bonds, bond_since, clock_offset_s, rates ("USD>MRS" = 1.0)
    [[links]]       id, src, dst, owner, domain, price, carry, light_time_s, contact, clock_tolerance_s, window_kind,
                    medium, range, rate_mbps, loss, store_and_forward, passes_upstream, sig_scheme,
                    receipt_lifetime_windows, bidirectional
    [[demands]]     id, payer, src, dst, gb_per_window | gb_per_day | bytes_per_window, from, to, label,
                    schedule = [{from, to, gb_per_window | bytes}]
    [[commitments]] a payer's commitment posted at window `at` (default 0): poster, src, dst, via, min_gb_per_window,
                    premium, windows, deadline, proving_windows, proving_period, escrow (default windows × min × (posted + premium))
    [[events]]      at (or from/to), kind, note, and the kind's fields:
                    link_down/link_up (link), node_down/node_up (node), outage (link or node, from, to),
                    price (link, price), add_node (node), add_link (link), add_demand (demand), post (commitment),
                    count (node, bytes), clock (node, offset_s), under_remit (mint, from, to, keep), map_place (place)
    [steward]       opening_escrow = { domain = amount }: fee income the Steward holds from before the run
"""

import json
import os
from dataclasses import dataclass, field
from fractions import Fraction

from .model import Contact, Demand, Domain, Link, Node, Place
from .money import GB, money

DEFAULT_PARAMS = {
    "f": "0.05",                   # the fee fraction paid to the Steward (Section 3.3)
    "m": "0.025",                  # the match cap, strictly below f (Section 7)
    "steward_margin": "0.005",     # the Steward pays out on a payer's receipts at most (f - margin) of the fees that payer paid in
    "mint_share": "0",             # the Mint's share of each payment (Section 4 names one, with no number)
    "program_window": 24,          # the Steward's programs run at the end of every so many windows
    "reconcile_every": 24,         # Mints reconcile at the end of every so many windows, when a link exists
    "first_link_size": "0",        # the first-link program's size per place, in the domain's unit
    "first_link_premium": "0.05",  # and its premium per GB
    "bounty_deadline": 168,        # windows a Steward commitment stays open
    "min_bond_age": 24,            # a payer's bond must be this old, in windows, to count toward a match
    "clock_base_s": "5",           # B9's default
    "drift_s_per_day": "0.76",     # the lab's worst oscillator
    "sees_ownership": True,        # the rule checks who owns a key (5.1); Section 10 says it cannot
    "max_wait": 12,                # routing: a path whose next contact is further off than this is not taken
    "bridge_after_exclusion": "stop",  # a bridge bonded on both sides stops settling with an excluded Mint, or keeps
    "epoch": 0,                    # Unix time of window 0, for the receipts' timestamps
}
FRACTION_PARAMS = ("f", "m", "steward_margin", "mint_share", "first_link_size", "first_link_premium", "clock_base_s", "drift_s_per_day")


class ScenarioError(ValueError):
    pass


@dataclass
class Scenario:
    name: str
    description: str = ""
    windows: int = 24
    window_seconds: int = 3600
    report: str = "phase"
    detail_windows: list = field(default_factory=list)
    per_gb: bool = False
    params: dict = field(default_factory=dict)
    domains: dict = field(default_factory=dict)
    places: dict = field(default_factory=dict)
    nodes: dict = field(default_factory=dict)
    links: dict = field(default_factory=dict)
    link_groups: dict = field(default_factory=dict)   # base id -> [link ids] for bidirectional links
    demands: dict = field(default_factory=dict)
    commitments: list = field(default_factory=list)   # dicts, posted by the engine at their `at`
    events: list = field(default_factory=list)        # dicts with `at`
    opening_escrow: dict = field(default_factory=dict)
    source: str = ""

    @property
    def window_hours(self):
        return Fraction(self.window_seconds, 3600)

    def unit_of(self, domain_id):
        return self.domains[domain_id].unit

    def symbol_of(self, unit):
        for d in self.domains.values():
            if d.unit == unit:
                return d.symbol
        return ""

    def name_slug(self):
        base = os.path.splitext(os.path.basename(self.source))[0] if self.source else self.name
        return "".join(c if c.isalnum() or c in "-_" else "-" for c in base.lower())

    def event_windows(self):
        ws = set()
        for e in self.events:
            ws.add(e["at"])
        for c in self.commitments:
            ws.add(c.get("at", 0))
        return sorted(w for w in ws if 0 < w < self.windows)


# ---------------------------------------------------------------- reading

def find(path_or_name):
    """A path as given, else relative to the package, else a bare scenario name."""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [path_or_name,
             os.path.join(here, path_or_name),
             os.path.join(here, "scenarios", path_or_name),
             os.path.join(here, "scenarios", path_or_name + ".toml"),
             os.path.join(here, "scenarios", path_or_name + ".json")]
    for c in cands:
        if os.path.isfile(c):
            return c
    raise ScenarioError("no scenario file at %s" % path_or_name)


def load_raw(path_or_name):
    """The scenario file as a dict, for a program that wants to change it before running (the attacks do)."""
    path = find(path_or_name)
    with open(path, "rb") as fh:
        if path.endswith(".json"):
            return json.load(fh)
        import tomllib
        return tomllib.load(fh)


def load(path_or_name):
    path = find(path_or_name)
    with open(path, "rb") as fh:
        if path.endswith(".json"):
            raw = json.load(fh)
        else:
            try:
                import tomllib
            except ImportError as e:  # pragma: no cover
                raise ScenarioError("TOML scenarios need Python 3.11+; write the scenario as JSON instead") from e
            raw = tomllib.load(fh)
    scn = from_dict(raw, os.path.splitext(os.path.basename(path))[0])
    scn.source = path
    return scn


def _bytes_of(d, window_seconds, prefix=""):
    if prefix + "bytes_per_window" in d:
        return int(d[prefix + "bytes_per_window"])
    if prefix + "bytes" in d:
        return int(d[prefix + "bytes"])
    if prefix + "gb_per_window" in d:
        return money(d[prefix + "gb_per_window"]) * GB
    if prefix + "gb_per_day" in d:
        per_day = Fraction(86400, window_seconds)
        return money(d[prefix + "gb_per_day"]) * GB / per_day
    return 0


def _node(d):
    rates = {}
    for k, v in (d.get("rates") or {}).items():
        a, b = [s.strip() for s in k.split(">")]
        rates[(a, b)] = money(v)
    return Node(id=d["id"], name=d.get("name", d["id"]), place=d.get("place"), owner=d.get("owner"),
                roles=tuple(d.get("roles", ["relay"])), bonds=d.get("bonds", {}),
                bond_since=int(d.get("bond_since", -10 ** 6)), clock_offset_s=money(d.get("clock_offset_s", 0)),
                rates=rates, up=bool(d.get("up", True)), since=int(d.get("since", 0)))


def _links(d, scn):
    """One or two Link objects from a link table (two when bidirectional)."""
    def one(lid, src, dst, owner):
        domain = d.get("domain")
        if domain is None:
            node = scn.nodes.get(src)
            if node is None or node.place is None or node.place not in scn.places:
                raise ScenarioError("link %s: give it a domain, its source %s has no mapped place" % (lid, src))
            domain = scn.places[node.place].domain
        if domain not in scn.domains:
            raise ScenarioError("link %s: unknown domain %s" % (lid, domain))
        contact = d.get("contact") or {}
        return Link(id=lid, src=src, dst=dst, owner=owner, domain=domain,
                    price=money(d.get("price", 0)), carry=money(d.get("carry", 0)),
                    light_time_s=money(d.get("light_time_s", 0)),
                    contact=Contact(int(contact.get("every", 1)), int(contact.get("duration", 1)), int(contact.get("offset", 0))),
                    clock_tolerance_s=(money(d["clock_tolerance_s"]) if "clock_tolerance_s" in d else None),
                    window_kind=d.get("window_kind", "contact" if contact else "hour"),
                    medium=d.get("medium", "wire"), range_class=d.get("range", d.get("range_class", "point-to-point")),
                    rate_mbps=(money(d["rate_mbps"]) if "rate_mbps" in d else None), loss=money(d.get("loss", 0)),
                    store_and_forward=bool(d.get("store_and_forward", False)),
                    passes_upstream=bool(d.get("passes_upstream", True)),
                    sig_scheme=d.get("sig_scheme", "ed25519"),
                    receipt_lifetime_windows=d.get("receipt_lifetime_windows"),
                    up=bool(d.get("up", True)), since=int(d.get("since", 0)))
    out = [one(d["id"], d["src"], d["dst"], d.get("owner", d["src"]))]
    if d.get("bidirectional"):
        out.append(one(d["id"] + "~", d["dst"], d["src"], d.get("owner_back", d["dst"])))
    return out


def _demand(d, window_seconds):
    sched = []
    for s in d.get("schedule", []):
        sched.append((int(s["from"]), int(s.get("to", s["from"])), int(_bytes_of(s, window_seconds))))
    return Demand(id=d["id"], payer=d.get("payer", d["src"]), src=d["src"], dst=d["dst"],
                  bytes_per_window=_bytes_of(d, window_seconds), start=int(d.get("from", 0)),
                  end=(int(d["to"]) if "to" in d else None), schedule=sched, label=d.get("label", ""))


def from_dict(raw, name="scenario"):
    s = raw.get("scenario", {})
    scn = Scenario(name=s.get("name", name), description=s.get("description", ""),
                   windows=int(s.get("windows", 24)), window_seconds=int(s.get("window_seconds", 3600)),
                   report=s.get("report", "phase"), detail_windows=list(s.get("detail_windows", [])),
                   per_gb=bool(s.get("per_gb", False)))
    params = dict(DEFAULT_PARAMS)
    params.update(raw.get("params", {}))
    for k in FRACTION_PARAMS:
        params[k] = money(params[k])
    for k in ("program_window", "reconcile_every", "bounty_deadline", "min_bond_age", "max_wait", "epoch"):
        params[k] = int(params[k])
    if not (0 <= params["f"] < 1):
        raise ScenarioError("f must be in [0, 1)")
    if params["m"] < 0:
        raise ScenarioError("m must not be negative")
    scn.params = params
    for d in raw.get("domains", []):
        scn.domains[d["id"]] = Domain(id=d["id"], name=d.get("name", d["id"]), unit=d.get("unit", "USD"),
                                      symbol=d.get("symbol", "$" if d.get("unit", "USD") == "USD" else ""),
                                      mint=d.get("mint"))
    if not scn.domains:
        raise ScenarioError("a scenario needs at least one settlement domain")
    for p in raw.get("places", []):
        if p.get("domain") not in scn.domains:
            raise ScenarioError("place %s: unknown domain %s" % (p["id"], p.get("domain")))
        scn.places[p["id"]] = Place(id=p["id"], name=p.get("name", p["id"]), domain=p["domain"],
                                    medium=p.get("medium", "radio"), range_class=p.get("range", p.get("range_class", "point")),
                                    mapped=bool(p.get("mapped", True)), since=int(p.get("since", 0)))
    for n in raw.get("nodes", []):
        node = _node(n)
        if node.place is not None and node.place not in scn.places:
            raise ScenarioError("node %s: unknown place %s" % (node.id, node.place))
        scn.nodes[node.id] = node
    for dom in scn.domains.values():
        if dom.mint is not None and dom.mint not in scn.nodes:
            raise ScenarioError("domain %s: its Mint %s is not a node" % (dom.id, dom.mint))
    for l in raw.get("links", []):
        for link in _links(l, scn):
            for end in (link.src, link.dst):
                if end not in scn.nodes:
                    raise ScenarioError("link %s: unknown node %s" % (link.id, end))
            if link.owner not in scn.nodes:
                raise ScenarioError("link %s: unknown owner %s" % (link.id, link.owner))
            scn.links[link.id] = link
            scn.link_groups.setdefault(l["id"], []).append(link.id)
    for d in raw.get("demands", []):
        dem = _demand(d, scn.window_seconds)
        for nid in (dem.payer, dem.src, dem.dst):
            if nid not in scn.nodes:
                raise ScenarioError("demand %s: unknown node %s" % (dem.id, nid))
        scn.demands[dem.id] = dem
    for c in raw.get("commitments", []):
        c = dict(c)
        c.setdefault("at", 0)
        scn.commitments.append(c)
    for e in raw.get("events", []):
        scn.events.extend(_expand_event(e))
    scn.events.sort(key=lambda e: e["at"])
    st = raw.get("steward", {})
    for dom, amt in (st.get("opening_escrow") or {}).items():
        if dom not in scn.domains:
            raise ScenarioError("steward.opening_escrow: unknown domain %s" % dom)
        scn.opening_escrow[dom] = money(amt)
    return scn


def _expand_event(e):
    e = dict(e)
    kind = e.get("kind")
    if kind is None:
        raise ScenarioError("an event needs a kind")
    if kind == "outage":
        a, b = int(e["from"]), int(e["to"])
        target = "link" if "link" in e else "node"
        down = dict(e, kind=target + "_down", at=a)
        up = dict(e, kind=target + "_up", at=b + 1, note=e.get("note_up", (e.get("note", "") + " ends").strip()))
        for k in ("from", "to", "note_up"):
            down.pop(k, None)
            up.pop(k, None)
        return [down, up]
    if kind == "under_remit":
        e["at"] = int(e["from"])
        return [e]
    if "at" not in e:
        raise ScenarioError("event %s needs `at`" % kind)
    e["at"] = int(e["at"])
    return [e]


def node_from_dict(d):
    return _node(d)


def links_from_dict(d, scn):
    return _links(d, scn)


def demand_from_dict(d, scn):
    return _demand(d, scn.window_seconds)
