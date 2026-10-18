"""Routes: which paths exist, which is cheapest, and who pays whom along one.

Section 3.4 and A.6: a payer's software ranks paths by what previous payers
settled; the simulator ranks by posted price (plus carry) in the payer's
unit, then by delay, then by name, which is the paper's "cheapest way around"
in every example. A path whose next contact is further off than `max_wait`
windows is not taken (A.6 computes routes from contact plans).

A sole bridge (Section 7, program 2) is a link with no second path between
its ends through a different mapped place. That reads the map, so it uses
the links that exist, up or down.
"""

from fractions import Fraction

MAX_HOPS = 8


def node_up(state, nid, w):
    n = state.nodes[nid]
    return n.up and w >= n.since


def link_exists(state, link, w):
    """In the map: registered, whether or not it is open or up this window."""
    return w >= link.since and link.src in state.nodes and link.dst in state.nodes


def link_open(state, link, w):
    """Carrying bytes this window."""
    return (link.up and w >= link.since and node_up(state, link.src, w) and node_up(state, link.dst, w)
            and link.contact.open(w))


def link_soon(state, link, w, max_wait):
    """Open this window, or up and planned to open within max_wait windows (A.6)."""
    if link_open(state, link, w):
        return 0
    if not (link.up and w >= link.since and node_up(state, link.src, w) and node_up(state, link.dst, w)):
        return None
    t = link.contact.next_open(w, max_wait)
    return None if t is None else t - w


def domain_of_node(state, nid):
    n = state.nodes[nid]
    if n.place is None:
        return None
    return state.places[n.place].domain


def place_of(state, nid):
    return state.nodes[nid].place


def out_links(state, nid):
    return [l for l in state.links.values() if l.src == nid]


def enumerate_paths(state, src, dst, w, max_wait, avoid_link=None, in_map=False):
    """All simple paths of links from src to dst, as lists of Link.

    in_map: paths that exist in the registry (for the Steward's map rule).
    Otherwise the first link must be open now and later ones open or soon.
    """
    out = []

    def usable(link, first):
        if avoid_link is not None and link.id == avoid_link:
            return False
        if in_map:
            return link_exists(state, link, w)
        if first:
            return link_open(state, link, w)
        return link_soon(state, link, w, max_wait) is not None

    def walk(node, path, seen):
        if len(path) >= MAX_HOPS:
            return
        for link in sorted(out_links(state, node), key=lambda l: l.id):
            if link.dst in seen or not usable(link, not path):
                continue
            if link.dst == dst:
                out.append(path + [link])
            else:
                walk(link.dst, path + [link], seen | {link.dst})

    walk(src, [], {src})
    return out


def payment_chain(state, path, payer):
    """Who pays for each link of a path, and how a bridge bills it back toward the payer (A.5).

    Returns a list aligned with `path`: {payer_of_record, unit, bills}, where
    bills is the stack of conversions between the payer and that payer of
    record, nearest the payer first: {from_, to, from_unit, to_unit, rate, passes}.
    """
    n = len(path)
    if n and path[-1].dst == payer and path[0].src != payer:
        order = list(range(n - 1, -1, -1))
    else:
        order = list(range(n))
    out = [None] * n
    por = payer
    por_unit = state.unit_of(domain_of_node(state, payer)) if domain_of_node(state, payer) else None
    stack = []
    prev = None
    for idx in order:
        link = path[idx]
        unit = state.unit_of(link.domain)
        if prev is None:
            if por_unit is None:
                por_unit = unit
        elif link.domain != prev.domain:
            bridge = shared_node(prev, link)
            node = state.nodes[bridge]
            rate = node.rate(unit, por_unit)
            if rate is None:
                rate = Fraction(1)
            stack = stack + [dict(from_=por, to=bridge, from_unit=por_unit, to_unit=unit, rate=rate,
                                  passes=prev.passes_upstream)]
            por = bridge
            por_unit = unit
        out[idx] = dict(payer_of_record=por, unit=unit, bills=list(stack))
        prev = link
    return out


def shared_node(a, b):
    for x in (a.src, a.dst):
        if x in (b.src, b.dst):
            return x
    raise ValueError("links %s and %s do not meet" % (a.id, b.id))


def cost_per_gb(state, path, payer, w, window_hours):
    """What the payer pays per GB along the path, in its own unit: posted prices plus carry, through the bridges."""
    chain = payment_chain(state, path, payer)
    total = Fraction(0)
    delay = 0
    for idx, link in enumerate(path):
        hold = 1 if (link.store_and_forward and idx > 0) else 0
        soon = link_soon(state, link, w, state.params["max_wait"])
        wait = soon if soon is not None else 0
        hours = (hold + wait) * window_hours
        amount = link.price + link.carry * hours
        for bill in reversed(chain[idx]["bills"]):
            if not bill["passes"]:
                amount = Fraction(0)
                break
            amount = amount * bill["rate"]
        total += amount
        delay += hold + wait
    return total, delay


def ranked_paths(state, src, dst, payer, w, avoid_link=None):
    paths = enumerate_paths(state, src, dst, w, state.params["max_wait"], avoid_link=avoid_link)
    scored = []
    for p in paths:
        cost, delay = cost_per_gb(state, p, payer, w, state.window_hours)
        scored.append((cost, delay, [l.id for l in p], p))
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return scored


def best_path(state, src, dst, payer, w):
    ranked = ranked_paths(state, src, dst, payer, w)
    return ranked[0][3] if ranked else None


def relays_on(path, payer):
    """The link owners paid along a path, in order, each once."""
    seen = []
    for l in path:
        if l.owner != payer and l.owner not in seen:
            seen.append(l.owner)
    return seen


def alternatives(ranked, relay):
    """How many acceptable paths reach the endpoint without this relay (Section 5)."""
    return sum(1 for _, _, _, p in ranked if all(l.owner != relay for l in p))


def opportunity_shares(state, path, payer, w):
    """Divide a purchase among the relays on a path by opportunity cost: weight 1/(1 + alternatives), normalised."""
    ranked = ranked_paths(state, path[0].src, path[-1].dst, payer, w)
    relays = relays_on(path, payer)
    if not relays:
        return {}
    weights = {r: Fraction(1, 1 + alternatives(ranked, r)) for r in relays}
    total = sum(weights.values())
    return {r: wgt / total for r, wgt in weights.items()}


def places_on(state, path):
    out = set()
    for l in path:
        for nid in (l.src, l.dst):
            p = place_of(state, nid)
            if p is not None:
                out.add(p)
    return out


def intermediate_places(state, path):
    out = set()
    for l in path[:-1]:
        p = place_of(state, l.dst)
        if p is not None:
            out.add(p)
    return out


def is_sole(state, link, w, origins=None, dests=None, existing=None):
    """No second path between the link's ends through a different mapped place (Section 7, A.5).

    The ends are read as the ends of the traffic the link carried: the nodes
    it came from and went to, with `existing` the places on the paths it
    took. A second path counts when the map has one from an origin to a
    destination that avoids the link and passes through a mapped place not
    on the existing paths (NOTES.md). With no traffic given, the link's own
    ends are used.
    """
    if not origins or not dests:
        origins, dests = {link.src}, {link.dst}
        existing = {place_of(state, link.src), place_of(state, link.dst)} - {None}
    existing = set(existing or ())
    for o in sorted(origins):
        for d in sorted(dests):
            for p in enumerate_paths(state, o, d, w, 0, avoid_link=link.id, in_map=True):
                for pl in intermediate_places(state, p):
                    if pl not in existing and state.places[pl].mapped and state.places[pl].since <= w:
                        return False
    return True


def domains_connected(state, d1, d2, w):
    """A link between the two domains exists this window, directly or through others (A.5: reconciliation)."""
    adj = {}
    for l in state.links.values():
        if not link_open(state, l, w):
            continue
        a, b = domain_of_node(state, l.src), domain_of_node(state, l.dst)
        if a is None or b is None or a == b:
            continue
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    seen, todo = {d1}, [d1]
    while todo:
        d = todo.pop()
        if d == d2:
            return True
        for nxt in adj.get(d, ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return False
