"""The objects the paper names.

NYMs are nodes (Section 2): a payer, a relay, a Mint, an endpoint, an anchor,
each a key with bonds in one or more settlement domains. A settlement domain
is a bookkeeper with a unit (Appendix A.5). A place is an anchor with a
medium and a range class (Section 5.1); the map is the registry of domains
and places (A.5). A link is what a relay publishes for one direction between
two nodes, with the parameters of Appendix A.1. A demand is traffic a payer
wants carried, per window. A commitment is Section 5.1's purchase commitment
with every field, the Steward's included (Section 7). A payment is one row of
the books: from, to, amount, unit, window, reason.
"""

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

from .money import GB, money


@dataclass
class Domain:
    id: str
    name: str
    unit: str = "USD"
    symbol: str = "$"
    mint: Optional[str] = None       # node id of the Mint that keeps this domain's books


@dataclass
class Place:
    id: str
    name: str
    domain: str
    medium: str = "radio"            # wire, radio, lora, hotspot, satellite, optical
    range_class: str = "point"       # building, street, hill, point-to-point, world
    mapped: bool = True              # accepted into the map by Mint consensus (A.5)
    since: int = 0                   # the window it entered the map


@dataclass
class Node:
    id: str
    name: str
    place: Optional[str] = None
    owner: Optional[str] = None      # the operator behind the key; the protocol cannot see it (Section 10)
    roles: tuple = ("relay",)
    bonds: dict = field(default_factory=dict)      # domain id -> amount, in the domain's unit
    bond_since: int = -10 ** 6       # the window the bond was posted; old unless said otherwise
    clock_offset_s: Fraction = Fraction(0)
    rates: dict = field(default_factory=dict)      # (from unit, to unit) -> how many `to` one `from` buys
    up: bool = True
    since: int = 0                   # the window the node appeared

    def __post_init__(self):
        if self.owner is None:
            self.owner = self.id
        self.bonds = {k: money(v) for k, v in self.bonds.items()}
        self.clock_offset_s = money(self.clock_offset_s)
        self.rates = {tuple(k) if not isinstance(k, tuple) else k: money(v) for k, v in self.rates.items()}

    def rate(self, from_unit, to_unit):
        if from_unit == to_unit:
            return Fraction(1)
        r = self.rates.get((from_unit, to_unit))
        if r is not None:
            return r
        r = self.rates.get((to_unit, from_unit))
        if r is not None and r != 0:
            return 1 / r
        return None


@dataclass
class Contact:
    """When a link exists: every `every` windows, for `duration` of them, from `offset` (A.1)."""
    every: int = 1
    duration: int = 1
    offset: int = 0

    def open(self, w):
        return (w - self.offset) % self.every < self.duration

    def next_open(self, w, limit):
        """The first window in [w, w + limit] when the contact is open, or None."""
        for t in range(w, w + limit + 1):
            if self.open(t):
                return t
        return None

    @property
    def continuous(self):
        return self.every == 1 and self.duration >= 1


@dataclass
class Link:
    """One direction between two nodes, published by its owner with the parameters of A.1."""
    id: str
    src: str
    dst: str
    owner: Optional[str] = None      # the relay paid for carrying across it; the source node unless said
    domain: Optional[str] = None     # the Mint that holds the bond for it; the source's place's domain unless said
    price: Fraction = Fraction(0)    # posted price per GB, in the domain's unit
    carry: Fraction = Fraction(0)    # per GB per hour the window's value stays locked (Section 3.6)
    light_time_s: Fraction = Fraction(0)
    contact: Contact = field(default_factory=Contact)
    clock_tolerance_s: Optional[Fraction] = None   # base tolerance; the run adds light time and drift (A.3)
    window_kind: str = "hour"        # hour, contact, transmission
    medium: str = "wire"
    range_class: str = "point-to-point"
    rate_mbps: Optional[Fraction] = None           # None: unlimited
    loss: Fraction = Fraction(0)     # fraction of bytes a lossy medium drops
    store_and_forward: bool = False  # a bundle received in one contact leaves in the next (A.2)
    passes_upstream: bool = True     # a bridge bills what it paid on the far side onward (A.5); False: its price covers it
    sig_scheme: str = "ed25519"
    receipt_lifetime_windows: Optional[int] = None
    up: bool = True
    since: int = 0

    def __post_init__(self):
        if self.owner is None:
            self.owner = self.src
        self.price = money(self.price)
        self.carry = money(self.carry)
        self.light_time_s = money(self.light_time_s)
        self.loss = money(self.loss)
        if self.clock_tolerance_s is not None:
            self.clock_tolerance_s = money(self.clock_tolerance_s)
        if self.rate_mbps is not None:
            self.rate_mbps = money(self.rate_mbps)
        if isinstance(self.contact, dict):
            self.contact = Contact(**self.contact)

    def cap_bytes(self, window_seconds):
        """The most this link carries in one window, or None."""
        if self.rate_mbps is None:
            return None
        return int(self.rate_mbps * 10 ** 6 / 8 * window_seconds)


@dataclass
class Demand:
    """Traffic a payer wants carried from `src` to `dst`, so many bytes a window."""
    id: str
    payer: str
    src: str
    dst: str
    bytes_per_window: Fraction = Fraction(0)       # may be fractional: 20 GB a day is 833,333,333⅓ bytes an hour
    start: int = 0
    end: Optional[int] = None        # inclusive
    schedule: list = field(default_factory=list)   # (from, to, bytes) overrides, inclusive
    label: str = ""

    def __post_init__(self):
        self.bytes_per_window = Fraction(self.bytes_per_window)

    def bytes_at(self, w):
        """Whole bytes this window; a fractional rate is rounded cumulatively so a day adds up exactly."""
        if w < self.start or (self.end is not None and w > self.end):
            return 0
        for a, b, n in self.schedule:
            if a <= w <= b:
                return int(n)
        k = w - self.start
        return int((k + 1) * self.bytes_per_window) - int(k * self.bytes_per_window)


@dataclass
class Commitment:
    """Section 5.1: a prepaid offer to buy bandwidth on a route, escrowed with a Mint.

    kind: 'payer' (a payer's own), 'match' is folded into the payer's as a
    contribution, 'second_bridge' and 'first_link' are the Steward's (Section 7).
    """
    id: str
    kind: str
    poster: str                      # account: a node id, or steward:<domain>
    domain: str
    unit: str
    min_bytes: int                   # minimum rate, bytes per window
    premium: Fraction                # per GB above the posted price
    windows: int
    deadline: int                    # window after which unspent escrow returns
    posted_at: int
    escrow: Fraction                 # value posted, every contribution included
    contributions: dict = field(default_factory=dict)   # account -> amount
    pool: Fraction = Fraction(0)     # what is left to pay out
    # the route, a payer's commitment: nodes or places, and a node the path must use
    src: Optional[str] = None
    dst: Optional[str] = None
    via: Optional[str] = None
    # the route, the Steward's: named in places (Section 7)
    from_places: frozenset = frozenset()
    to_places: frozenset = frozenset()
    avoid_link: Optional[str] = None
    existing_places: frozenset = frozenset()
    to_place: Optional[str] = None
    # proving (Section 5.1)
    proving_windows: int = 0
    proving_period: int = 168
    proving_used: dict = field(default_factory=dict)    # period index -> windows reimbursed
    # state
    served: set = field(default_factory=set)
    status: str = "open"             # open, complete, exhausted, expired
    label: str = ""
    paid_out: Fraction = Fraction(0)
    refunded: Fraction = Fraction(0)

    def __post_init__(self):
        self.premium = money(self.premium)
        self.escrow = money(self.escrow)
        self.pool = money(self.pool)
        self.contributions = {k: money(v) for k, v in self.contributions.items()}
        self.from_places = frozenset(self.from_places)
        self.to_places = frozenset(self.to_places)
        self.existing_places = frozenset(self.existing_places)

    @property
    def open(self):
        return self.status == "open"

    def per_window_premium(self):
        return self.premium * Fraction(self.min_bytes, GB)


@dataclass
class Payment:
    """One row of the books."""
    from_: str
    to: str
    amount: Fraction
    unit: str
    window: int
    reason: str
    kind: str = "payment"            # payment, fee, remit, escrow, match, bounty, premium, refund, reimburse, carry, slash, convert
    nbytes: int = 0                  # settled bytes the row is for, when it is for bytes
    link: Optional[str] = None
    demand: Optional[str] = None
    commitment: Optional[str] = None
    bundle: Optional[str] = None     # demand@window dispatched, so a bundle's rows can be gathered

    def row(self):
        return (self.from_, self.to, "%.6f" % float(self.amount), self.unit, self.window, self.reason)


@dataclass
class Settlement:
    """What a Mint writes once it holds both halves of a window (Section 3.6)."""
    window: int
    link: str
    domain: str
    payer: str
    hop: str
    hop_index: int
    payer_bytes: int
    hop_bytes: int
    settled_bytes: int
    status: str                      # settled, disputed, declined
    amount: Fraction                 # gross: posted price on settled bytes plus carry
    carry: Fraction
    fee_due: Fraction
    fee_remitted: Fraction
    receipt_sig: str
    ack_sig: str
    demand: str
    payer_of_record: str
    holding_windows: int = 0
    tolerance_s: Fraction = Fraction(0)
    commitment: Optional[str] = None
    mirror_domains: tuple = ()
