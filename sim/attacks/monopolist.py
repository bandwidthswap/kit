"""A monopolist on a sole bridge stands up a second key on the same hill (Section 7, program 2; Section 10).

Two payers in a town reach the far side through one relay on a hill, at $1
a gigabyte. The Steward's rule sees a sole bridge carrying fees and posts
those fees as a bounty for a second path through a different mapped place.
The monopolist answers with a second key on the same hill, posting $0.90 to
pull its own customers over and collect the bounty. The map rule pays only a
path through a place not on the existing one: the second key earns the
lower price and none of the bounty. The same key on another mapped hill
would collect it, and would be a second bridge, which is the purpose (the
paper accepts that one party may own both).
"""

from fractions import Fraction

from ..money import fmt, pct
from . import run_dict

PROGRAM = 24
SWITCH = 48
WINDOWS = 96


def monopolist_scenario(second_key=None, price=Fraction(9, 10)):
    """second_key: None, 'same-hill' or 'other-hill'."""
    places = [
        {"id": "town", "name": "the town", "domain": "v", "medium": "wire", "range": "street"},
        {"id": "hill", "name": "the hill", "domain": "v", "medium": "radio", "range": "hill"},
        {"id": "far", "name": "the far side", "domain": "v", "medium": "fibre", "range": "world", "mapped": False},
    ]
    if second_key == "other-hill":
        places.append({"id": "hill2", "name": "the other hill", "domain": "v", "medium": "radio", "range": "hill", "since": SWITCH})
    nodes = [
        {"id": "p1", "name": "payer one", "place": "town", "roles": ["payer"], "bonds": {"v": 50}},
        {"id": "p2", "name": "payer two", "place": "town", "roles": ["payer"], "bonds": {"v": 50}},
        {"id": "mono", "name": "the monopolist's relay", "place": "hill", "owner": "Mono", "roles": ["relay"], "bonds": {"v": 100}},
        {"id": "far", "name": "the far side", "place": "far", "roles": ["endpoint"]},
        {"id": "mint", "name": "the valley's Mint", "place": "town", "roles": ["mint"]},
    ]
    links = [
        {"id": "a1", "src": "p1", "dst": "mono", "owner": "mono", "price": 0, "medium": "wire", "range": "street"},
        {"id": "a2", "src": "p2", "dst": "mono", "owner": "mono", "price": 0, "medium": "wire", "range": "street"},
        {"id": "bridge", "src": "mono", "dst": "far", "owner": "mono", "price": 1.00, "medium": "radio", "range": "hill"},
    ]
    events = []
    if second_key:
        place = "hill" if second_key == "same-hill" else "hill2"
        events += [
            {"at": SWITCH, "kind": "add_node", "note": "the monopolist stands up a second key on %s" % ("the same hill" if second_key == "same-hill" else "the other hill"),
             "node": {"id": "mono2", "name": "the monopolist's second key", "place": place, "owner": "Mono", "roles": ["relay"], "bonds": {"v": 100}}},
            {"at": SWITCH, "kind": "add_link", "link": {"id": "b1", "src": "p1", "dst": "mono2", "owner": "mono2", "price": 0, "medium": "wire", "range": "street"}},
            {"at": SWITCH, "kind": "add_link", "link": {"id": "b2", "src": "p2", "dst": "mono2", "owner": "mono2", "price": 0, "medium": "wire", "range": "street"}},
            {"at": SWITCH, "kind": "add_link", "link": {"id": "bridge2", "src": "mono2", "dst": "far", "owner": "mono2", "price": str(price), "medium": "radio", "range": "hill"}},
        ]
    return {
        "scenario": {"name": "the monopolist's second key (%s)" % (second_key or "no second key"), "windows": WINDOWS, "window_seconds": 3600},
        "params": {"f": "0.05", "m": "0.025", "program_window": PROGRAM, "reconcile_every": 10 ** 6, "first_link_size": "0", "bounty_deadline": 168},
        "steward": {"opening_escrow": {"v": "100"}},
        "domains": [{"id": "v", "name": "the valley", "unit": "USD", "symbol": "$", "mint": "mint"}],
        "places": places, "nodes": nodes, "links": links,
        "demands": [{"id": "d1", "label": "payer one's traffic", "payer": "p1", "src": "p1", "dst": "far", "gb_per_window": 10},
                    {"id": "d2", "label": "payer two's traffic", "payer": "p2", "src": "p2", "dst": "far", "gb_per_window": 10}],
        "events": events,
    }


def run_monopolist(second_key=None, price=Fraction(9, 10)):
    run = run_dict(monopolist_scenario(second_key, price), "monopolist")
    after = (SWITCH, WINDOWS - 1)
    earned = run.net_by_owner("Mono", *after)
    bounty = sum(p.amount for p in run.rows(*after) if p.kind == "bounty" and run.nodes[p.to].owner == "Mono")
    posted = sum(c.escrow for c in run.commitments.values() if c.kind == "second_bridge")
    open_ = sum(c.pool for c in run.commitments.values() if c.kind == "second_bridge" and c.open)
    moved = run.gb_settled_on("bridge2", *after) if "bridge2" in run.links else Fraction(0)
    return dict(run=run, earned=earned, bounty=bounty, posted=posted, open=open_, moved=moved)


def main(argv=None):
    base = run_monopolist(None)
    same = run_monopolist("same-hill")
    other = run_monopolist("other-hill")
    print("A sole bridge at $1.00/GB carries 20 GB a window for two payers. At the end of each day the Steward posts the bridge's")
    print("fees as a bounty for a second path through a different mapped place. At window %d the monopolist stands up a second key." % SWITCH)
    print()
    print("%-44s %14s %16s %16s %14s" % ("windows %d–%d" % (SWITCH, WINDOWS - 1), "Mono earned", "bounty posted", "bounty collected", "GB moved"))
    for label, r in (("no second key", base), ("second key on the same hill, $0.90", same), ("second key on the other hill, $0.90", other)):
        print("%-44s %14s %16s %16s %14s" % (label, fmt(r["earned"], "USD", "$"), fmt(r["posted"], "USD", "$"),
                                              fmt(r["bounty"], "USD", "$"), "%.0f" % float(r["moved"])))
    print()
    lost = base["earned"] - same["earned"]
    print("SUMMARY: the second key on the same hill pulled all %.0f GB over at $0.90 and collected $0.00 of the %s bounty, which stays open; "
          "the monopolist earned %s less than with one key and gained nothing (lost %s). The same key on another mapped hill "
          "collected %s of the bounty: a real second bridge, which is what the bounty is for, even in the same hands." % (
              float(same["moved"]), fmt(same["posted"], "USD", "$"), fmt(lost, "USD", "$"), fmt(lost, "USD", "$"),
              fmt(other["bounty"], "USD", "$")))
    return 0
