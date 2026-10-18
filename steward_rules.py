"""The Network Steward's rules as pure functions, shared by the simulator and the kit.

The Steward is a rule, not a party (paper, Section 7). These are the parts of
the rule that size money: the per-payer cap of Section 3.3, the match, the
second-bridge bounty, the first-link offer and the split of a purchase's
premium (Section 5.1). They carry no state and know nothing about nodes:
the simulator (sim/steward.py) feeds them exact Fractions and the Mint's
Steward (bwkit.py) feeds them floats, and both get the same rule, which is
the point of having one file.

Every amount is in the domain's unit; byte counts are integers; a gigabyte
is 10**9 bytes. The functions never return a negative amount.
"""

GB = 10 ** 9


def allowance(f, margin, fees_in, paid_on):
    """What the Steward may still pay out on one payer's receipts (paper, 3.3).

    Across every program, the Steward never pays out on receipts whose payer
    is a given NYM more than (f - margin) of the fees that NYM has paid in;
    fees_in is that sum (the gross amounts the payer paid for carriage) and
    paid_on what the Steward has paid out on its receipts so far.
    """
    cap = max(0, (f - margin) * fees_in)
    return max(0, cap - paid_on)


def draw(amount, pool_own, pool_match, allowed):
    """Take `amount` from a purchase's two pools for one payout: the poster's money first, then the Steward's,
    which the per-payer cap limits to `allowed`. Returns (from the poster, from the Steward, held by the cap)."""
    own = min(amount, pool_own)
    rest = amount - own
    match = min(rest, pool_match, allowed) if rest > 0 else 0
    match = max(0, match)
    held = max(0, min(rest, pool_match) - match)
    return own, match, held


def match_size(m, escrow, income, matched, steward_escrow):
    """Program 1: how much the Steward adds to a payer's purchase of `escrow` (paper, Section 7).

    The smaller of m of the poster's escrow, what is left of m of the
    Steward's income to date after the matches already made, and the escrow
    the Steward has in hand. m is strictly below f, so the poster ends behind.
    """
    if m <= 0 or escrow <= 0:
        return 0
    return max(0, min(m * escrow, m * income - matched, steward_escrow))


def bounty_budget(steward_escrow, m, income, matched):
    """What programs 2 and 3 may spend: the escrow less the unspent match allowance, which is held in reserve."""
    reserve = max(0, m * income - matched)
    return max(0, steward_escrow - reserve)


def second_bridge_size(f, fees, budget):
    """Program 2: a bounty for a second path around a sole link is sized from the Steward's income on it,
    f of the fees the link carried over the program window, within the budget (paper, Section 7; sim NOTES 16)."""
    return max(0, min(f * fees, budget))


def second_bridge_premium(value, nbytes):
    """The bounty's premium per GB: its value over the bytes the sole link settled, which is f of the link's
    effective price, so the Steward's take of a toll is the bounty for the next bridge. 0 when nothing moved."""
    if nbytes <= 0 or value <= 0:
        return 0
    return value * GB / nbytes


def first_link_size(first_link, budget, open_so_far):
    """Program 3: what to post toward a mapped place's standing offer, which is topped up toward `first_link`
    on every run from what the budget allows (PLAN.md 2.4), so an offer posted at a hundredth of a cent grows."""
    want = max(0, first_link - open_so_far)
    return max(0, min(want, budget))


def premium_split(settled_bytes, min_bytes, premium_per_gb):
    """Section 5.1's rule after the smaller count: the premium paid on a window and the part of it that returns.

    At or above the minimum the relay is paid the full premium on the bytes
    moved; below it, the premium in proportion to what moved over the
    minimum (which is the same figure, premium on the bytes moved), and the
    rest of that window's premium (the premium on the minimum) returns to the
    poster. Returns (paid, refund).
    """
    paid = premium_per_gb * settled_bytes / GB if settled_bytes > 0 else 0
    short = max(0, min_bytes - settled_bytes)
    refund = premium_per_gb * short / GB if short > 0 and min_bytes > 0 else 0
    return max(0, paid), max(0, refund)
