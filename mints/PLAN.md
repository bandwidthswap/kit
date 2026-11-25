# A second Mint, and reconciliation between two Mints: a plan

Written Nov 24, 2026, against bwkit 0.5.1 (kit e7fac95), `spec/PROTOCOL.md` as it stands that day and whitepaper v0.6 (Sections 4, 7, 9, 10 and Appendix A.5). This is item 9 of the site's `BUILD-ORDER.md`. Nothing here is built and nothing on a node was changed to write it. Section 1 lists where the paper, the spec and the simulator are silent or disagree, in the manner of `sim/NOTES.md`; Section 9 is the decisions that are the owner's, each with a recommendation and none of them taken; Section 8 asks whether this is the best we can do.

## 0. Where we stand

**One Mint settles everything.** `bw-mint` holds the only ledger with windows in it. It takes receipts and acknowledgements from the NYMs in its peer list and refuses every other signer, registers the announcements of relays that name it as their `domain` and refuses any that name another Mint (409 `domain`), settles, commits each hour, writes the invoice, holds `bw-house`'s deposit and countersigns its locks, runs the Steward, and posts the public report. A hop takes a lock only when its own Mint countersigned it. Profile 1 says so in its first paragraph: one Mint per settlement domain.

**What `bw-mint2` is today.** A Pi 3 B made on Oct 19, 2026 with `ROLE=mint phase0/neighbor.sh`: the kit installed as a Mint, its own key (the NYM is in `PUNCH-LIST.md`, "The two new nodes"), an empty ledger, no peers, no `status_url`, no timers. Its LoRa board relays receipts for `bw-neighbor` (`bwradio` 0.2.1), and that relay still names the hop's old address. It has been at the house on the house Wi-Fi since Nov 23, 2026, on the tailnet, and a stronger supply for it is on the build list's blockers. It has never settled a window.

**What is already in place for a second Mint.** The feed, the page and the map took any number of Mints on Oct 19, 2026: a Mint signs its own report and the Worker stores it under its NYM with no registration (`/mints/<NYM>/status`), the map record lists Mints (`map.mints`), the page has a Mint picker for when `/mints` lists more than one. The proof document of an hour (`GET /proof?window=`) and its verifier (`check_proof`, `docs/bsp.js`) already let anyone check one Mint's settlement against that Mint's signed root. The rebuild already reproduces a ledger from the parties' copies. None of that needs to change.

**What the paper promises.** A Mint settles the links of one settlement domain, and a domain is a bookkeeper, not a currency: two Mints that keep their books in one unit are two domains (Section 4). Where a link crosses domains the Mints reconcile their published commitments whenever a link between them allows, and reconciliation does three things (A.5): it is a check (a Mint whose commitments do not reconcile, or that remitted less to its Steward than the fee requires, is shown to have done so from its own publications), a deposit (each Mint includes the other's latest commitment in its own, so a Mint that disappears leaves its register with the rest), and traffic (it crosses the bridge at the bridge's posted price, so withholding it is a declined window on the record). A body or a place enters the map when the Mints accept it at reconciliation. A Mint shown to have cheated is excluded, not slashed. Section 10 leaves the wire format open: what it carries and what it is for are stated, how it is encoded is not.

**What the spec leaves out.** All of it. A link's `domain` is a single NYM and must be the registering Mint's own. Nothing lets a Mint take a record from a signer outside its own peer list, nothing names another Mint, and the map record's note says that until Mints reconcile the map among themselves it is the pilot membership's file. `PLAN.md` cut "a second Mint" by name on Oct 18, and `PUNCH-LIST.md` lists it under "What profile 1 left out".

**What the simulator has.** `sim/mints.py` gives every domain a Mint, settles a link in the one domain the link names, has a relay bonded on both sides carry a copy of each settlement to the other Mint, and at reconciliation audits one thing: the fee remitted against the fee due on the other Mint's published settlements. `sim/attacks/mint.py` is the Mint that keeps the fee while nobody can look and is excluded at the first reconciliation after. Inclusion proofs, the deposit, withheld reconciliation, the audit of a Steward's postings, consensus on the map and readmission are not modelled (`sim/NOTES.md`, items 8, 21, 26, 27, 28).

## 1. Where the paper, the spec and the simulator are silent or disagree

Each item names the place, the gap, and what this plan does about it. Where the choice is the owner's it points at Section 9.

1. **Two things are called "more than one Mint".** Section 4's target of one proposer and four attesters is a committee inside one domain; A.5's domains are one Mint each, reconciling. Item 9 is the second. The committee is deferred by Section 10 and this plan does not touch it: `bw-mint2` is a second bookkeeper, not an attester of the first.
2. **Which Mint settles a link between two domains.** A.1 says a link settles in one domain, "the Mint that holds its bond". A.5 says the relay posts a bond on each side and "either Mint" resolves a dispute from the two receipts. `INSTALLATIONS.md` (the Moon) has both Mints settle the window, each from its half. The simulator settles in the one domain the link names, which in its Earth to Moon scenario is the payer's side (`NOTES.md`, item 32), and mirrors the settlement to the other. The spec has one `domain` per announcement. This plan: the Mint the announcement names settles and is the one whose word on the terms counts; the payer's own Mint keeps the same settlement, record for record, after checking it against the two signed copies (Section 2.4). That satisfies all four readings except A.1's phrase, which for a relay with two bonds has to mean "the Mint its announcement names".
3. **Who is bonded where.** The paper bonds the bridge relay with both Mints and never has a payer pay a relay bonded only elsewhere: value crosses inside the relay, which earns in one unit and pays an onward hop in the other. Profile 1 has one hop per route, so there is no onward hop, and the case item 9 names is the bare one: a payer known to one Mint, a hop known to the other. Something has to stand at the hop's Mint for the payer's bond, and the paper has nothing for it. This plan adds a record for it (Section 2.2; decision 1).
4. **Whose Steward takes the fee on such a window.** Section 3.3 pays *f* to "the Network Steward" and Section 7 implies one Steward per domain; neither says which when the payer and the relay are in different domains. The simulator pays the Steward of the domain the link settles in. This plan follows the simulator (decision 2).
5. **Money between two Mints.** In a shared unit the paper has none: the bridge relay holds both positions. Netting between Mints is named only where no unit is shared and past the horizon (A.5, A.7). A payer whose deposit is at one Mint paying a hop at another puts money owed to the far side in the near Mint's hands, which is a credit between Mints the paper does not describe. This plan leaves it out of the first build and designs it as a late step (Section 2.8; decision 3).
6. **"Each Mint includes the other's latest commitment in its own."** The spec fixes the leaf and the tree for good (Section 14: what is already signed), and a leaf is a settlement. Putting another Mint's root into the hourly tree would change what `count` means and what a proof's items are. This plan makes the deposit a signed record that carries the roots and is chained from one reconciliation to the next (Section 2.5), and records the departure. A root inside the tree is a matter for profile 2.
7. **Reconciliation as traffic.** On the lab both Mints sit on one LAN and one tailnet, and no priced link runs between them. Nothing here meters reconciliation or charges for it. What is kept is the part that makes withholding visible: the asking Mint's own signed, dated statement with no answer to it.
8. **The remittance.** The simulator's Mint collects a fee and remits it, and can keep some. The kit has no such act: the Steward's income is arithmetic over the ledger (the sum of `fee_usd`), on the same machine. So the paper's "remitted less than Section 3.3 requires" can be shown on the lab only as a Steward's book that disagrees with its own Mint's committed settlements. The check is built late and described as what it is (Section 2.7).
9. **The map, accepted at reconciliation.** With two Mints, consensus is two signatures on one file. The paper does not say what a Mint does while the two differ. Today `map.json` is one file on `bw-mint`, `map_record` lists only the Mint that signs it, and the Worker takes the map from a Mint its operator names (`ADMIN_MINTS`). This plan: each reconciliation record signs the hash of its Mint's map, equal hashes are the acceptance, a difference is a mark with no other consequence, and the Worker's rule stays as it is until a later step (Section 2.6).
10. **A place has one domain.** `phase0/map.json` gives each place a `domain`, and nothing in the kit holds a link's `domain` against the domain of the place its hop is at. The lab will have two bookkeepers in one house. This plan adds a domain to the map and no place: two shelves in one room are not two places at the resolution of a radio, which is the only resolution the map claims.
11. **A commitment that changes after it was reconciled.** The paper reconciles "published commitments" as if an hour were closed. The kit commits after every settlement, and a window that waited in an outbox lands in the hour it started in, so an hour's root can change days later (the 51 hours of Nov 21 to 23 are the kind of gap that does it). This plan lists a changed hour again in the next record (Section 2.5).
12. **Exclusion with one operator.** A.5's penalty is the other Mint's refusal. Both Mints here are the owner's, so on the lab it is a mark and a closed door between two processes, forced on a copy to prove the mechanism. Readmission is governance in the paper (Section 10) and a command here.
13. **How often.** "Whenever a link between them exists." On the lab that is always; the plan reconciles each hour, after the commitment.
14. **The audit of a Steward's postings** (Section 7, Execution) is not modelled by the simulator and is not built here.

## 2. The design

Three words first. A node's **home Mint** is the Mint whose peer list holds it; the peer list stands in for the bond (spec, Section 1), so this is where the paper would have it bonded, and a payer's deposits are there. A link's **settling Mint** is the one its announcement names in `domain`; a hop is always a member of its link's settling Mint, because a Mint registers only its own members' announcements. A **crossed window** is one whose payer's home Mint is not the settling Mint of the link it used.

And one word that is taken. The paper calls reconciliation a deposit; in records a deposit is already value entering a payer's balance (Section 16). As the bounty became a *purchase* to keep it apart from the Merkle commitment, the paper's deposit is here the `hours` list of a reconciliation record, and nothing new is called a deposit.

Everything below is an extension of profile 1, as purchases and prepayment are: nothing already signed changes (the receipt's eight fields, the commitment's signed form, the leaf, the tree), and a Mint with no other Mint configured behaves exactly as it does today.

### 2.1 A Mint knows another Mint

A Mint's configuration gains `mints`: for each other Mint it deals with, the NYM, a name, and where to reach it. The address is configuration and never appears in a record, a report or this repository. A Mint reports the NYMs and names in `GET /status`. That is the whole of the trust: an operator's decision, as the peer list is.

### 2.2 The members record: what one Mint says of its own

Signed by a Mint, served at `GET /members`, fetched and kept by every Mint that lists it in `mints`:

```
{"kind": "members", "proto": 1,
 "mint": "<the signer's NYM>",
 "at": <time>,
 "fee": 0.05,                        f, the fee fraction of this Mint's domain: the one input of a settlement that no signed record carries today
 "members": [{"nym": "<NYM>", "name": "<node name>", "role": "payer" | "hop", "since": <time>}, …],   ascending by nym
 "sig": "<base64url>"}
```

Signed bytes: the domain string `bandwidthswap/members/v1`, a newline, and the canonical JSON of the record without `sig`. Its hash is the SHA-256 of the canonical JSON of the whole record, as an announcement's is. Names and roles only; the kit's `assert_public` applies. A Mint signs a new one when its peer list or its fee changes; of two from one Mint the one with the greater `at` is in force, and a reader refuses one whose `at` is ahead of its own clock by more than its tolerance. A Mint keeps the ones it has signed and received in `mints.jsonl`.

What it changes at the door (`POST /receipt`):

- The settling Mint takes a receipt from a signer outside its peer list when the signer is a `payer` in the members record in force from a Mint it lists, the receipt's `hop` is its own member, and an announcement is in force for that payer, hop and `t_first`. With none in force it declines (409 `route`): a crossed window is never settled under the pilot defaults, because the defaults are a Mint's unsigned configuration and the other side could not check them.
- The payer's home Mint takes an acknowledgement from a signer outside its peer list when the signer is a `hop` in the members record in force from a Mint it lists and the acknowledgement's `payer` is its own member. It stores the pair and does not settle it by itself (Section 2.4).

Everything else at the door is unchanged, and a signer that is in neither list is refused as today (403). The hop's own peer list is untouched: a hop still serves only the payers its operator added, whatever their Mint.

The record makes a Mint take signers on another Mint's word. What that exposes: a Mint that lists a payer who then never pays costs the other Mint's relay the windows it carried on credit, which is the exposure the invoice already is, moved one bookkeeper away.

### 2.3 What a payer and a hop do differently

Without prepayment, nothing a payer signs or sends changes, and one line of a hop's does. The payer posts its receipt and the acknowledgement it received to its own Mint, as today. The hop posts its acknowledgement to its own Mint, as today, and with it the payer's receipt, which it has kept since bwkit 0.4.0 and never sent: the settling Mint then needs neither the payer nor the payer's Mint to settle. In a one-Mint domain that second copy is answered as a duplicate, which the outbox already treats as sent.

The payer's home Mint does the carrying. When it stores a receipt of its own payer for a hop that is another Mint's member, it forwards the receipt and the acknowledgement to that Mint's `/receipt`, from an outbox of its own that retries as a node's does. The door there checks signatures and membership, not who knocked, so no new endpoint is needed.

### 2.4 Whose ledger holds the settlement

Both, and they are the same record.

**The settling Mint settles.** It holds the announcement, so it is the only Mint that can say which terms were in force. It settles by Section 8 as it settles any window, writes the record, and commits the hour. Its invoice carries the relay's line, and the fee on the window is its Steward's (decision 2). One rule is new: no purchase governs a crossed window. A premium is drawn from pools in the order a Mint settles, which a second Mint cannot reproduce (spec, Section 15, the note on order), and whether a payer's fees in one domain count toward the Steward's cap in another is a question the paper has not asked.

**The payer's home Mint checks, then keeps the same record.** It holds the two signed copies from its own payer. It gets the settling Mint's settlement (from the answer to its forward when the forward completed the pair, otherwise from that Mint's `GET /proof?window=`), verifies the proof against the settling Mint's signed commitment, fetches the announcement the settlement names by its hash from that Mint's `/links` and checks the relay's signature and `domain` on it, takes *f* from the members record in force at the window's `t_first`, settles the pair itself by Section 8, and compares leaves. When they are equal it appends the settlement to its own ledger with its own `t` and commits its own hour. `t` is outside the leaf, so the leaf in the two hourly trees is the same 32 bytes. When they are not equal it writes nothing and holds a difference (Section 2.5).

The home Mint keeps the settling Mint's announcements for that relay in its own `links.jsonl`, whole, so its rebuild (Section 11) picks the same one in force. A settlement whose announcement names another Mint is a **mirror**, and that is how every reader tells: the home Mint's Steward leaves mirrors out of its income and out of the per-payer cap's fees paid in, its invoice prints the payer's line and says where it settles, and `bwkit owed` on the payer compares against it as it does today. The rebuild takes `mints.jsonl` as a sixth input for the fee.

Why check and copy, and not settle independently on both sides as the installation notes have it: a settlement is final (Section 8), and a home Mint that settled first under a stale copy of the other Mint's announcements could never take it back. The settling Mint goes first; the other side either reaches the same leaf or holds the proof that it should not.

What this buys over a single copy: the payer's Mint needs no second code path for an invoice or a balance, either Mint alone can be rebuilt from its members' copies, and a Mint that disappears leaves its crossed settlements, committed, in the other's ledger.

### 2.5 The reconciliation record

A reconciliation is to two Mints what a receipt and its acknowledgement are to a payer and a hop: a **statement** signed by one, and an **answer** in the same form signed by the other and chained to it.

```
{"kind": "reconcile", "proto": 1,
 "id": "<SHA-256 of the canonical record without id and sig>",
 "mint": "<the signer's NYM>", "with": "<the other Mint's NYM>",
 "through": <hour>,                  the signer has given its word on every hour that started before this one
 "hours": [[<hour>, "<root>", <count>, "<signature>"], …],
                                     the signer's current commitment (spec, Section 10) for each hour listed, ascending
 "crossed": [[<hour>, "<leaf>", "<receipt_sig>"], …],
                                     one entry per settlement in those hours whose payer or hop is a member of `with`, in leaf order
 "members": "<hash of the signer's members record in force>",
 "map": "<SHA-256 of the canonical JSON of the signer's map>",
 "fees": {"due": <integer>, "credited": <integer or null>},
                                     micro-units: the fee on the signer's own links' settlements in `hours`, and what its Steward's book took in from them; null with no Steward
 "prev": "<id of the last record it holds from `with`, or \"\">",
 "answers": "" | "<the sig of the statement this record answers>",
 "at": <time>,
 "sig": "<base64url>"}
```

Signed bytes: the domain string `bandwidthswap/reconcile/v1`, a newline, and the canonical JSON of the record without `id` and `sig`. A statement has `answers` empty; an answer carries the statement's signature, as an acknowledgement carries its receipt's. Amounts are integers in micro-units, as every record since Section 16. A record lists at most 168 hours and 2,000 crossed entries; a longer gap takes several, oldest first.

**Which hours a record lists.** Every hour whose commitment is new or has changed since the signer's last record to that Mint, and every hour the other Mint's latest record listed that the signer has not listed since. An hour stops being listed once each has given its current word on it; an hour that a late window changes is listed again (Section 1, item 11).

**The exchange.** `POST /reconcile` takes a statement and answers with the answer. The answering Mint checks the shape and `id` (400), the signature under `mint` (400), that `mint` is in its `mints` (403), that `with` is its own NYM (409 `domain`), and that `at` is within its clock tolerance (409 `since`); asked again with a statement it has answered, it returns the same answer, as a hop returns the acknowledgement it holds. Both keep both records in `reconcile.jsonl`, served at `GET /reconcile`. The Mint with the lesser NYM asks at ten past each hour; the other asks at twenty to, if that hour brought it no statement.

**What each Mint concludes**, for every crossed window, once it holds the other's current word on the hour:

- **agreed**: both list the window with the same leaf.
- **missing there**: this Mint lists it and the other does not. This Mint sends the two envelopes to the other's `/receipt`, the ordinary door, and the window appears in the other's next record as a changed hour. That is the whole repair, and it is the outbox's case: a Mint that was away.
- **missing here**: the reverse, repaired by the other Mint the same way.
- **differs**: both list the window and the leaves are not the same.

And for every hour with crossed entries it fetches the other's proof document and checks that the listed leaves are items under the root, count and signature that the record gives. A Mint whose record lists a leaf its own commitment does not hold has contradicted itself in two signatures.

**Reconciled through** an hour means every hour up to it that has a crossed window is agreed and proved on both sides. That one figure per Mint is what the page shows.

**The deposit.** Each signed half carries its Mint's roots, and `prev` and `answers` chain the halves, so each Mint holds, under the other's signature and the other's clock, a record that binds the roots it had published by then. A receipt settled on one side is thereby dated on the other, and a Mint that disappears leaves its roots and its crossed settlements with the Mint that remains. It is not a leaf in the other's tree (Section 1, item 6), and the record does not carry the settlements of windows that did not cross: the other Mint learns their hourly roots and counts and nothing else about them.

### 2.6 The map and a Mint's listing

Three separate things today, and the plan keeps them separate.

- **A Mint accepting another Mint** is its own `mints` configuration (Section 2.1).
- **The map agreed between them.** `map.json` gains the second domain with its Mint, and `map_record` builds `mints` from every domain in the file instead of from the signer alone. Each reconciliation record signs the hash of its Mint's map. Equal hashes in a statement and its answer are the two Mints' acceptance of that map at reconciliation, which is the paper's sentence made checkable. Unequal hashes are reported as *the maps differ* and nothing else follows: each Steward reads its own file and spends only its own domain's income. An operator brings them together with a command that fetches the other Mint's `/map`, shows the difference and installs it on a yes; nothing installs a map by itself.
- **A Mint's listing on the public feed** is the Worker's: it lists the Mints named in the map posted by a Mint its operator names. That stays. Once both Mints reconcile, the report each already signs can carry its map hash, and the Worker can say beside the list whether every listed Mint signed the map it is listed by. That is one field and one line on the network page, the Worker's deploy is the owner's, and it is the last step that touches the site (step 12). Whether and when `bw-mint2` is listed at all is decision 5.

### 2.7 What a disagreement looks like, and how either side proves its copy

A crossed window is disputed between a payer and a hop exactly as before, and either Mint resolves it from the two signed counts; that is not a disagreement between Mints. Between Mints there are four, and each is proved from signatures the other Mint made:

| What | What it looks like | The proof |
|---|---|---|
| A settlement against the rule | **differs**, or the home Mint refuses to mirror | the receipt, the acknowledgement, the announcement named, *f* from the members record, and the Mint's own proof item for the window under its signed commitment: anyone settles the two copies by Section 8 and sees the committed leaf is not what they give |
| An omission | **missing there**, still missing in a later record | the two envelopes, and the Mint's signed record whose `hours` cover the window's hour while its `crossed` does not hold it; or its proof document for the hour, every item under the signed root and none of them the window |
| A record against its own commitment | a listed leaf that is not under the listed root | the signed record and the signed commitment, side by side |
| A fee not credited | `fees.credited` below `fees.due`, or `due` below what the hour's proofs sum to | the signed record and the proof documents of the hours it lists |

The first three travel as one document, a **difference**, in the manner of the proof document: the window's key, the two envelopes, the announcement, the fee and the members record it came from, and for each Mint either its proof item with its commitment or its signed record that lacks the window. `bwkit verify-difference <file>` verifies every signature in it, settles the window from the signed copies, and says of each Mint whether its committed leaf is what the copies give. It runs on any machine and asks no Mint, which is the pilot's third clause again: resolved from the receipt data alone.

**Exclusion.** A Mint that holds a difference that verifies against the other Mint stops taking that Mint's members at its door and stops reconciling with it, keeps the document in `reconcile.jsonl`, and says so in its status and its report, where a payer can find it before opening a window. Settled windows stand. Readmission is an operator's command that leaves its own dated record. Nothing is slashed; there is no bond to reach (decision 6 is whether the door closes by itself).

The fourth row is weaker than the paper's and the plan says so: `credited` is the same Mint's own figure, kept on the same machine, and the lab has no account the fee could fail to arrive in (Section 1, item 8). The check shows a book that contradicts its ledger and nothing subtler.

### 2.8 Prepayment across two Mints

Three ways, and the choice is decision 3.

- **Left out.** A crossed window settles on credit, as every window did before Nov 2, 2026. A hop that requires a lock takes only locks its own Mint countersigned, so it declines a payer from another Mint; a crossed link announces `prepay: false`. This is what the first build does whatever is decided, because it costs nothing.
- **The lock stays at the payer's home Mint and is honored across.** The payer locks where its deposit is. The hop takes a lock countersigned by a Mint that its own Mint lists (it reads the list through its proxy). The settling Mint keeps a copy of the lock, shown to it by the hop, with no balance to check, so both settlements carry the same `lock`, `debit` and `covered` and the leaf stays one. A claim goes to the hop's own Mint, which writes the claimed settlement; it reaches the home Mint as a crossed leaf and is mirrored there against the claim's signature. Two things follow that the one-Mint lock does not have. The home Mint cannot release a crossed lock on its own clock: it releases when a record from the settling Mint covers the lock's claim window with no claim in it, so the payer's value stays locked until the Mints next reconcile. And the home Mint then holds money that belongs to the other side: the **position** is the sum of the debits on agreed crossed windows less what has been marked paid, computed from the agreed leaves as an invoice is summed from a ledger, and settled by hand with a paid mark, as invoices are.
- **The payer deposits with the settling Mint.** Then it is that Mint's member for money, nothing is owed between Mints, and the payer is no longer known to one Mint only. It is the paper's two bonds, moved from the relay to the payer.

## 3. The lab

### 3.1 Bench 2, on the kit as it is

A second bench that settles on `bw-mint2` and shares nothing with the live link: not its tunnel, not its hop, not its Mint, not `bw-house`. It needs no code from this plan, which is how the plan gives it early: it is steps 1 and 2 of the build order, ahead of every record above, and it is wanted for its own sake. The forced tests (the two disputes, the clock rule, a forfeiture, an outage) then run there at any minute of the hour, and the house-to-neighbor link carries only its own windows.

| Node | Bench 2 role | Today |
|---|---|---|
| `bw-mint2` | the Mint, and beside it the bench's payer as a second process | an empty Mint; its board relays for `bw-neighbor` |
| `bw-forest` | the hop | the idle bench hop, on bwkit 0.4.0, LAN only |
| `bw-cabin` | spare; in step 10 the board for a third Mint process | idle hop; the HaLow link's far end (build item 3) |

The second payer is a process, not a board: there is no seventh Pi in the handoff. bwkit reads its directory and its configuration from `BWKIT_DIR` and `BWKIT_CONFIG` and its port from the configuration, so two processes can share a board; it has not been run that way on a node, and the kit lacks the units and the install step for a second instance. Where that process runs is decision 4; the table shows the recommendation.

What would have to be set up, plainly:

1. **A way to `bw-forest` that is not through `bw-house`.** It is off the tailnet and has been reached with `bw-house-ts` as the jump, and `bw-house` meters `tailscale0`, so that path adds a session's bytes to the live payer's window (the caution of Nov 23). Jump through `bw-mint2-ts` or `bw-mint-ts` instead (`FOREST_JUMP` takes any node), or join it to the tailnet, which needs a pre-authentication key from the owner. A lab key for the working account goes on it from an account that has one; only the first three keys are there.
2. **`bw-forest` on the current kit**, from 0.4.0.
3. **A tunnel between `bw-mint2` and `bw-forest`** over the house LAN, WireGuard as on the first bench, its keys made on the two nodes, on a subnet of its own beside the first bench's and the HaLow pair's.
4. **The payer process** on `bw-mint2` (working name `bw-bench`): its own directory, configuration, port, key, service and hourly receipt timer, metering the new tunnel.
5. **`bw-forest` re-pointed**: its Mint is `bw-mint2`, by its `.local` name if that resolves there, so no address has to be reserved; its metered interface is the new tunnel; its peer list gains `bw-bench`; it announces its link to `bw-mint2`, naming it as `domain`. Its October windows and its old announcement stay on `bw-mint`, untouched.
6. **`bw-mint2` as a working Mint**: `bw-bench` and `bw-forest` in its peer list, `status_url` and `bwkit-status.timer` so it posts its own report (reachable by its NYM on the feed and listed nowhere until the map lists it), the invoice timer if a month's invoice is wanted. No Steward key yet: the fee is computed and shown as its own line with nobody running the rule, until step 11.
7. **Traffic.** The live link buys a download through an exit node; the bench has none. An hourly push of some tens of megabytes across the tunnel, so its windows are above the 64 KB floor and the margin means something.
8. **The test scripts** taking a payer that is a second process: `dispute-test.sh` and `clock-test.sh` already take the hop, the interface, the target and the Mint.
9. **Power.** A stronger supply for `bw-mint2` is already owed; it now carries a Mint, a payer, a tunnel and a radio.

A bench deposit, for a forfeiture test, is a record that says value came in. On the bench no dollar stands behind it, and its note should say exactly that; whether the owner wants such a record on a Mint whose report is public is theirs to say before step 2.

### 3.2 The first crossed window, off the live Mint

The bench's one link, with its two ends split between two Mints: `bw-bench` stays a member of `bw-mint2`, and `bw-forest` announces its link to a third Mint process (working name `bw-mint3`, its own directory, port and key, on `bw-cabin`'s board), which lists `bw-mint2` and is listed by it. A payer known to one Mint, a hop known to the other, a settling Mint and a home Mint, reconciling each hour, and every forced case run without touching the live ledger or the public page: a window one Mint never received, a changed line in a copy of a ledger, a map edited on one side, a Mint switched off for three hours. Whether to go this way round or straight to `bw-mint` is decision 7.

### 3.3 `bw-mint` and `bw-mint2`

The item is done when the first Mint and the second agree on what crossed between them. By then the kit on `bw-mint` has carried the code for weeks with `mints` empty, which changes nothing it does. What crosses is one of two things, on the owner's word at that step because it touches the live ledger and the public page: the bench's payer made a member of `bw-mint` for a clean week (its windows then appear in `bw-mint`'s ledger as mirrors and in its totals, and are kept off the page's map by the `private` mark of step 9); or the cabin's HaLow link, once build item 3 has given it a window, announced to `bw-mint2`, which makes the house's own payer the one that crosses.

## 4. Build order

Each step is one session and ends with a check that can be run. **bwkit.py** marks a step that edits `bwkit.py` and `test_bwkit.py` and so needs the one writer's hold; **nodes** marks a step that changes a node and so belongs to the session that holds them; the rest can run beside anything. Every code step lands its own spec text and vectors, as prepayment's steps did.

| Step | What | Holds | Check |
|---|---|---|---|
| 1 | **Bench 2's tooling.** Units and an install step for a second bwkit process on one board; the test scripts take it; the hourly push. Kit files other than `bwkit.py`. | nothing | on the Mac, two processes with their own directories and ports serve side by side from a temporary directory, and `sh -n` passes on every script touched |
| 2 | **Bench 2 stands.** Section 3.1's list, on the nodes. | nodes | a window from `bw-bench` to `bw-forest` settles and is committed on `bw-mint2`; `bwkit rebuild --compare` from the two nodes' copies matches its ledger; the feed answers at its NYM; `bw-mint`'s ledger shows nothing new from the bench |
| 3 | **The forced tests move.** Both disputes and the clock rule on bench 2; the build list and the handoff say tests run there. | nodes | the two disputed windows and the declined receipt are on `bw-mint2`'s ledger, each resolved again from the two signed files, and the live link's window of that hour settled within its margin |
| 4 | **The draft.** `spec/MINTS-DRAFT.md`: the records of Section 2 as a future Section 17, every member, signer, domain string and refusal, with the owner's decisions written in. | nothing | no `kind`, domain string or 409 word collides with Sections 3, 9, 15 and 16; every decision of Section 9 has its answer and date in it |
| 5 | **A Mint knows another; the members record; the door.** `mints` in the configuration and in `/status`; the record, `GET /members`, `mints.jsonl`; the two door rules of 2.2; the hop sends the payer's receipt with its acknowledgement. | bwkit.py | `test_bwkit.Mints`: a payer known only to one Mint and a hop known only to the other; the settling Mint settles under the announcement; the home Mint stores the pair and settles nothing; a payer dropped from the next members record is refused; with `mints` empty the vectors and every older test are unchanged |
| 6 | **Forward and mirror.** The home Mint's outbox toward the settling Mint; proof, announcement, fee, compare, append; mirrors out of the Steward's income and cap; the invoice's wording; the rebuild's sixth input. | bwkit.py | the same leaf under both Mints' signed roots for a crossed window; a settling Mint that was away gets the window later and both land it in the hour it started in; `rebuild --compare` agrees on each Mint; a settling Mint whose settlement was altered is not mirrored |
| 7 | **The reconciliation record.** Statement, answer, `POST` and `GET /reconcile`, `bwkit reconcile`, the four conclusions, the repair of a missing window, a changed hour listed again. | bwkit.py | three tests: a clean day agrees and each Mint holds the other's signed half; a window one Mint never received is named, sent, and agreed at the next exchange; a window that arrives a day late reopens its hour and closes it again. `tools/conform.py spec/vectors` passes with the new files |
| 8 | **The difference and the closed door.** The document, `bwkit verify-difference`, exclusion and readmission. | bwkit.py | on a copy with one settlement altered: the document verifies on a machine that holds neither ledger and names the Mint; the other Mint's door refuses that Mint's members; a readmission reopens it and leaves its record |
| 9 | **By itself, and visible.** The hourly timer; *reconciled through* on the Mint's page and in the report; a `private` mark for a peer, which the Worker's plan described on Oct 18 and the kit never built, so a bench node stays out of the public map. Then the site: the Worker admits the new field, the page says the line. | bwkit.py, then the site; the Worker's deploy is the owner's | `dashboard/test.mjs` on a fixture with two Mints; the site's `npm test`; a report from a Mint that reconciles is accepted by the Worker run locally |
| 10 | **The bench run.** Section 3.2 on the nodes: the third Mint process, a day of hourly reconciliation, then the forced cases one at a time. | nodes | twenty-four exchanges all agreed; a blocked forward healed by the next exchange; a Mint off for three hours caught up; a difference made from a copy verified on the Mac |
| 11 | **Fees and the map in the check.** A Steward key on `bw-mint2`; `fees` held against the hours' proofs; the map's hash compared, the command that takes the other's map. | bwkit.py, then nodes | a book short of its ledger's fees is named from the Mint's own records; a place added on one Mint shows *the maps differ* until the other takes it, then agrees |
| 12 | **`bw-mint` and `bw-mint2`.** Section 3.3, on the owner's word: the two Mints list each other, something crosses, the map names both, the Worker says who signed it. | nodes; the Worker's deploy is the owner's | a week of hourly exchanges agreed between the two; the public page shows both Mints and the line; item 9 moves to Done |
| 13 | **Prepayment across**, only if decision 3 asks for it: the hop's wider lock rule, the settling Mint's copy of a lock, the claim's path, release at reconciliation, the position and its paid mark. Then a bench run with `require_lock` on. | bwkit.py, then nodes | a covered crossed window carries the same `lock` and `debit` on both; a claim settles *claimed* on both; a crossed lock is not released until a record covers its claim window |
| 14 | **The words.** The draft becomes Section 17 of `PROTOCOL.md` with its notes in Appendix A, `CONFORMANCE.md` gains the rows, and the paper's sentences go to a proof sheet for approval (Section 6). | nothing | the kit's suite, `tools/conform.py` and the site's `npm test` pass; the cards are exact before and after pairs against the paper's source |

Steps 1 to 4 can start at once and beside any other item. Steps 5 to 9 are five turns with `bwkit.py`, each short, and none of them has to follow the one before on the same day.

## 5. Test vectors and conformance

`spec/vectors/` gains, from two more published seeds in `inputs.json` (a second Mint and a hop that is its member; test-only like the first three):

| File | What |
|---|---|
| `keys.json` | five NYMs where there were three |
| `members-a.json`, `members-b.json` | each Mint's members record, with its hash |
| `link-crossed.json` | the second hop's announcement, naming the second Mint |
| `receipt-crossed.json`, `ack-crossed.json` | a crossed window: the first Mint's payer, the second Mint's hop |
| `settlement-crossed.json` | the one settlement both Mints hold, under the second Mint's fee |
| `commitment-b.json`, `proof-b.json` | the second Mint's hour that holds it; the first Mint's `commitment.json` hour is left as it is, and a new hour carries its mirror beside a window of its own |
| `reconcile-statement.json`, `reconcile-answer.json` | an exchange that agrees, the answer chained to the statement, a changed hour among those listed |
| `difference.json` | the same window with the second Mint's committed settlement altered: a document that must verify and must name the second Mint |
| `rebuild/mints.jsonl`, `rebuild-b/` | the sixth input of the first Mint's rebuild, and the second Mint's own rebuild set with its ledger |

`CONFORMANCE.md` gains a row for each, and `tools/conform.py` a line for each. The new records are an extension, as Sections 15 and 16 are: an implementation with no second Mint still conforms to `bsp/1`, and the runner reports per file. The existing files do not change, which a test holds: with no second Mint in the inputs the kit must still write today's vectors byte for byte. The site's `tools/check-vectors.mjs` gains the difference document once `bsp.js` can settle a window, which is its own small piece and not in the order above.

## 6. What the paper and the spec should then say

Not edited here. The paper's are for a proof sheet, the owner's to approve.

- Section 10's reconciliation wire format has its first answer, and Section 9 its sentence once step 12 has run.
- A.1's "the Mint that holds its bond", for a relay with two (Section 1, item 2).
- A.5: whose Steward takes the fee on a link between domains (item 4); what stands at a Mint for a payer it holds no bond for (item 3); what a Mint does with an hour that changes after it was reconciled (item 11).
- The spec's Appendix A gains its departures: the deposit as a chained record and not a leaf; reconciliation unpriced; the remittance as a book checked against a ledger; no purchase on a crossed window.

## 7. Risks, and what was rejected

**Risks.**

- **The live Mint.** Every rollout reaches `bw-mint`. The new doors open only for Mints named in `mints`, and step 5's test holds that an empty `mints` changes nothing. The rule for rollouts stands: after the hour's receipt has settled and before twenty to the hour.
- **A wider door.** A listed Mint can name any NYM as its member. The two door rules bound what such a NYM can write: only a window that involves one of this Mint's own members. Request sizes and the sixteen-envelope limit are as they are.
- **The mirror waits on the other Mint.** While the settling Mint is away the home Mint holds its payer's two copies unsettled, and its invoice lags by that much. The status says how many wait and for whom.
- **One leaf, two makers.** Any field a later change adds to a settlement must be one both Mints can compute, or the leaves part. Step 6's test is the guard, and it is the reason purchases stay off crossed windows.
- **Hours that reopen.** A long outage lands many old windows at once. The record's limits and the oldest-first rule bound it; step 7 tests a day-late window and not a fifty-hour gap, which the bench run should force once.
- **The public page.** It draws one payer and the hop with the newest receipt. A bench node known to `bw-mint` would redraw it. The `private` mark comes before anything crosses to the live Mint.
- **One operator.** Every Mint here is the owner's, so nothing in the lab is an adversary and exclusion is rehearsed, not suffered. The run shows the mechanism and not that it deters anyone.
- **Boards and power.** `bw-mint2` gains a payer and a tunnel on a supply already marked weak; `bw-cabin` would carry a Mint process during step 10 and is also wanted by the HaLow work.
- **Seven turns with `bwkit.py`** (steps 5 to 9, 11 and 13), one writer at a time, beside every other code item on the list.

**Rejected.**

- **A committee of Mints.** A different thing (Section 1, item 1), and deferred by the paper.
- **The other Mint's root as a leaf, or a domain in the receipt.** Both change what is already signed. The second would alter Figure 2 and every signature, for the same reason the margin went into the announcement and not the receipt.
- **Both Mints settling on their own.** Final settlements made from two copies of one registry (Section 2.4).
- **Only the settling Mint keeping the settlement.** The payer's Mint would need a second kind of invoice line and a second kind of debit, and a Mint that vanished would take its crossed windows with it.
- **Comparing whole ledgers.** Simpler to write, and it hands each Mint every window of the other's members. The record carries roots, counts and the windows that concern both.
- **Both peer lists by hand, with a mark for where a peer settles.** Less code than a signed record and an errand per node; decision 1 keeps it as an option.
- **Reconciling through the status Worker.** A signed record could cross a relay that cannot forge it, and two Mints behind two home connections may one day need that. On the lab they reach each other directly, the Worker is a third party in a check between two, and its deploys are the owner's.
- **Bench 2's payer on `bw-house`**, over the first bench's tunnel, which still exists: nothing new to plumb, and it puts the test bench on the live payer's board, the one with the supply warning and the bytes that arrive wrong. Kept as an option in decision 4.
- **A place on the map for bench 2.** Two shelves in one room (Section 1, item 10).

## 8. Is this the best we can do?

Asked of the plan before any of it is built. What the answers changed:

1. **The bench needs none of the protocol.** The first draft put a working `bw-mint2` after the records. Reading what the kit already does showed that a second Mint with its own payer and hop is configuration and a pair of unit files. It moved to steps 1 to 3, and it is worth doing if nothing after it is ever built.
2. **Check, then copy.** The first draft had each Mint settle from its half, as the installation notes say. A settlement is final and the two Mints hold two copies of one announcement registry, so the first stale copy would have been a permanent difference with nobody at fault. The settling Mint goes first and the other proves the same leaf or holds the evidence.
3. **Payers and hops barely change.** The first draft taught every node about two Mints. The home Mint forwarding its own payer's copies means a payer changes nothing and a hop one line, until prepayment crosses. The records are between Mints, where the paper put them.
4. **A crossed window needs an announcement.** The defaults are one Mint's unsigned configuration; a second Mint could not check a settlement made under them. So the door declines, and *f*, the one remaining input no record carried, went into the members record.
5. **The deposit kept its name for one thing.** Two records called a deposit would have been read as one within a week.
6. **The remittance check is thin and the plan says so.** The simulator's Mint can keep a fee; the kit's cannot fail to remit what it never moves. Building the check as if it caught the simulator's cheat would have been a claim the lab cannot support.
7. **What the other Mint learns.** A whole-ledger comparison was the obvious first cut. A Mint's members did not agree to show a second bookkeeper their windows, and the record does not need them.
8. **What is still not the best.** The listing on the public feed is one operator's file until step 12, and after it still an operator's deploy. Reconciliation is not priced traffic, so the third of A.5's three purposes is not shown at all. Exclusion costs a Mint nothing here that its owner cannot undo. One unit: nothing about a bridge's price as an exchange rate is touched. And the largest: on Nov 1 the kit's punch list said that `bw-mint2` as a Mint answers no question the neighbor link does not already answer. For steps 5 to 13 that is still true until a second operator exists to run one. The bench of steps 1 to 3 is wanted now; the rest earns its keep the day the neighbor, or anyone, keeps their own books.

## 9. Open decisions for the owner

None of these is taken. Each has the options, what each costs, and a recommendation with its reason.

1. **What stands at one Mint for a payer bonded at another.** (a) The members record: each Mint signs the list of its members and the other takes them at its door for crossed windows. It costs one record, two door rules and a vector, and it makes a Mint accept signers on another Mint's word. (b) Both peer lists by hand: every party that crosses is added to both Mints, with a mark saying where it settles. It costs less code and an operator's errand per node, and every crossed party is then known to both Mints, which is not the case the item names. *Recommended: (a).* It is the only one in which a payer is known to one Mint, and the word it rests on is signed and dated.
2. **Whose Steward takes the fee on a crossed window.** (a) The settling Mint's, the one the link's announcement names. Nothing moves between Mints for it, and it is what the simulator already runs. (b) The payer's home Mint's. The payer's fees then fund the Steward where the payer is a member, which is where the per-payer cap counts them, and the settling Mint must leave them out and trust the other to take them in. (c) Split between the two. It costs a rule the paper has no sentence for. *Recommended: (a).* The paper puts a link in one domain, the fee is taken where the settlement is written, and the other side has only to leave it out.
3. **Prepayment when the deposit is at one Mint and the hop at another.** (a) Left out: a crossed window settles on credit and a hop that requires a lock declines a payer from another Mint. It costs nothing and gives a crossed link no cover. (b) The lock stays at the payer's Mint and is honored across (Section 2.8). It costs the largest step in the plan, keeps a payer's value locked until the Mints next reconcile, and puts money owed to one Mint's relays in the other Mint's hands. (c) The payer deposits with the Mint its hop settles in. It costs no protocol and makes the payer a member of both. *Recommended: (a) now, and (b) as step 13 only when a crossed link wants to require a lock.* The first twelve steps do not depend on it, and the credit between Mints that (b) creates is something the paper should speak to before the kit does.
4. **Where bench 2's payer runs.** (a) A second bwkit process on `bw-mint2`'s board, with a new tunnel to `bw-forest`. It costs a tunnel and leaves the live payer, the live hop and the live Mint untouched; the Mint and its payer share a board and a weak supply. (b) A second process on `bw-house`, over the first bench's tunnel, which still exists. It costs no plumbing and puts the test bench on the live payer's board. (c) A seventh board, if one is on the shelf. It costs a Pi, a card and a supply, and is the cleanest. *Recommended: (a), or (c) if the board exists.* The point of the second bench is that tests stop touching what the live link runs on.
5. **Whether `bw-mint2` is listed on the public feed, and what its domain is called.** (a) Unlisted until step 12, then listed by a map both Mints have signed. Its report is reachable by its NYM meanwhile, and the public page stays one Mint's. (b) Listed as soon as bench 2 stands. The Mint picker and the network page then show a bench whose business is forced disputes. (c) Never listed: a bench is not the network. The name is the owner's either way; `bench` is the working one. *Recommended: (a).* The first map to list two Mints would then be one accepted at a reconciliation, which is what the paper says a listing is.
6. **Whether a proven difference closes the door by itself.** (a) By itself: a Mint that holds a document that verifies against another Mint stops taking its members and stops reconciling, and an operator's command reopens it. (b) Only on an operator's command; until then the Mint reports the difference and goes on. *Recommended: (a).* A difference is either a cheat or a bug in the kit, and on a lab both should be loud; the command that undoes it leaves a record.
7. **Where the first reconciliation runs.** (a) On bench 2 alone, with a third Mint process on `bw-cabin`'s board standing in for the other side, and `bw-mint` joining last (steps 10 and 12). It costs one more process on a bench board and keeps every forced case off the live ledger and the public page. (b) Between `bw-mint` and `bw-mint2` from the start. It is one step shorter, and every forced case then writes into the live Mint's ledger and totals. *Recommended: (a).* The same reason as decision 4, and `bw-mint` then meets code that has already run a day of exchanges and each failure once.

## 10. The seven decisions against the paper (session `lab`, Nov 24, 2026)

The owner asked for the decisions of Section 9 to be held against the white paper, so that the two-Mint design keeps to it and builds no lasting imbalance of money between Mints. This section is that review. **It is a proposal: no decision is taken, and Sections 2 to 9 above are not yet revised for it.** Read for it: the paper's Sections 2, 3.3, 4, 5, 5.1, 6, 7, 8 and 10 and Appendix A (v0.6). Not re-read for it: Sections 1, 3.1, 3.2, 3.4 to 3.8, and 9.

| # | Decision | What the paper says | The review |
|---|---|---|---|
| 1 | Accepting a payer registered at another Mint | Nothing. Its only crossing party is a relay bonded on both sides (A.5) | Change, with 3, below |
| 2 | Whose Steward takes the fee on a crossed window | Nothing on which Steward. The fee is a ratio that crosses unchanged (A.5) | Change: split it. **The owner, Nov 24: it should be split across those Mints by the difficulty of the hop** |
| 3 | Prepayment across Mints | The pilot is on the prepaid side; no money moves between Mints in a shared unit (A.4, A.5) | Change, with 1, below |
| 4 | Where bench 2's payer runs | Nothing | Holds |
| 5 | Listing `bw-mint2` on the public feed | A domain enters the map only when the Mints accept it at reconciliation (A.5) | Holds; the other options would break this |
| 6 | A proven difference closes the door by itself | Exactly this: publish the proof, stop reconciling (A.5) | Holds, with one fix, below |
| 7 | Where the first reconciliation runs | Nothing | Holds; it keeps forced cases off the ledger that bills real money |

**Decision 2, the fee.** The Steward's income is the only money that pays for second bridges and first links (Section 7). If the relay's side takes the whole fee on every crossed window, a domain that mostly pays funds building only where the relays are, and its own Steward goes without. The paper never says which Steward takes the fee when the payer and the relay are in different domains, so a split contradicts nothing in it. It does fix four things a split must keep:

1. The shares add up to exactly the fee fraction *f*. No more and no less leaves the payer's reach (Section 3.3).
2. Each Steward's cap per payer counts only what that Steward received, or a payer could be paid back more than was taken.
3. The weights come from settled receipts and the map, never from what a relay announces (Section 6 forbids trusting an announcement for payment), so a relay's stated medium or range cannot be the measure of difficulty.
4. No balance builds up between the two Mints: each bills its own Steward's share on its own invoice.

What *difficulty* can mean under the paper: its measure is opportunity cost, the fewer other paid paths around a hop the harder the hop (Section 5), and it uses that only to divide a Steward's spending among relays, saying outright that it is not a price. Two things follow. On a single hop between two Mints the difficulty is one number, the same from either end, so a split by difficulty of one crossing hop is an even split. On a route of several hops in different domains, the fee on each hop goes to the Steward of the domain that hop settles in; a sole bridge charges more, so more flows to where the hard hop is, with nothing new to measure. The proposal ties the split to each hop's own fee and not to a separate difficulty score, because the second-bridge bounty is defined as the Steward's income on that link (Section 7) and a separate score would break that equality. Before it is adopted: the simulator's three attacks run against the split, and a sentence for the paper goes to the proof sheet, the standard the paper holds its own fee figures to.

**Decisions 1 and 3 together, the risk in money.** The recommended pair makes unsecured credit across Mints: decision 1 lets a hop at one Mint serve a payer on the other Mint's signed word, decision 3 leaves a crossed window on credit, and so a relay lends up to a month to a payer whose bond only the other Mint holds, where the paper says plainly that no Mint can reach another's bond. The plan's later remedy, honouring a lock across Mints, leaves one Mint holding money owed to the other's relays and settled by hand, a standing imbalance by design. The proposal is the plan's third option for decision 3: **the payer deposits with the Mint its hop settles in.** Nothing is then owed between Mints, the hop is covered, and it is the paper's own model of a party with standing on both sides. The members record can stay for identity; it should not stand in for money.

**Decision 6, one fix.** The paper lets an excluded Mint back only when it publishes a commitment that reconciles and has remitted what it owed (A.5). The plan reopens the door on a bare operator command. The command should check both conditions, or a debt can be forgiven with a keystroke.

**Two gaps that matter for money.**

- *Nobody audits a Steward's spending.* The paper has a Mint's Steward postings audited at reconciliation (Section 7). The plan does not build that and the simulator does not model it. Once fees are split across Mints it is the check that stops a Mint misusing Steward money, and it should be built before the live Mint joins (before step 12).
- *Exclusion has no teeth in money on the lab.* The paper's penalty rests on a bridge relay's two bonds, and the pilot holds no bonds. That is acceptable for a bench; the plan should say so.

**What revising the plan would touch,** once the owner decides: Section 2.4 (whose ledger and whose Steward), 2.5 (the reconciliation record gains each Steward's share and the audit of postings), 2.8 (prepayment: a deposit at the settling Mint, not a lock honoured across), Section 4's steps for those, Section 6's sentences for the paper, and Section 9's recommendations for decisions 1, 2, 3 and 6.
