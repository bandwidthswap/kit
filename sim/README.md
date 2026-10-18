# sim: the Bandwidth Swap protocol, run over scenarios with the money counted

A deterministic simulator of whitepaper v0.5. A scenario file declares a map, links, actors, prices, traffic and events; the run moves bytes window by window, writes a receipt for every hop, settles each at the smaller of the two signed counts, pays for it less the fee, lets the Steward's rule spend the fees, reconciles Mints across worlds, and records every payment as a row: from, to, amount, unit, window, reason. Nothing is random. Amounts are exact fractions and are rounded only when printed.

    python3 -m sim scenarios/street.toml          # a flow table, and sim/out/street.csv
    python3 -m sim earth-mars-jupiter --by phase   # a bare name finds sim/scenarios/<name>.toml
    python3 -m sim cabin --windows 48-50 --log     # every row of those windows, every log line
    python3 -m sim.attacks wash                    # the Section 3.3 sweep; also monopolist, mint
    python3 -m sim.tests                           # every scenario and attack, the headline numbers asserted

Python 3.11 or newer for TOML scenarios (3.9 reads JSON ones); no other dependency. Runs take well under a second each; the wash sweep, a few hundred runs, about six seconds. The tests also run as `python3 -m unittest sim.tests` or `python3 -m unittest discover -s sim` from the kit's root. One test is skipped unless the `cryptography` package is installed: `Receipts.test_against_bwkit_when_it_imports`, which signs receipts with real ed25519 keys through bwkit.py and checks `bwkit.resolve` against the copied rule.

What follows is the model in prose, in the paper's order, written as the protocol draft it is. The paper's sections are cited in brackets; where the paper is ambiguous the choice made is in [NOTES.md](NOTES.md) with the section number, and this document says only what the simulator does.

## 1. Settlement domains, units and Mints [2, 4, A.5]

A **settlement domain** is a bookkeeper with a unit of account: in practice a world. It has a name, a unit (`USD`, `MRS`, `JUP`), a symbol for printing, and a **Mint**, the node that keeps its books. Two Mints keeping books in the same unit are two domains (Earth and the Moon); one Mint settling links in two units would be one.

A Mint settles the links of its domain. For each settlement it collects the fee *f* from the payer's side and remits it to the domain's Steward, and it publishes the settlement. A relay bonded in more than one domain carries a copy of each settlement to the Mint on its other side, so each Mint holds the other's record of the links they share.

## 2. NYMs: payers, relays, endpoints, Mints [2, 5]

A **node** is a key: a payer, a relay, an endpoint, an anchor, a Mint, or several of these. It has a place, an owner (the operator behind the key, which the simulator can see and the protocol cannot; Section 10), bonds in one or more domains with the window each was posted, a clock offset in seconds, and, for a bridge, exchange rates between units. A node that appears mid-run has a `since` window and its bond is dated then.

## 3. Places and the map [5.1, A.5]

A **place** is an anchor somebody operates: a name, a domain, a medium (wire, Wi-Fi, 915 MHz, Ka-band) and a range class (building, street, hill, world), and whether the map has accepted it. The **map** is the registry of domains and places. The Steward's programs read it: a route in a bounty is named in places, and two paths are disjoint when they pass through different mapped places, which a second key on the same hill cannot fake. A place enters the map by declaration in the scenario, dated with `since`, or by a `map_place` event.

## 4. Links and their parameters [A.1]

A **link** is one direction between two nodes, published by its owner, the relay paid for carrying across it. It carries the parameters of Appendix A.1: a posted price per gigabyte in its domain's unit; a carry, the price per gigabyte per hour that a window's value stays locked; its one-way light time; a contact plan (open every so many windows for so many, from an offset; continuous by default); a base clock tolerance; the kind of window it accepts (an hour, a contact); a medium and a range class; a signature scheme and how long a receipt stays presentable; and the settlement domain it settles in, which is the Mint holding its bond. The simulator adds a capacity (`rate_mbps`), a loss fraction, whether it stores a bundle received in one contact and forwards it in the next, and, on a bridge's near-side link, whether the bridge bills what it paid on the far side back toward the payer. A link declared `bidirectional` is two links, each owned by the node that sends across it.

## 5. Windows, receipts and the smaller count [3.1, 3.5, 3.6, Figure 2]

The clock runs in **windows** of a fixed length, an hour on the street, six hours between planets. A **demand** is traffic a payer wants carried from a source to a destination, so many bytes a window (a per-day rate is spread over the day's windows so it adds up exactly), from one window to another, with a schedule of overrides for rain or a test.

Each window, each demand's bytes become a **bundle** on the cheapest route open to it (Section 6 below). The bundle crosses one link at a time. Crossing a link, the payer signs the Figure 2 receipt for that hop:

    (payer NYM, hop NYM, hop index, merkle root, bytes, t_first, t_last, prev receipt sig)

with the bytes it sent and the window's bounds as timestamps, chained to the previous hop's acknowledgement; the hop countersigns the same tuple with the bytes it forwarded. Before that, the hop checks the payer's clock against its own: the tolerance is the link's base, plus the light time both ways between the payer and the hop, plus the drift expected since the link's last contact; outside it the hop declines, the window is lost and nothing is paid. The hop's count is what reached it, capped by the link's capacity and less its loss; a scenario can force either count (the bench's counter resets).

The hop settles when the next hop takes custody, at the window it does: the smaller of the two signed counts, recorded `settled` when they are within one percent (or 64 KiB) and `disputed` otherwise, by the same rule either way. The tuple, the signing domain, the thresholds and the rule are copied exactly from bwkit.py (`sim/receipts.py`), because bwkit imports the `cryptography` package at module level and the simulator must run without it; when bwkit does import, the copies are checked against it, and `tests.py` checks the rule against `bwkit.resolve` with real keys. The simulator's "signature" is a SHA-256 over the signer and the canonical bytes: enough to chain receipts and tell copies apart, not a signature.

## 6. Routes and bridges [3.4, 5, A.5, A.6]

A route is the cheapest path open this window from the demand's source to its destination: the sum of posted prices and carry in the payer's unit, converted through the bridges' rates, then the shortest delay, then name. A link that is not open now but whose plan opens it within `max_wait` windows can be a later hop; a link down by event cannot.

Who pays for each hop is read from the payer's end of the path. The payer pays the hops in its own domain. Where the path crosses into another domain, the node between the two links is the bridge: it pays the far hops in the far unit and, if its near-side link `passes_upstream`, bills the payer of record behind it at its own rate, as a conversion row. The Mars bridge does ($63 = $20 + $3 carry + $40 for the Jupiter hop); the Jupiter orbiter does not, and pays the Europa lander out of its own price. The fee on each hop goes to the Mint of the link's domain, in that unit, so a route across three worlds pays three Stewards.

## 7. Payment, the fee and the rows [3.2, 3.3]

For each settled hop the simulator writes: the payer of record pays the relay the posted price on the settled bytes plus any carry, less the fee *f* (default 5%); the payer of record pays the fee to the Mint; the Mint remits it to the Steward (less what a cheating Mint keeps). A Mint's own share (`mint_share`) is zero by default. Conversions, premiums, reimbursements, escrows, matches, bounties, refunds and bond forfeits are rows of their own kinds. The CSV is every row; the flow table aggregates them by period, from, to and purpose, then says what each party kept.

## 8. Purchase commitments [5.1]

A **commitment** carries every field of 5.1: the poster, a route (a source, a destination, a node the path must use), a minimum rate in bytes per window, a premium per gigabyte above the posted prices, a number of windows, a deadline, and a proving allowance (windows per period). Its value is escrowed with the poster's Mint at posting: by default windows × minimum × (posted + premium), the cabin's 720 × 1 GB × $0.25 = $180.

A window on the route by the poster draws from the escrow: the posted prices, then the premium on the bytes moved, divided among the relays on the path by opportunity cost (each weighs one over one plus the number of acceptable paths that avoid it; two sole relays split it evenly). Below the minimum, the premium is paid in proportion and the rest of the window's premium returns to the poster at once. A window with nothing sent draws nothing. The escrow pays for the stated number of served windows or until the deadline, when what is left returns to its contributors pro rata; an escrow that runs dry is marked exhausted and the poster pays the rest itself.

**Proving.** A payer other than the poster, bonded before the commitment and not owned by a relay on the path, who buys bytes on the route is reimbursed its posted price from the escrow, up to the allowance per period. The testers keep the bandwidth and the route settles in the receipt graph.

## 9. The Network Steward [3.3, 7]

The Steward is a rule, one per domain, with an escrow. Its **income** is *f* of every fee, remitted by the Mint; a scenario may give it an opening balance, the fees taken before the run. Its only output is purchase commitments, under three programs:

1. **Matching.** When a payer whose bond is at least `min_bond_age` windows old posts a commitment, the Steward adds the smaller of *m* × the escrow, *m* × its income to date less matches already made, and what it holds; *m* (default 2.5%) is strictly below *f*. The match buys more windows at the poster's own terms and is kept apart from the poster's money, which is drawn first; so a fully served commitment collects its escrow plus the match, less *f* on every payout, and a payer serving its own bounty ends behind.
2. **The second bridge.** At the end of every `program_window` windows, for each link that carried fees, the Steward asks the map whether the traffic that crossed it has a second path: one from where that traffic came from to where it went that avoids the link and passes through a mapped place not on the paths it took. If none, the link is a sole bridge, and the Steward posts a commitment sized from its own income on that link, *f* times the fees it carried (capped by the escrow left after the match reserve): premium per gigabyte equal to *f* of the link's effective price, a minimum of the link's bytes per window, open for `bounty_deadline` windows. Whoever serves a qualifying path collects it on the bytes of eligible payers, divided by opportunity cost; a second key on the same hill qualifies no path and earns nothing. Unspent escrow returns and the link is considered again.
3. **The first link.** Every mapped place in the domain across which no receipt has settled gets a standing commitment of `first_link_size` at `first_link_premium` per gigabyte (off by default), reposted until a receipt settles there.

**The per-payer cap.** Section 3.3's condition, that no payer gets back through the Steward as much as the Steward took from it, is enforced at every payout of the Steward's money, across all three programs: on receipts whose payer is a given NYM, the Steward never pays out more than *f* − `steward_margin` (default 0.5%) of the fees that NYM has paid in for carriage in the domain. It is counted per payer NYM with no view of ownership, because every Steward payout is against a receipt and every receipt names its payer; a trader that splits its traffic across several NYMs faces caps that add up to the same bound. What the cap holds back stays in the commitment's pool and returns to the Steward at the deadline.

Everything a commitment returns comes back to the escrow. The Steward never pays a payer and never pays for presence: every unit it spends traces to a settled receipt.

## 10. Reconciliation and exclusion [A.5]

At the end of every `reconcile_every` windows, each pair of domains joined by any chain of open links reconciles: each Mint checks the other's published settlements since they last met against its remittance, and the two deposit each other's commitment (logged). A Mint that remitted less than *f* of the fees it settled is shown to have done so from its own publications and is **excluded** by the Mint that finds it: that Mint stops reconciling with it, and every relay bonded on both sides either stops settling in the excluded domain (its links there decline windows; the default) or keeps settling and forfeits its bond on the honest side at the next window it does. A Mint nobody can reach, during a conjunction, is found at the first reconciliation after.

## 11. Events

An event has a window (`at`, or `from`/`to` for an outage) and a kind: a link or node down or up; an outage over a range; a price change; a new node, link or demand; a commitment posted; a counter override (a node's signed count for one window); a clock step; a Mint under-remitting over a range; a place accepted into the map. Events draw the phase boundaries of the flow table.

## 12. Scenario files

TOML (or JSON with the same shape). The header of [scenario.py](scenario.py) lists every table and key; [scenarios/street.toml](scenarios/street.toml) is the smallest complete example. Parameters and their defaults: `f` 5%, `m` 2.5%, `steward_margin` 0.5%, `mint_share` 0, `program_window` 24, `reconcile_every` 24, `first_link_size` 0, `first_link_premium` 0.05, `bounty_deadline` 168, `min_bond_age` 24, `clock_base_s` 5, `drift_s_per_day` 0.76, `sees_ownership` true, `max_wait` 12, `bridge_after_exclusion` stop, `epoch` 0.

## 13. The scenarios shipped

Numbers from INSTALLATIONS.md and the field note, which the flow tables reproduce and `tests.py` asserts.

| File | What | Headline |
|---|---|---|
| `bench.toml` | Phase 0: payer, hop, Mint on one LAN; the forced tests of Oct 16 in windows 0–4 | 21,938,052 vs 21,939,460 settled at the smaller; 1,712 disputed; 272; the 10 s step declined |
| `street.toml` | Three dishes at $0.10/GB, street links at $0.02/GB; House 1's dish down at window 24 | Working: $2.85, $2.85, 95¢ a day, 35¢ to the Steward. Down: House 2 $7.60, House 1 earns 57¢ and pays $2.00, 48¢ to the Steward; the Steward's 3¢ take of the Wi-Fi's 60¢ posted as a bounty for a path through House 3's hill |
| `cabin.toml` | The cabin's $180 bounty; the ridge relay built at 48; rain, a hiker, a dark day, dark for good; deadline at 1440 | A full window pays 30¢ (11.4¢ ridge, 17.1¢ House 3, 1.5¢ Steward); rain pays 12.5¢ and 5¢ back; the hiker's 4.5¢ reimbursed; the Steward matches $4.50; $60.94 back to the cabin at the deadline |
| `cabin-nomatch.toml` | The same with m = 0 | No match, 720 windows |
| `earth-moon.toml` | 50 GB an hour home at $1/GB, two Mints in one unit | $1,140 a day to the relay, $60 to Earth's Steward; 6 s accepted, 9 s declined; the Mints agree daily |
| `earth-mars-jupiter.toml` | 10 GB a six-hour contact from Europa; conjunction windows 100–183; Mars behind the Sun 140–147 | $50/GB clear, $47.50 kept by the orbiter; $63/GB around Mars ($21.85 Mars, $38 Jupiter, $3.15 to two Stewards); nothing moves in the silence |

## 14. The attacks

`python3 -m sim.attacks wash` sweeps *f* from 0 to 10% and *m* from 0 to *f* and on, with one operator owning payer, relay, sink and Mint, posting a bounty on its own route and serving it, ownership unseen. Net per dollar is *m*(1 − *f*) − *f* while *m* is below *f* − margin (−2.62¢ at the paper's defaults) and flat beyond it, where the cap holds the match back (−0.95¢ at *m* = *f*); no *m* turns it positive. A second table lets the trader build the second path the Steward asks for and collect the bounty on its own traffic: −2.11¢ a dollar at best, because the bounty is *f* of the sole link's fees and the cap stops every payout at *f* − margin of what the trader paid in. A third splits the trader across 1, 2 and 4 payer NYMs: the same figure to the cent. NOTES.md item 14 records the version of the rule under which the trader gained.

`python3 -m sim.attacks monopolist`: a sole bridge at $1/GB answers the Steward's bounty with a second key on the same hill at $0.90. It pulls its own customers over, collects none of the bounty, and earns $91.20 less over two days. The same key on another mapped hill collects $22.80 of the $24 bounty, and is a second bridge.

`python3 -m sim.attacks mint`: during a conjunction with no detour the Jupiter Mint keeps the fee on local traffic; the Earth and Mars Mints find the shortfall at the first reconciliation after, exclude it, and the orbiter bonded on three worlds stops settling there or forfeits two bonds.

Each ends with a summary line of who lost or gained what.

## 15. Files

| | |
|---|---|
| `money.py` | exact amounts and printing |
| `receipts.py` | the Figure 2 tuple and the smaller-count rule, copied from bwkit.py and checked against it |
| `model.py` | domains, places, nodes, links, demands, commitments, payments, settlements |
| `scenario.py` | TOML/JSON scenario files |
| `routing.py` | paths, who pays whom along one, opportunity cost, sole bridges, domain connectivity |
| `engine.py` | the run: events, bundles, settlement, payment, premiums, refunds |
| `steward.py` | the three programs |
| `mints.py` | fee collection, publication, reconciliation, exclusion |
| `report.py` | the flow table and the CSV |
| `attacks/` | wash, monopolist, mint |
| `tests.py` | the headline numbers |
| `NOTES.md` | every ambiguity found, by section |

## 16. What is not modelled

The payment lock and prepayment (every hop pays at settlement); route privacy and forwarding receipts between hops (the payer signs every hop's receipt, as the pilot does); Merkle inclusion proofs; reconciliation as priced traffic; readmission; access points, anchor exchanges and per-payer caps at a place; loss of a bundle in custody; the lifetime of a receipt; governance. NOTES.md says where each of these touches a rule the simulator does apply.
