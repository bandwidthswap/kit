"""The wash trader (Section 3.3, Open Question A1/A2): payer, relay and Mint in one hand.

One operator, W, owns the payer, the relay, the sink, the Mint and the keys. W
posts a purchase commitment on its own route and serves it with its own
traffic, so every dollar it escrows comes back to it less the fee f, plus the
Steward's match of m. Over a run in which the commitment is fully served
(the match buys more windows at W's own terms), W's net per dollar escrowed is

    (1 + m)(1 - f) - 1 = m(1 - f) - f

which is negative for every m strictly below f, and crosses zero at
m = f / (1 - f), above f. The sweep below runs the model rather than the
formula, with a Steward whose pool is deep (other people's fees), which is
the worst case for the rule: the per-commitment cap m × escrow binds, not
the cap on the Steward's income.

The second table lets W also build the second path the Steward's rule asks
for: W's only link is a sole bridge, so after the first program window the
Steward posts its take of that link's fees (f of them) as a bounty for a
path through a different mapped place. W stands up a second anchor on a
second hill it owns and moves its traffic there. Ownership is unseen
(Section 10) by default, and every Steward payout on a payer's receipts is
capped at f minus a margin of the fees that payer paid in, so the bounty
cannot hand W back what the fee took. The third table splits W's traffic
across several payer NYMs: the caps are per NYM and add up to the same bound.
"""

from fractions import Fraction

from sim.money import fmt, money, pct
from sim.attacks import run_dict

WINDOWS = 200          # windows the commitment buys; m × 200 is whole for m in steps of 0.5%
PROGRAM = 24


def wash_scenario(f, m, every_program=False, sees_ownership=False, pool=Fraction(1000), payers=1, margin=None):
    """payers: how many payer NYMs W splits its traffic (and its bounty) across; the cap is counted per NYM."""
    f, m = money(f), money(m)
    extra = int(m * WINDOWS)                       # windows the match buys
    served = WINDOWS + extra
    total = served + 2 * PROGRAM + 8
    places = [
        {"id": "yard", "name": "W's yard", "domain": "w", "medium": "wifi", "range": "building"},
        {"id": "shed", "name": "W's shed", "domain": "w", "medium": "wifi", "range": "building"},
        {"id": "sink", "name": "W's server", "domain": "w", "medium": "wire", "range": "building", "mapped": False},
    ]
    nodes = [
        {"id": "whop", "name": "W's relay", "place": "shed", "owner": "W", "roles": ["relay"], "bonds": {"w": 100}},
        {"id": "wsink", "name": "W's server", "place": "sink", "owner": "W", "roles": ["endpoint"]},
        {"id": "wmint", "name": "W's Mint", "place": "shed", "owner": "W", "roles": ["mint"]},
    ]
    links = [
        {"id": "l2", "src": "whop", "dst": "wsink", "owner": "whop", "price": 0, "medium": "wire", "range": "building"},
    ]
    demands, commitments, events = [], [], []
    share = Fraction(1, payers)
    for k in range(1, payers + 1):
        pid = "wpay" if payers == 1 else "wpay%d" % k
        nodes.append({"id": pid, "name": "W's payer" if payers == 1 else "W's payer %d" % k, "place": "yard", "owner": "W",
                      "roles": ["payer"], "bonds": {"w": 100}})
        links.append({"id": "l1" if payers == 1 else "l1-%d" % k, "src": pid, "dst": "whop", "owner": "whop", "price": 1.00,
                      "medium": "wifi", "range": "building"})
        demands.append({"id": "wash" if payers == 1 else "wash%d" % k, "label": "W's traffic to itself", "payer": pid,
                        "src": pid, "dst": "wsink", "gb_per_window": str(share), "from": 0, "to": served - 1})
        commitments.append({"at": 0, "id": "wash-bounty" if payers == 1 else "wash-bounty-%d" % k,
                            "label": "W's bounty on its own route" if payers == 1 else "W's bounty %d" % k, "poster": pid,
                            "src": pid, "dst": "wsink", "min_gb_per_window": str(share), "premium": 0.10, "posted": 1.00,
                            "windows": WINDOWS, "deadline": total - 1})
    if every_program:
        places.append({"id": "hill2", "name": "W's second hill", "domain": "w", "medium": "wifi", "range": "hill",
                       "since": PROGRAM})
        events.append({"at": PROGRAM, "kind": "add_node", "note": "W stands up a second anchor on a second hill",
                       "node": {"id": "whop2", "name": "W's second relay", "place": "hill2", "owner": "W", "roles": ["relay"],
                                "bonds": {"w": 100}}})
        events.append({"at": PROGRAM, "kind": "add_link", "link": {"id": "l4", "src": "whop2", "dst": "wsink", "owner": "whop2",
                                                                   "price": 0, "medium": "wire", "range": "building"}})
        for k in range(1, payers + 1):
            pid = "wpay" if payers == 1 else "wpay%d" % k
            events.append({"at": PROGRAM, "kind": "add_link", "link": {"id": "l3" if payers == 1 else "l3-%d" % k, "src": pid,
                                                                       "dst": "whop2", "owner": "whop2", "price": 1.00,
                                                                       "medium": "wifi", "range": "hill"}})
            events.append({"at": PROGRAM, "kind": "price", "link": "l1" if payers == 1 else "l1-%d" % k, "price": 1.01,
                           "note": "W nudges the first link's price so its traffic takes the second path"})
    params = {"f": str(f), "m": str(m), "program_window": PROGRAM if every_program else 10 ** 6,
              "reconcile_every": 10 ** 6, "sees_ownership": sees_ownership, "first_link_size": "0",
              "bounty_deadline": total, "min_bond_age": 0}
    if margin is not None:
        params["steward_margin"] = str(money(margin))
    raw = {
        "scenario": {"name": "wash trader f=%s m=%s" % (pct(f), pct(m)), "windows": total, "window_seconds": 3600},
        "params": params,
        "steward": {"opening_escrow": {"w": str(pool)}},
        "domains": [{"id": "w", "name": "W's domain", "unit": "USD", "symbol": "$", "mint": "wmint"}],
        "places": places, "nodes": nodes, "links": links,
        "demands": demands, "commitments": commitments, "events": events,
    }
    return raw


def run_wash(f, m, **kw):
    """W's net per dollar it put in: (received - paid) over every account W owns, divided by what W escrowed and paid."""
    run = run_dict(wash_scenario(f, m, **kw), "wash")
    net = run.net_by_owner("W")      # an honest Mint nets to zero: it remits every fee it collects
    out = sum((run.paid(a, kinds=("escrow", "payment", "fee", "convert")) for a in run.owner_accounts("W")), Fraction(0))
    return (net / out if out else Fraction(0)), run


def sweep(step=Fraction(1, 200), print_=True):
    """f from 0 to 10% in whole percent; m from 0 to f in steps of 0.5%, then on until the net turns positive."""
    lines = []
    results = {}
    header = "%6s | %12s %12s %12s | %s" % ("f", "m = 0", "m = f/2", "m = f", "first m with net > 0")
    lines.append(header)
    lines.append("-" * len(header))
    for fi in range(0, 11):
        f = Fraction(fi, 100)
        nets = {}
        m = Fraction(0)
        first_positive = None
        while True:
            net, _ = run_wash(f, m)
            nets[m] = net
            if net > 0 and first_positive is None:
                first_positive = m
                break
            if m > f and f == 0:
                break
            if m > 3 * f + step:
                break
            m += step
        results[f] = (nets, first_positive)
        half = f / 2
        def at(mm):
            if mm in nets:
                return fmt(nets[mm] * 100, "", "") + "¢"
            lo = max((k for k in nets if k <= mm), default=None)
            return fmt(nets[lo] * 100, "", "") + "¢" if lo is not None else "-"
        fp = "never up to m = 3f: the cap holds payouts below f" if first_positive is None else "%s (f/(1-f) = %s)%s" % (
            pct(first_positive), pct(f / (1 - f)) if f < 1 else "-", "" if first_positive <= f else ", above f")
        if f == 0:
            fp = "net is 0 at every m: nothing is taken, nothing is matched"
        lines.append("%6s | %12s %12s %12s | %s" % (pct(f), at(Fraction(0)), at(half), at(f), fp))
    if print_:
        print("\n".join(lines))
    return results


def every_program_table(f=Fraction(5, 100), m=Fraction(5, 200), print_=True):
    """W also builds the second path the Steward asks for; ownership unseen (Section 10) unless said."""
    rows = []
    for sees in (False, True):
        for pool in (Fraction(1000), Fraction(0)):
            net, run = run_wash(f, m, every_program=True, sees_ownership=sees, pool=pool)
            bounty = sum(c.paid_out for c in run.commitments.values() if c.kind == "second_bridge")
            posted = sum(c.escrow for c in run.commitments.values() if c.kind == "second_bridge")
            rows.append((sees, pool, net, bounty, posted, run))
    if print_:
        print()
        print("W also builds the second path the Steward asks for (f = %s, m = %s, payouts capped at f − margin = %s of W's fees in):" % (
            pct(f), pct(m), pct(f - Fraction(1, 200))))
        print("%-36s %-26s %12s %14s %16s %12s" % ("rule", "Steward's pool", "net per $", "bounty posted", "bounty collected", "cap held"))
        for sees, pool, net, bounty, posted, run in rows:
            print("%-36s %-26s %11s¢ %14s %16s %12s" % (
                "Section 10: ownership unseen" if not sees else "5.1: ownership seen, W's traffic out",
                "deep (others' fees, $1000)" if pool else "empty (W's own fees only)",
                fmt(net * 100, "", ""), fmt(posted, "USD", "$"), fmt(bounty, "USD", "$"),
                fmt(run.stewards["w"].capped, "USD", "$")))
    return rows


def split_table(f=Fraction(5, 100), m=Fraction(5, 200), print_=True):
    """W splits its traffic and its bounty across several payer NYMs; the caps are counted per NYM."""
    rows = []
    for k in (1, 2, 4):
        net, run = run_wash(f, m, every_program=True, sees_ownership=False, payers=k)
        rows.append((k, net, run.stewards["w"].paid_out, run.stewards["w"].capped))
    if print_:
        print()
        print("W splits its traffic across payer NYMs (ownership unseen, deep pool):")
        print("%-14s %12s %18s %12s" % ("payer NYMs", "net per $", "Steward paid out", "cap held"))
        for k, net, paid, held in rows:
            print("%-14d %11s¢ %18s %12s" % (k, fmt(net * 100, "", ""), fmt(paid, "USD", "$"), fmt(held, "USD", "$")))
    return rows


def main(argv=None):
    print("The wash trader: W owns the payer, the relay, the sink and the Mint; posts a bounty on its own route and serves it.")
    print("Net per dollar W put in (escrow plus any direct payments), after the commitment and the Steward's match are fully served.")
    print("The Steward's pool is deep ($1000 of other people's fees), so the match is bounded by m × W's escrow, the worst case.")
    print("Ownership is unseen (Section 10). The Steward pays out on any one payer's receipts at most f − 0.5% of the fees it paid in.")
    print()
    results = sweep()
    rows = every_program_table()
    split = split_table()
    f, m = Fraction(5, 100), Fraction(5, 200)
    net, _ = run_wash(f, m)
    nets, fp = results[f]
    print()
    worst = max(r[2] for r in rows)
    spread = max(r[1] for r in split) - min(r[1] for r in split)
    summary = ("SUMMARY: at the paper's defaults (f = 5%%, m = 2.5%%) the wash trader loses %s on every dollar it washes; "
               "the match never makes it whole (%s). Building the second path the Steward asks for and collecting its "
               "bounty on its own traffic leaves it at %s per dollar at best, because the bounty is sized at f of the sole "
               "link's fees and every payout on W's receipts stops at f − 0.5%% of what W paid in. Splitting its traffic "
               "across 1, 2 or 4 payer NYMs moves the net by %s: the caps add up to the same bound." % (
                   fmt(-net * 100, "", "") + "¢",
                   ("positive first at m = %s, above f" % pct(fp)) if fp else "no m up to 3f turns it positive",
                   fmt(worst * 100, "", "") + "¢", fmt(spread * 100, "", "") + "¢"))
    print(summary)
    return 0
