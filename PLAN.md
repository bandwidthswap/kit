# The build plan: the protocol, formally, and the Steward's tools

Written Oct 18, 2026, against whitepaper v0.6 and bwkit 0.2.0, after the fourth node's first night. The lab runs; this plan turns what it runs into something a stranger can implement, and then builds the one part of the paper that has no code at all, the Network Steward. The last section asks whether this is the best we can do, and what changed because of the answer.

## 0. Where we stand

Two documents describe the protocol today, and they agree only because one person wrote both: the paper's prose (Sections 3 and 4, Appendix A) and `bwkit.py`'s behavior. A second operator, the success criterion of Section 9, cannot implement from either. The kit also lags the paper in three places that the first night exposed:

- The paper says the agreement margin is a parameter of the link (3.6, A.1). The kit hardcodes one percent (`AGREE_PCT`) and 64 KB.
- The paper takes the fraction *f* of every fee for the Steward (3.3). The kit's settlement carries `amount_usd` and nothing else; the invoice sums it; nobody takes five percent. The lab's books have never had a Steward line.
- The paper has link announcements (Section 6, A.1). The kit has none: the price is the Mint's constant, the clock tolerance a constant, and a payer accepts nothing because nothing is offered.

The Steward (Sections 5.1 and 7) exists as prose and as `sim/steward.py`, which runs it over scenarios and found the wash trader's second hill. Nothing on a node runs it.

## 1. The protocol, formally

### 1.1 The specification

`spec/PROTOCOL.md`: **Bandwidth Swap Protocol, pilot profile 1** (`bsp/1`), RFC-style, normative, written from the kit's behavior and the paper's rules. It says, in this order:

1. **Conventions.** MUST and SHOULD; which document wins when the paper and the spec differ (the spec, for the pilot profile; the paper is the argument).
2. **Identities.** A NYM is `ed25519:` followed by the base64url (no padding) of the 32-byte public key. Signatures are Ed25519 over the canonical bytes defined per record.
3. **Encodings.** Canonical JSON: keys sorted, separators `,` and `:`, ASCII-escaped, no whitespace. Byte strings base64url without padding. Hashes SHA-256 as 64 lowercase hex characters. Times are Unix seconds, integers. Byte counts are integers.
4. **The receipt** (paper Figure 2): the eight fields `payer, hop, hop_index, merkle_root, bytes, t_first, t_last, prev_sig`, their types and limits (`check_receipt`), the meter root (what the payer commits to), the canonical form and the signature, the envelope `{kind: "receipt", proto: 1, from, impl, receipt, sig}`.
5. **The acknowledgement.** The same tuple with the hop's `bytes` and `prev_sig` set to the payer's signature, in an envelope `{kind: "ack", ...}`; what the hop MUST check before signing (signature, the clock rule, the chain) and when it MUST decline.
6. **Windows.** An hour on the pilot's links; `t_first` and `t_last` from the payer's clock; the clock tolerance; a declined window is lost and the next ack covers both (3.6); the chain through `prev_sig`; a window is unique by `(payer, hop, t_first)`.
7. **The link announcement** (new; A.1). A record the relay signs and registers with its Mint: `{kind: "link", proto: 1, relay, payer (or "*"), price, unit, carry, clock_tolerance_s, margin, min_bytes, patience_windows, window: "hour", medium, range_class, sig_scheme: "ed25519", receipt_lifetime_s, domain (the Mint's NYM), light_time_s, contact_plan: null, since}`. The parameters in force for a window are those of the newest announcement whose `since` is at or before the window's `t_first`. The pilot's defaults are today's constants: ten cents a gigabyte, zero carry, five seconds, one percent and 64 KB, patience 0 (never decline), an hour, no light time.
8. **Settlement.** `settled = min(payer_bytes, hop_bytes)`; `agree` when `|x − y| ≤ max(min_bytes, margin × max(x, y))` with the link's margin; status `settled` or `disputed`; `amount = settled × price`; **the fee** `fee = f × amount` to the Steward and `amount − fee` to the relay, with *f* a parameter of the domain (five percent in the pilot); the settlement record's fields, including `margin`, `price`, `fee`, `link` (the hash of the announcement used), and, when a purchase applies, `premium` and `refund` (Section 2 below).
9. **The Mint's interface.** `POST /receipt` and `POST /ack` (on the hop) take one envelope; `POST /link` registers an announcement; `POST /purchase` posts a purchase commitment; `GET /status`, `GET /ledger`, `GET /commitments`, `GET /proof?window=`; the error codes; the `User-Agent`. Every write is a signed record, so the Mint authenticates nothing but signatures and the peer list.
10. **The hourly commitment and its proof.** The leaf (`leaf_of`), the tree (`merkle_root`, `_split` at the largest power of two below n), the path format `[["L"|"R", hex], …]`, the commitment record and its canonical signed form, the proof document, what a verifier MUST check (`check_proof`). This section does not change: the kit's 0.2.0 commitments stay valid.
11. **The rebuild.** The rule that anyone with `receipts.jsonl`, `acks.jsonl` and the announcements reproduces the Mint's ledger and its commitments, and what a mismatch means.
12. **Patience** (3.6, A.1). A hop counts, per payer, the windows whose payer count fell short of its own by more than the margin; after `patience_windows` of them it declines that payer's next receipt and reports the payer as declined in `/status`. Declining a window costs the hop that window, so the hop's real remedy is to stop serving, which is outside the protocol; the kit runs a configured hook when it declines (Section 1.2).
13. **Security considerations.** Everything v0.6 admitted: the payer's bounded discount, the padding hop and the lock, what is counted and whose cost the path's loss is, the clock, replay (a window is unique by its key; a Mint rejects a second receipt for the same key with a different signature), and attribution.
14. **Versioning.** `proto: 1` in every envelope. A record without `proto` is profile 1 (the 0.2.0 lab's records stay valid). Profile 2 may change any encoding; a Mint announces the profiles it accepts in `/status`.

### 1.2 bwkit 0.3.0, the reference implementation of the spec

One file still. Changes, each with tests in `test_bwkit.py`:

- **Announcements.** The hop's config gains a `link` object (price, margin, min_bytes, patience_windows, clock_tolerance_s, medium, range_class, carry); `bwkit announce` signs it and posts it to the Mint, which appends it to `links.jsonl`; the Mint's `/status` lists the links it holds. Settlement reads the announcement in force; a settlement with no announcement uses the pilot defaults and says so (`link: null`), so the lab keeps settling during the rollout.
- **The fee.** `f` in the Mint's config (`steward_fee: 0.05`); every settlement carries `fee_usd`; the invoice shows the relay's share and the Steward's line; the Steward's income is the sum of `fee_usd` (Section 2).
- **Patience** on the hop: per-payer counters in its state, the decline, the `/status` report, and `on_decline` in the config, a command run with the payer's NYM as its argument (on the neighbor's Pi: a script that stops advertising the exit node, so declining has teeth; it stops service to every payer, and the lab has one).
- **`proto: 1`** in every envelope the kit writes; readers accept records without it.
- **`bwkit report`** carries, for the latest committed hour, the commitment with its signature, the Mint's NYM and the proofs of that hour's settlements, so a browser can check them (Section 1.4); the Worker's shape check admits the new fields and still refuses addresses.
- **Vectors.** `tools/vectors.py` writes `spec/vectors/`: fixed test keys (marked test-only, never used on a node), a receipt, its ack, the settlement, a disputed one, an hour's commitment with proofs, a rebuild set (`receipts.jsonl`, `acks.jsonl`, `links.jsonl`, the expected `ledger.jsonl`). Deterministic: fixed timestamps, fixed keys. `test_bwkit.py` checks the kit against its own vectors, so a change to an encoding fails a test before it reaches a node.
- **Rollout** on the four nodes with `phase0/provision.sh update` and the neighbor's script, Mint first; the lab keeps running throughout because every old record stays valid.

### 1.3 Conformance

`spec/CONFORMANCE.md` says what an implementation must reproduce from the vectors to call itself `bsp/1`: the signatures, the settlement records byte for byte, the commitment root and signature, every proof, the rebuilt ledger. A conformance run is `python3 tools/conform.py <dir>` over a directory in the vectors' layout that the implementation produced.

### 1.4 The second implementation: a verifier in the browser

`docs/verify.html` and `docs/bsp.js` on the site, no server: canonical JSON, SHA-256 (WebCrypto), the Merkle check and Ed25519 (WebCrypto where the browser has it, otherwise a pinned noble-ed25519 from the CDN). It does two things:

- Paste or open a proof document from `bwkit prove` and a commitment, and it says whether every settlement is in the root and the root is signed by the Mint's NYM.
- Once `bwkit report` carries the latest hour's commitment and proofs (1.2), the Lab status section verifies them on every load and draws a check beside the commitment line: the receipt is the proof, checked in your browser against the Mint's key.

`node tools/check-vectors.mjs` runs `bsp.js` over `spec/vectors/` so the two implementations are held to the same bytes. This is the smallest second implementation that proves the spec is sufficient, and it is the one people will see.

## 2. The Steward's tools

*Built October 18, 2026, kit commits 8543c5c (2.3, `steward_rules.py` shared with the simulator), 5d80ac2 (2.1 and 2.2, purchases and settlement against them), d998fb0 (2.4, the Steward role, its timer, the map), 9bcb170 (2.5, the report's `mint`, `proof` and `steward`), 7dee697 and 77bd0ce (the spec's extension as built, a purchase vector, a purchase governs the window running when it is posted). 2.6 ran on the lab the same morning: the Steward's first run at 04:06 CDT on real fees, the house's purchase posted at 04:06, the first settlement against it at the 05:00 window. What changed on the way: the per-payer cap counts fees in as the gross amounts a payer paid for carriage (the paper's "fees that NYM has paid in", and the simulator's reading), not the sum of `fee_usd` as 2.3 below says; a place counts as linked once a settled window has either end at it; the Steward's income is the sum of `fee_usd`, so the 42 windows settled before 0.3.0 contribute nothing to it, and the lab's Steward started with one window's fee, not the mockup's 0.21¢.*

### 2.1 The purchase commitment, as a record

The paper calls both the Merkle commitment and the bounty a commitment. In the code the bounty is a **purchase**, to keep the ledger readable. The spec's extension section defines it:

```
{kind: "purchase", proto: 1, id, poster, program, route: {from, to, not_via: []},
 min_bytes, premium_per_gb, windows, deadline, proving: null | {windows_per_period, period_s},
 escrow, unit, posted_at, sig}
```

`program` is `payer` (a payer's own bounty), `match`, `second_bridge` or `first_link`; `route.from` and `route.to` are NYMs or place ids, and `route.from` is `null` when any relay may serve it (a first link); `escrow` is the prepaid value in the unit (dollars in the pilot, an invoice line, since there is no rail). A purchase's lifecycle: `open` from `posted_at`; windows settle against it; `closed` when its windows are spent or its deadline passes, with the unspent escrow returned to the poster. The poster signs it; the Steward's postings are signed by the Steward's own key.

### 2.2 Settlement against a purchase (5.1)

When a window's receipt matches an open purchase (its payer is the poster, or the poster is the Steward and the route matches), the Mint applies the rule after the smaller count: rate = `settled_bytes` over `t_last − t_first`; at or above `min_bytes` per window the relay gets the full premium `settled_bytes × premium_per_gb`; below it, the premium in proportion to `settled_bytes / min_bytes`, and the rest of that window's premium returns to the poster. The settlement record carries `purchase`, `premium_usd`, `refund_usd` and who paid the premium (`from_poster`, `from_steward`). The invoice gains a premium column and a refund line. *f* is taken from the premium as from any fee.

### 2.3 The per-payer cap (3.3)

Applied at every payout of Steward money: on receipts whose payer is NYM *p*, the Steward never pays out more than `(f − margin) × fees_in(p)` in all, across programs, where `fees_in(p)` is the sum of `fee_usd` on *p*'s settled receipts. What the cap holds back stays in escrow. The rule is `steward_rules.py`, a file of pure functions (`allowance`, `match_size`, `second_bridge_size`, `first_link_size`) with no node state, imported by both `sim/steward.py` and `bwkit.py`, so the simulator that found the second hill and the Steward on the Mint are one rule. `sim/tests.py` gains a test that the simulator's bench scenario and the kit's Steward, fed the same receipts, post the same purchases.

### 2.4 The Steward on the Mint

A fourth role in the kit, `steward`, run on the Mint's host as a timer (`bwkit-steward.timer`, hourly after the commitment) with its own key, its own state (`steward.jsonl`: income, escrow, postings, payouts, refunds, what the cap held) and the map:

- **The map** is `/etc/bwkit/map.json`, the membership's file in a one-Mint domain: domains with their units and Mints, places with name, domain, medium, range class and `mapped: true` once accepted. The lab's map: domain `earth` (USD, bw-mint), places `house` (the bench's wire), `next-door` (the neighbor's Wi-Fi), and one mapped place with no link yet, so the first-link program has something to want.
- **Income.** Each settlement's `fee_usd` is the Steward's; the role reads the ledger and credits its escrow.
- **Program 1, matching.** When a bonded, aged payer posts a purchase (bond age in the pilot: the peer's `since` in the Mint's peer list), the Steward adds up to *m* of its escrow, *m* below *f*, within *m* of its income in all.
- **Program 2, the second bridge.** Over the program window (a week in the pilot) for every link that carried fees, if the map shows no second path between its ends through a different mapped place, post a purchase for one, sized at *f* of the fees the link carried, premium the link's effective price, route named in places with `not_via` the existing place. A link whose two ends are at the same place, like the bench wire, has no second bridge to post for and is skipped.
- **Program 3, the first link.** Every mapped place with no settled receipt gets a standing purchase sized by `first_link_usd`, posted at what the budget allows and topped up toward its size on each run from what is left, until a receipt settles across a link to it. The simulator's rule gains the same top-up, so the two stay one rule.
- **Refunds** and unspent escrow return to the Steward's escrow and are posted again. The commands: `bwkit steward status | book | run | map`, and for payers `bwkit purchase post | list`.

### 2.5 The Steward's book on the site

`bwkit report` gains a `steward` object: `{nym, f, m, income_usd, escrow_usd, posted_usd, paid_usd, refunded_usd, held_by_cap_usd, purchases: [{id, program, route (names), premium_per_gb, min_bytes, windows_left, deadline, escrow_usd, paid_usd, status}], payouts: [last 24 h: {t, window, payer, hop, premium_usd, from_steward_usd, from_poster_usd}]}`, names only, no addresses. `docs/steward.html`, "The Steward's book", reads the feed: the rule in one line, the balances, the open offers as cards with how to collect each, the payouts, the map. The mockup is at https://claude.ai/artifact/B5ZV4oTawJHLMt5RV7m3Aa, drawn on the lab's real fees to date with the offers and payouts worked by hand, and the page is built from it. Amounts are shown in cents to three places, because at the lab's scale the Steward's whole income is a fifth of a cent, and the page says that the escrow is a line on the invoice, not money in an account.

### 2.6 The first real purchase

The house posts a purchase on the neighbor's link: keep it in use at a minimum of 50 MB a window for a week, premium two cents a gigabyte, escrow a dollar. The Steward matches it up to *m*. The next settlements pay the premium, one falls short and refunds, and the month's invoice shows all of it. That is the roadmap's Phase 2 item, done on the lab.

## 3. Order, sessions and what done looks like

Dependencies: the spec and bwkit 0.3 are one change (every item in 1.1 has a line in 1.2); the vectors need 0.3; the browser verifier needs only the commitment and proof formats, which do not change, so it can start now; the Steward needs the fee and the purchase record from 0.3; the Steward's page needs only the feed shape in 2.5.

| Session | Repo | Builds | Done when |
|---|---|---|---|
| A, now | kit | 1.1 spec, 1.2 bwkit 0.3.0, vectors, 1.3 conformance | tests pass against the vectors; the four nodes run 0.3; a window settles with a fee and a margin from an announcement; the rebuild still matches |
| B, now | site | 1.4 the verifier page against the current proof format, with the vector check | a proof from `bwkit prove` verifies in the browser; a tampered one fails; `check-vectors.mjs` passes |
| C, after A | kit | 2.1 to 2.4, 2.6 | the Steward's timer runs on bw-mint; the book shows income from real fees; the house's purchase is matched and paid; the simulator and the kit agree on the bench scenario |
| D, after B | site | 2.5 the Steward's book page from the mockup, the Lab status check mark | the page draws the live feed's `steward` object; the commitment line shows its check |

Each session commits to its repo's main as the repo's CLAUDE.md says, and never touches the other repo. A and C both edit `bwkit.py`, so they are sequential; B and D both edit `index.html`, so they are too.

Risks: scope, which is why announcements go to the Mint and not to a gossip layer, why places buy nothing yet beyond a name on the map, and why proving allowances are a field the Mint stores and does not pay; the margin's rollout, handled by the default when no announcement exists; the fee appearing on invoices that have been fee-free for two days, which is a change in what the neighbor is owed, so the Steward's share is shown as its own line from the first invoice that carries it.

## 4. Is this the best we can do?

Asked of the plan above before anything was built. What it changed:

1. **The lab has never taken the fee.** The plan started with the margin as the kit's biggest gap. Reading the invoice code showed that nothing takes *f*: the Sybil condition, the paper's one defense, is not in the pilot's books. The fee moved to the top of 1.2 and the Steward's income is defined as its sum, so the first thing the Steward sees is real.
2. **Two things called a commitment.** The Merkle commitment and the bounty share a word in the paper. In the code the bounty is a purchase, and the spec says so in its first line on it.
3. **Where the margin lives.** The first draft put the link's parameters into the receipt. That changes Figure 2 and every signature. Registering announcements with the Mint changes nothing a payer signs, keeps every 0.2.0 record valid, and is what Section 6 says a Mint is in a one-Mint domain. The rebuild gets a third input instead.
4. **Declining has no teeth.** Patience as written in the paper has the hop decline a receipt, which costs the hop the window and the payer nothing. The kit gives the decline a hook that stops service, and the spec says plainly that the remedy is outside the protocol.
5. **One rule, not two.** The simulator's Steward and the Mint's Steward would drift apart in a month. The cap and the three sizings become pure functions in one file both import, and a test holds them together.
6. **The verifier is the second implementation.** A second client in another language would prove the spec and be seen by nobody. A verifier on the status page proves the commitment half of the spec and is the paper's central claim made visible. The receipt half is proved by the conformance runner over the vectors.
7. **What the lab can actually collect.** The second-bridge program will post a bounty on the house-to-neighbor link, but the lab has no third place to build through, so nobody can collect it. The first-link program and the house's own matched purchase are the ones that pay out on the lab. The mockup shows both kinds, and says which is collectible today.
8. **What was cut.** Announcements over gossip, places that buy service through anchors, proving allowances that pay, and a second Mint. Each is in the paper and none is needed to run the Steward on four nodes; each is a line in the spec marked not in profile 1.

Asked again of the mockup, with the lab's real numbers in it:

9. **A link inside one place.** The bench wire runs from the house to the house. Worked by hand, the second-bridge program wanted to post a bounty for a second path around it, which is nonsense: there is no second place to go through. The program skips links whose ends share a place, and the spec says so. The simulator never met this because its scenarios never had one.
10. **The match is the small program at this scale.** The rule caps all matching at *m* of the Steward's income, which on the lab is a hundredth of a cent, so the house's dollar bounty gets matched with 0.008¢. That is the rule working, and the page has to be legible when it does: amounts in cents to three places, every balance adding up on the page, and the card saying why the match is small. A first-link offer posted at what the budget allows would otherwise sit at a hundredth of a cent forever, hence the top-up in 2.4.
11. **What the operator sees is part of the tool.** The mockup's `bwkit steward book` block is the page in text form, so the operator on the Mint and the reader on the site see the same balances; the feed is built from the same function.
