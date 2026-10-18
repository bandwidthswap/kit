"""A Mint under-remits the fee to its Steward during a conjunction (A.5: reconciliation, exclusion).

The Earth–Mars–Jupiter scenario with a conjunction and no detour: for 84
six-hour windows nothing crosses between the worlds. A station on Ganymede
keeps buying bytes inside the Jupiter domain, and the Jupiter Mint keeps the
whole fee on those windows instead of remitting it to its Steward. Nobody
outside can audit it until the line returns. At the first reconciliation
after, the Earth Mint and the Mars Mint each compare the Jupiter Mint's
published settlements with its remittance, find the shortfall from its own
publications, publish the proof and exclude it. The Jupiter relay orbiter,
bonded on all three worlds, either stops settling in Jupiter's domain or
forfeits its Earth and Mars bonds the next time it does.
"""

from fractions import Fraction

from sim.money import fmt
from sim.scenario import load_raw
from sim.attacks import run_dict

START, END = 100, 183


def mint_scenario(behaviour="stop"):
    raw = load_raw("earth-mars-jupiter")
    raw["scenario"]["name"] = "the Jupiter Mint under-remits during the conjunction (bridges %s)" % behaviour
    raw["params"]["bridge_after_exclusion"] = behaviour
    # a conjunction with no detour: every interplanetary link down
    raw["events"] = [
        {"kind": "outage", "link": "direct", "from": START, "to": END, "note": "solar conjunction, no detour", "note_up": "the line returns"},
        {"kind": "outage", "link": "jup-mars", "from": START, "to": END, "note": "Mars behind the Sun too"},
        {"kind": "outage", "link": "mars-earth", "from": START, "to": END, "note": "Mars behind the Sun too"},
        {"kind": "under_remit", "mint": "jup-mint", "from": START, "to": END, "keep": 1.0,
         "note": "the Jupiter Mint keeps the fee on every window it settles while nobody can look"},
    ]
    raw["places"].append({"id": "ganymede", "name": "Ganymede", "domain": "jupiter", "medium": "UHF proximity radio", "range": "world"})
    raw["nodes"].append({"id": "ganymede", "name": "Ganymede station", "place": "ganymede", "roles": ["payer"], "bonds": {"jupiter": 5000}})
    raw["links"].append({"id": "jup-local", "src": "ganymede", "dst": "jup-orbiter", "owner": "jup-orbiter", "domain": "jupiter",
                         "price": 5.00, "medium": "UHF proximity radio", "range": "world", "window_kind": "contact"})
    raw["demands"].append({"id": "ganymede", "label": "Ganymede's local traffic", "payer": "ganymede", "src": "ganymede",
                           "dst": "jup-orbiter", "gb_per_window": 20})
    raw["scenario"]["detail_windows"] = []
    return raw


def run_mint(behaviour="stop"):
    run = run_dict(mint_scenario(behaviour), "mint-" + behaviour)
    kept = run.mints["jupiter"].kept
    due = sum(s.fee_due for s in run.settlements if s.domain == "jupiter" and START <= s.window <= END)
    found = {by: w for (by, of), (w, short) in run.excluded.items() if of == "jupiter"}
    settled_local = sum(1 for s in run.settlements if s.link == "jup-local" and START <= s.window <= END)
    unserved_after = sum(1 for (w, d, n, why) in run.unserved if d == "ganymede" and w > END)
    return dict(run=run, kept=kept, due=due, found=found, settled_local=settled_local, slashed=run.slashed,
                unserved_after=unserved_after)


def main(argv=None):
    stop = run_mint("stop")
    keep = run_mint("keep")
    r = stop
    print("Earth, Mars and Jupiter with a conjunction and no detour, windows %d–%d (six-hour contacts). Ganymede keeps buying" % (START, END))
    print("20 GB a window inside the Jupiter domain at 5 JUP/GB; the Jupiter Mint keeps the whole fee on those windows.")
    print()
    print("fees due to Jupiter's Steward in the silence:  %s" % fmt(r["due"], "JUP", ""))
    print("kept by the Jupiter Mint:                      %s" % fmt(r["kept"], "JUP", ""))
    print("local windows it settled meanwhile:            %d" % r["settled_local"])
    for by, w in sorted(r["found"].items()):
        print("found by the %s Mint at window %d:          first reconciliation after the line returned at %d" % (by.capitalize(), w, END + 1))
    print()
    print("bridges stop settling with the excluded Mint: the orbiter's Jupiter link closes; Ganymede loses %d windows of service" % r["unserved_after"])
    for (w, node, dom, bond) in keep["slashed"]:
        print("bridges keep settling instead:                %s forfeits its %s bond of %s at window %d" % (
            keep["run"].nodes[node].name, dom, fmt(bond, keep["run"].unit_of(dom), keep["run"].symbol(keep["run"].unit_of(dom))), w))
    print()
    first = min(r["found"].values()) if r["found"] else None
    print("SUMMARY: the Jupiter Mint kept %s of %s due to its Steward over %d silent windows; the Earth and Mars Mints found the "
          "shortfall from its own publications at window %s, the first reconciliation after the conjunction, published the proof and "
          "excluded it; its gain is the kept fee, its loss is every future reconciliation and the orbiter's business (%d windows of "
          "Ganymede's traffic unserved by run end), and an orbiter that kept settling would have forfeited %s." % (
              fmt(r["kept"], "JUP", ""), fmt(r["due"], "JUP", ""), END - START + 1, first, r["unserved_after"],
              " and ".join(fmt(b, keep["run"].unit_of(d), keep["run"].symbol(keep["run"].unit_of(d))) + " on " + d for (_, _, d, b) in keep["slashed"]) or "nothing"))
    return 0
