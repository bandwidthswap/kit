# Bandwidth Swap Protocol, pilot profile 1 (`bsp/1`)

Normative. Written October 18, 2026, from the behavior of `bwkit.py` 0.3.0, the reference implementation, and the rules of the whitepaper v0.6 (Sections 3, 4, 5.1, 6, 7 and Appendix A). A second implementation that reproduces `spec/vectors/` under `spec/CONFORMANCE.md` conforms to this profile.

Profile 1 is the pilot: one hop per route, hourly windows, no light time, dollars by invoice, one Mint per settlement domain. Everything the paper makes a parameter of a link is here a field of the link announcement (Section 7); the values the pilot runs with are in Appendix B.

## 1. Conventions

MUST, MUST NOT, SHOULD and MAY are as in RFC 2119. A reader is any implementation that accepts a record; a writer is one that makes it.

Where this document and the paper differ, this document governs profile 1; the paper is the argument for the rules and may run ahead of them. Each such place is marked *Note (paper)* in the text and listed in Appendix A. Where this document and the reference implementation differ, that is a bug in one of them, and the vectors decide which.

The *payer* buys bandwidth and signs receipts. The *hop* (the paper's relay) carries the bytes and signs acknowledgements. The *Mint* keeps the ledger, settles windows and signs commitments. A *peer list* is the set of NYMs a node accepts records from; in profile 1 it is configured by the operator (`peers.json`) and stands in for the paper's bonds.

## 2. Identities

A NYM is the string `ed25519:` followed by the base64url encoding, without padding, of a 32-byte Ed25519 public key (RFC 8032). A reader MUST refuse a NYM that does not decode to exactly 32 bytes.

A signature is Ed25519 over the bytes defined for the record (Sections 4, 7 and 10), encoded as base64url without padding; it decodes to 64 bytes. Every record carries the NYM of its signer in a named field, and a reader MUST verify the signature under that NYM before using anything else in the record. Nothing in this profile authenticates a sender by transport: a Mint and a hop check signatures and their peer list, and nothing else.

Profile 1 has one signature scheme. The announcement's `sig_scheme` field (Section 7) MUST be `ed25519`.

## 3. Encodings

**JSON.** Every record is a JSON object. Readers MUST ignore members they do not know, except where a section says a record has exactly the members listed.

**Canonical JSON** of an object is its JSON text with members sorted by key (by code point of the key string, which for the ASCII keys of this profile is byte order), nested objects treated the same way, arrays in their given order, no whitespace, `,` between members and array elements, `:` between key and value, every non-ASCII character written as a `\uXXXX` escape, and `"`, `\` and control characters escaped as JSON requires. This is what Python's `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)` produces, and the vectors fix it.

**Numbers.** Integers (counts, times, indices) are written as decimal digits with no sign, fraction or exponent, and are at most 2^53 − 1. Non-integers (a price, a margin, an amount, a fee fraction) are IEEE 754 doubles and MUST be written exactly as the reference implementation writes them, which is Python's `repr` of the double: the shortest digit string that reads back to the same double, and

- when the value's decimal exponent *e* (the value written as *d.ddd* × 10^*e* with 1 ≤ *d* < 10) is below −4 or at or above 16, in scientific notation: the digits with a point after the first only when there is more than one, the letter `e`, a sign, and at least two exponent digits: `5e-05`, `1.2e-05`, `1e+16`;
- otherwise in positional notation with at least one digit after the point: `0.0`, `0.1`, `1.0`, `0.011743`.

ECMAScript's `Number.prototype.toString` gives the same digits but formats them differently in three cases, which an implementation in JavaScript MUST correct: an integral value (`1` where this profile writes `1.0`); a magnitude below 10^−4 (`0.00005` where this profile writes `5e-05`, and `1e-7` where this profile writes `1e-07`, since the exponent has at least two digits); and a magnitude at or above 10^16 (positional there, `1e+16` here). Values that are not finite MUST NOT appear. Negative amounts do not occur in profile 1.

**Byte strings** are base64url without padding (RFC 4648 Section 5). **Hashes** are SHA-256, written as 64 lowercase hexadecimal characters. **Times** are Unix seconds, integers. **Byte counts** are integers, and a gigabyte is 10^9 bytes. **Amounts** are US dollars as a number rounded to six decimal places (Section 8).

**Rounding** to six places, `round6(x)`, is to the nearest multiple of 10^−6 of the exact value of the double *x*, ties to even, which is Python's `round(x, 6)`. An implementation that rounds ties away from zero (ECMAScript's `toFixed`) agrees on every value whose double is not exactly halfway between two multiples of 10^−6, which the arithmetic of Section 8 is not expected to produce; a conforming implementation MUST still implement ties to even.

**Signed bytes** are always a domain string followed by a newline and then the record's canonical form, so that a signature over one kind of record can never verify as another:

| Record | Domain string | Canonical form |
|---|---|---|
| receipt, acknowledgement | `bandwidthswap/receipt/v1` | the eight values of Section 4 as a JSON array, in field order |
| commitment | `bandwidthswap/commitment/v1` | `[mint, window, root, count]` as a JSON array |
| link announcement | `bandwidthswap/link/v1` | the canonical JSON of the record without `sig` |

The arrays use the canonical separators and escaping above.

**Files.** A `.jsonl` file is one record per line, each line a JSON object, in the order written; the reference implementation writes each line with sorted keys and a single space after `:` and `,`, which is not canonical and does not matter, since every hash and signature is computed over the canonical form of the parsed object, never over the file's bytes.

## 4. The receipt

The receipt is the paper's Figure 2:

```
(payer NYM, hop NYM, hop index, merkle root, bytes, t_first, t_last, prev receipt sig)   signed by the payer
```

As a JSON object it has exactly these eight members:

| Field | Type | Rule |
|---|---|---|
| `payer` | NYM | the signer |
| `hop` | NYM | the hop asked to acknowledge |
| `hop_index` | integer | the hop's position in the route, from 1; profile 1 has one hop, so it is 1 |
| `merkle_root` | hash | the payer's commitment for the window (below) |
| `bytes` | integer ≥ 0 | the payer's count of bytes through its metered interface in the window, both directions |
| `t_first` | time | the start of the window, on the payer's clock |
| `t_last` | time ≥ `t_first` | the end of the window, on the payer's clock |
| `prev_sig` | string | `""` in a payer's receipt |

A reader MUST refuse a receipt with any other member, a missing member, a type other than the one given (a JSON boolean is not an integer), a negative integer, `hop_index` below 1, `t_first` after `t_last`, a `merkle_root` that is not 64 lowercase hex characters, or a `prev_sig` that is neither empty nor a 64-byte signature.

**The meter root.** Profile 1 has no chunked payload, so there is nothing to build a Merkle tree over. `merkle_root` is the payer's commitment to its own meter readings for the window: the hex SHA-256 of the ASCII string

```
bwkit-meter-v1|<payer NYM>|<t0>|<rx0>|<tx0>|<t1>|<rx1>|<tx1>
```

where `t0, rx0, tx0` are the time and the interface's receive and transmit counters at the start of the window and `t1, rx1, tx1` at its end, as decimal integers. A hop and a Mint check only its shape. *Note (paper):* Section 3.1 makes it the root over the chunks the payer sent; the field keeps that name so the chunked form is a change of content, not of format.

**Signed bytes.** The domain string `bandwidthswap/receipt/v1`, a newline, and the eight values as a JSON array in the order of the table: `["ed25519:…","ed25519:…",1,"…",12345,1800000000,1800003600,""]`.

**The envelope.** A receipt travels as

```
{"kind": "receipt", "proto": 1, "from": "<node name>", "impl": "<implementation/version>",
 "receipt": {…the eight fields…}, "sig": "<base64url>", "meter": {…}}
```

`kind` and `receipt` and `sig` are required. `proto` is the profile (Section 14). `from` is the node's own name, informational: a Mint names the sender by its peer list, not by this field. `impl` names the implementation, informational; envelopes written before this profile carry `bwkit` instead. `meter` is optional and informational: the readings behind the meter root, `{t0, rx0, tx0, t1, rx1, tx1, reset}`, where `reset` is true when a counter went backwards in the window and the raw reading was taken as the count. A reader MUST verify `sig` under `receipt.payer` and MUST refuse an envelope whose `kind` is not `receipt` or `ack`, whose `receipt` fails the table above, or whose `proto` it does not accept.

## 5. The acknowledgement

The hop's acknowledgement is the same eight-field tuple with two changes: `bytes` is the hop's own count for the window, and `prev_sig` is the payer's signature over the receipt (the receipt envelope's `sig`). Every other field is copied from the receipt unchanged. It is signed by the hop over the same domain string and array form, and travels in an envelope with `kind` `ack`, signed under `receipt.hop`. A reader MUST refuse an acknowledgement whose `prev_sig` is empty, and a receipt whose `prev_sig` is not.

When the two counts agree the acknowledgement is the countersignature that closes the window; when they do not it is the dispute, in the same format. Either way the Mint settles from the two signed copies alone (Section 8).

**What the hop MUST check before signing**, in this order, and what it answers when a check fails (Section 9 has the codes):

1. The envelope is a `receipt` whose signature verifies, and `receipt.hop` is the hop's own NYM. Otherwise the request is malformed (400).
2. `receipt.payer` is in the hop's peer list. Otherwise the payer is unknown (403).
3. **The clock rule.** |*now* − `t_last`| ≤ the link's `clock_tolerance_s` (Section 7; the hop reads its own announcement's value, or the default). Otherwise the hop declines (409 `clock`) and the window is lost (Section 6). *Note (paper):* Section 3.6 also has the hop decline a receipt whose timestamps disagree with what it observed; in profile 1 the hop checks `t_last` against its own clock and signs the payer's `t_first` as given.
4. **Patience** (Section 12). A payer the hop has stopped serving is declined (409 `patience`).
5. The hop has a baseline sample for its meter. Without one it takes one now and declines (409); acknowledgements start with the next window.

Then it reads its metered interface, counts the bytes since its own last sample (both directions; a counter that went backwards is read raw), signs, moves its sample to `t_last`, keeps its own copy of the acknowledgement, and returns the envelope to the payer. It SHOULD also send its copy to the Mint itself, so that a payer cannot drop an acknowledgement it dislikes.

## 6. Windows

A window is the stretch of a link's time that one receipt covers. On the pilot's links it is an hour (`window: "hour"` in the announcement): the payer makes a receipt at the top of every hour. Its bounds are the payer's: `t_first` is the time of the payer's previous sample (the previous receipt's `t_last`, or the baseline sample before the first receipt) and `t_last` is now. The payer moves its sample to `t_last` when it signs, before it asks the hop, so a window is never signed twice. Timestamps are the payer's because the payer is the one paying (paper, Section 3.5); the hop checks `t_last` against its own clock under the tolerance and otherwise accepts them.

**A declined window is lost.** When the hop declines (clock, patience, no baseline), the payer's sample has already moved, the receipt goes to the Mint without an acknowledgement and stays unsettled, and nothing is paid for it. The hop's own sample did not move, so its next acknowledgement counts the bytes of both windows; that window settles at the smaller count, which is the payer's (Section 8). The hop loses the window it declined.

**Uniqueness.** A window is identified by the key `(payer, hop, hop_index, t_first, t_last)`. A Mint MUST refuse a receipt whose key it already holds under a different signature, and an acknowledgement chaining to a receipt it already holds an acknowledgement for under a different signature (409 `replay`); it MUST answer a record it already holds, by signature, with 409 `duplicate` without storing it again. The first signed record the Mint received is the one that stands. *Note (plan):* the build plan names the key as `(payer, hop, t_first)`; the reference implementation keys on all five fields and this profile follows it.

**The chain.** `prev_sig` links an acknowledgement to the receipt it answers, and nothing else: a payer's receipts are not chained to the previous window's in profile 1, since the window key makes each unique. *Note (paper):* Figure 2's *prev receipt sig* chains forwarding receipts along a route; with one hop the chain is receipt to acknowledgement.

**Time.** Window bounds are Unix seconds; the pilot's nodes keep time by NTP, and the clock rule of Section 5 is what the protocol rests on, not the time source.

## 7. The link announcement

A relay announces the parameters of each link it offers, and a payer that opens a window accepts them for that window (paper, Appendix A.1). In profile 1 an announcement is a signed record the relay registers with its Mint, which is the registry of a one-Mint domain; nothing in it is trusted for payment, and nothing a payer signs changes.

The record is flat, exactly these members:

```
{"kind": "link", "proto": 1,
 "relay": "<NYM>",              the signer, the hop of every window the announcement governs
 "payer": "*",                  the payer it is for: a NYM, or "*" for any payer in the Mint's peer list
 "price": 0.1,                  the posted price, in `unit`
 "unit": "USD/GB",              US dollars per 10^9 bytes; the only unit in profile 1
 "carry": 0,                    price per second a window's value stays locked (paper 3.6); MUST be 0 in profile 1
 "clock_tolerance_s": 5,        Section 5, step 3
 "margin": 0.01,                the agreement margin, a fraction of the larger count (Section 8)
 "min_bytes": 65536,            the agreement margin's floor in bytes (Section 8)
 "patience_windows": 0,         Section 12; 0 means never decline
 "window": "hour",              the kind of window; the only kind in profile 1
 "medium": "wire",              a lowercase token: wire, wifi, ptp-radio, lora, hotspot, tunnel, or another the operator names
 "range_class": "room",         a lowercase token: room, building, street, valley, orbit, or another
 "sig_scheme": "ed25519",       the only scheme in profile 1
 "receipt_lifetime_s": null,    how long a receipt stays presentable; null is without limit, and a profile-1 Mint records it and does not enforce it
 "domain": "<NYM>",             the Mint the link settles in, by its NYM
 "light_time_s": 0,             one-way light time; MUST be 0 in profile 1
 "contact_plan": null,          MUST be null in profile 1 (a continuous link)
 "since": 1800000000,           the first window start it governs
 "sig": "<base64url>"}
```

`medium` and `range_class` are 1 to 32 characters of `a-z`, `0-9` and `-`. `price` is a number ≥ 0; `margin` a number in [0, 1]; `min_bytes`, `clock_tolerance_s` and `patience_windows` integers ≥ 0; `receipt_lifetime_s` null or an integer ≥ 0; `since` a time.

**Signed bytes.** The domain string `bandwidthswap/link/v1`, a newline, and the canonical JSON of the record without `sig`, signed by `relay`.

**The announcement's hash** is the SHA-256 of the canonical JSON of the whole record, `sig` included. Settlements name the announcement they used by this hash (Section 8), and a Mint refuses a second registration of the same hash as a duplicate.

**Registration.** The relay posts the record to the Mint (`POST /link`, Section 9). The Mint MUST check, and refuse if any fails: the signature under `relay`; `relay` is in its peer list (403); `payer` is `*` or in its peer list; `domain` is the Mint's own NYM (409 `domain`); the profile-1 constants (`unit`, `carry`, `window`, `sig_scheme`, `light_time_s`, `contact_plan`: 409 `profile`); the types above (400); `since` is not earlier than the Mint's clock less its clock tolerance (409 `since`: an announcement cannot be backdated); and the Mint holds no receipt or acknowledgement for that relay, from that payer (or from any payer when `payer` is `*`), whose `t_first` is at or after `since` (409 `since`: an announcement cannot change a window already received). A Mint that accepts the record appends it to `links.jsonl` and answers with its hash. The relay keeps its own copy in its own `links.jsonl`.

**Which announcement is in force.** For a window with payer *P*, hop *H* and start *T*, take the announcements the Mint holds with `relay` = *H*, `payer` = *P* or `*`, and `since` ≤ *T*. The one in force is the one with the greatest `since`; of two with the same `since`, the one with `payer` = *P*; of two still tied, the one with the lexically greater hash. If there is none, the window is governed by the pilot defaults (Appendix B) and the settlement says so with `link: null`.

The future is allowed: an announcement whose `since` has not come governs nothing until it does. A relay changes its terms by registering a new announcement; the old one still governs the windows that started before the new `since`, so the ledger's history never changes under it.

*Note (paper):* Section 6 announces over a gossip layer to peers; profile 1 registers with the Mint, because the Mint is the registry of a one-Mint domain and the rebuild (Section 11) needs the announcements as a third input. Announcements over gossip, contact plans, light time, carry, a receipt lifetime that is enforced, and units other than `USD/GB` are in the paper and not in profile 1.

## 8. Settlement

A Mint settles a window once it holds the payer's receipt *R* and the hop's acknowledgement *A* that chains to it: *A*.`prev_sig` = *R*'s signature, both signatures verify, and every field of *A* but `bytes` and `prev_sig` equals *R*'s. It takes the announcement *L* in force at (*R*.`payer`, *R*.`hop`, *R*.`t_first`) (Section 7), or the defaults, and computes:

```
x = R.bytes,  y = A.bytes
settled_bytes = min(x, y)
agree = |x − y| ≤ max(L.min_bytes, L.margin × max(x, y))
status = "settled" if agree else "disputed"
amount_usd = round6(settled_bytes / 10^9 × L.price)
fee_usd    = round6(amount_usd × f)
```

*f* is the Steward's fee fraction, a parameter of the settlement domain (5% in the pilot). `amount_usd` is the gross amount the payer owes; `fee_usd` of it is the Steward's and the rest is the relay's. The rule is the same whether or not the counts agree: each party can only lower what the other is paid or paid for; the margin decides what is written, never what is paid.

The settlement record:

```
{"kind": "settlement", "proto": 1,
 "t": <the Mint's clock when it settled>,
 "payer", "hop", "hop_index", "t_first", "t_last":  copied from R,
 "payer_bytes": x, "hop_bytes": y, "settled_bytes": min(x, y),
 "status": "settled" | "disputed",
 "rule": "the smaller of the two signed counts; neither party is bound beyond what it signed",
 "receipt_sig": <R's signature>, "ack_sig": <A's signature>,
 "price_usd_per_gb": L.price, "amount_usd": …,
 "steward_fee": f, "fee_usd": …,
 "margin": L.margin, "min_bytes": L.min_bytes,
 "link": <L's hash, or null when the defaults governed>}
```

`rule` is that exact string. When a purchase applies (Section 15) the record also carries `purchase`, `premium_usd`, `refund_usd`, `from_poster_usd` and `from_steward_usd`; a Mint that does not implement purchases never writes them.

A Mint appends the settlement to its ledger and then commits (Section 10). A settlement is final: a Mint never rewrites one, and a window is settled once.

**Settlements written before this profile** (the reference implementation's 0.2.0) have no `proto`, `steward_fee`, `fee_usd`, `margin`, `min_bytes` or `link`. A reader MUST take such a record as a profile-1 settlement under the pilot defaults with no fee taken; Section 11 says how a rebuild compares against one.

**The invoice.** Profile 1 has no payment rail. A Mint sums a month's settlements per (payer, hop): the windows, how many were disputed, the settled bytes, the gross amount, the relay's share (gross less fees) and the Steward's share (the fees), and presents the Steward's share as its own line. *Note (paper):* Section 3.6's lock of a window's value before its bytes move is, in profile 1, the invoice; the fee goes to the Steward as a line on it.

## 9. The Mint's interface

HTTP, JSON bodies, on a private network; the signatures are what protect the records, not the transport. A request body is at most 1 MB (413). A client SHOULD send a `User-Agent` naming its implementation and version (the reference implementation sends `bwkit/<version>`). Answers are JSON objects; an error is `{"error": "<short reason>"}` and MAY add `"detail"`.

**The Mint.**

| Request | Answer |
|---|---|
| `POST /receipt`, body one envelope (Sections 4, 5) or a list of up to 16 | 200 `{"stored": [sigs], "duplicates": [sigs], "settlements": [records], "commitments": [records]}`: what was new, what it already held, what the request settled, and the commitment made after each settlement. A request that stores nothing, settles nothing and only repeats what the Mint holds is 409 `duplicate`. |
| `POST /link`, body one announcement (Section 7) | 200 `{"stored": "<hash>"}`; 409 `duplicate` when it holds that hash |
| `POST /purchase` | Section 15; a Mint without the extension answers 404 |
| `GET /status` | `{"name", "role": "mint", "nym", "impl", "proto": [1], "time", "ledger_lines", "settlements", "commitments", "disputed", "pending": {"receipts_without_ack", "acks_without_receipt"}, "links": [announcements]}`: `proto` lists the profiles it accepts |
| `GET /ledger` | the ledger, `application/x-ndjson`, one record per line in the order written |
| `GET /links` | the announcements it holds, a JSON list in registration order |
| `GET /commitments` | the current commitment of every hour, a JSON list, oldest first (Section 10) |
| `GET /proof?window=<time>` | the proof document for the hour containing that time (Section 10); 404 when the hour has no commitment |

**The hop.**

| Request | Answer |
|---|---|
| `POST /ack`, body one receipt envelope | 200 the acknowledgement envelope (Section 5) |
| `GET /status` | `{"name", "role": "hop", "nym", "impl", "proto": [1], "time", "sample", "outbox", "link", "patience"}`: `link` is its announced parameters (or null), `patience` is Section 12's state per payer |

**Codes.** 400: the body is not JSON, a record is malformed, or a signature fails. 403: the signer, or the payer an acknowledgement names, is not in the peer list. 404: no such path, or nothing to prove. 409: a protocol decline, with `error` one of `duplicate`, `replay`, `clock`, `patience`, `since`, `domain`, `profile`, or another short reason, and `detail` saying why. 413: too large. Every write is a signed record, so the Mint authenticates nothing but signatures and the peer list.

**The outbox.** A node writes every envelope it means to send to the Mint before sending it, and keeps trying; a Mint that was unreachable receives the window later and settles it then (its `t` is then, its commitment lands in the hour the window started in). A node keeps its own signed copies regardless: the payer its receipts and the acknowledgements it received, the hop its acknowledgements, both the Mint's answers.

## 10. The hourly commitment and its proof

This section is unchanged from the reference implementation's 0.2.0, and the commitments that Mint signed stay valid under it.

**The hour** a settlement belongs to is the Unix hour its window started in: `hour = t_first − (t_first mod 3600)`. A window from 21:00:03 to 22:00:02 is in hour 21:00, and a window settled late still lands in the hour it started in.

**The leaf** of a settlement is the SHA-256 of the byte `0x00` followed by the canonical JSON of the settlement record without its `t` member. `t` is the Mint's own clock, which nobody else can know; everything else in the record follows from the two signed receipts and the announcement, so a ledger rebuilt elsewhere makes the same leaf. Pre-profile settlements (Section 8) are hashed as they are, without the members they do not have.

**Leaf order** within an hour: by `(t_first, t_last, payer, hop, hop_index, receipt_sig)`, ascending, strings by code point.

**The tree** is RFC 6962's. An inner node is the SHA-256 of the byte `0x01`, the left child and the right child. The root over *n* leaves: one leaf is its own root; otherwise let *k* be the largest power of two strictly below *n* (so *k* < *n* ≤ 2*k*), and the root is the node over the root of the first *k* leaves and the root of the rest.

**A path** from leaf *i* to the root is a list of `[side, hex]` pairs, innermost first, where `hex` is a sibling root and `side` is `"L"` when that sibling is on the left of the running hash and `"R"` when on the right. To check a leaf *h* against a root: for each pair, *h* ← node(sib, *h*) when the side is `L`, node(*h*, sib) when `R`; the leaf is included when the result equals the root. For one leaf the path is empty.

**The commitment record**, appended to the ledger after every settlement:

```
{"kind": "commitment", "proto": 1, "t": <the Mint's clock>, "mint": "<the Mint's NYM>",
 "window": <hour>, "root": "<hex>", "count": <n>, "signature": "<base64url>"}
```

It is over all the settlements the Mint holds for that hour at that moment, in leaf order. The signed bytes are the domain string `bandwidthswap/commitment/v1`, a newline, and the JSON array `[mint, window, root, count]`. An hour that settles several windows gets a commitment after each, with `count` growing; the newest for an hour is the Mint's current word on it and the ledger keeps them all. A Mint that settled windows before it made commitments (the 0.2.0 lab's first hours) has none for those hours until another window settles in one of them.

**The proof document** for an hour:

```
{"kind": "proof", "proto": 1, "impl": "…", "mint", "window", "root", "count", "signature",
 "settlements": [{"index": i, "leaf": "<hex>", "path": [[side, hex], …], "settlement": {…}}, …]}
```

with the commitment's five fields copied and one item per settlement in leaf order.

**What a verifier MUST check**, with nothing but the document (and the Mint's NYM when it knows it): that `kind` is `proof`; that `signature` verifies under `mint` over `[mint, window, root, count]`, and that `mint` is the expected NYM when one is expected; and for every item, that `settlement.kind` is `settlement`, that its hour (above) equals `window`, that `index` is an integer in [0, `count`), and that the leaf computed from `settlement` climbs `path` to `root`. A verifier MAY also check that `leaf` equals the computed leaf. A changed byte in a settlement, a path or the root fails the check, which is the point.

## 11. The rebuild

Anyone holding the payer's receipts, the hop's acknowledgements and the announcements reproduces the Mint's settlements and the roots it committed to. The inputs are three files in the `.jsonl` form: `receipts.jsonl` (the payer's envelopes), `acks.jsonl` (the hop's envelopes) and `links.jsonl` (the announcements, from either the Mint or the relay), plus the domain's parameters: the default price and the fee fraction *f*, which are not in any signed record.

The rule:

1. Verify every envelope and announcement. A line that is not JSON, a record that is malformed, a signature that fails, or an announcement outside profile 1 is named and left out; so is an envelope whose `kind` does not match its file. (The rebuild has no Mint NYM to hold an announcement's `domain` against; a Mint's registry holds only its own.)
2. Drop exact repeats (the same signature twice). Of two valid receipts with the same window key (Section 6), keep the first in file order and name the other as a replay.
3. Pair each receipt with the acknowledgement whose `prev_sig` is its signature. A receipt without one stays without (the hop declined it); an acknowledgement without a receipt is listed last.
4. Settle each pair by Section 8 under the announcement in force at its `t_first` from `links.jsonl`, with `t` set to the rebuild's own clock, and name a pair that does not settle (the acknowledgement changed a field) as left out.
5. Order the ledger: receipts by `(t_last, t_first, signature)`, each followed by its acknowledgement and its settlement; after the last settlement of each hour, in that order, an unsigned commitment over the hour (`mint` and `signature` null); then the acknowledgements without receipts.

**Comparison** with a Mint's ledger, by window (keyed by `receipt_sig`) and by hour:

- A window settled here and absent on the Mint, or present on the Mint and absent here, is a difference.
- A window on both sides whose leaves differ is a difference, named field by field. When the Mint's record is pre-profile (Section 8), the rebuilt record is compared in that form: its profile-1 members are dropped for the comparison, and it MUST then carry the pilot defaults and `link: null`, or the Mint settled a window without an announcement that was in force, which is a difference.
- For each hour, the root and count rebuilt here (over the leaves as compared, so a pre-profile settlement contributes its pre-profile leaf) against the Mint's current commitment; a Mint commitment whose signature does not verify under its `mint`; and the Mint's own settlements for the hour against its own commitment. Each failure is a difference. An hour the Mint has not committed to is a note, not a difference.

The rebuilt ledger and the Mint's agree when there is no difference. A difference in a window means one side holds a signed record the other does not, or the Mint settled against the rule; a difference in an hour's root with no differing window means the Mint committed to something other than its settlements; a Mint whose own ledger no longer matches the commitment it signed has, in the paper's words, signed its own conviction.

## 12. Patience

A hop keeps, per payer, a count of the windows in which the payer's count fell short of the hop's own by more than the agreement margin: windows where `hop_bytes − payer_bytes > max(min_bytes, margin × hop_bytes)`, with the margin and floor of the hop's own announcement (or the defaults). It counts when it signs the acknowledgement, since that is when it learns both counts. A window where the payer's count is the larger does not count.

When `patience_windows` is greater than zero and the count for a payer reaches it, the hop declines that payer's next receipt (409 `patience`), marks the payer declined, and keeps declining until its operator clears the count. It reports the state in `GET /status` as `patience`: `{"<payer NYM>": {"short": n, "declined": bool, "declined_at": time or null}}`. With `patience_windows` zero, the pilot default, it never declines.

Declining a window costs the hop that window and the payer nothing (Section 6), so the hop's real remedy is to stop serving, which is outside the protocol. The reference implementation runs a configured command (`on_decline`, with the payer's NYM as its argument) the first time it declines a payer, so that an operator can withdraw the service that the receipts pay for. *Note (paper):* Section 3.6 and A.1 give the hop patience as the bound on the payer's discount; this section is that rule, with the plain statement that the decline itself has no teeth.

## 13. Security considerations

Everything the paper admits (v0.6), as it stands in profile 1:

- **The payer's bounded discount.** A payer that signs a count below what it received pays less, and the smaller-count rule allows it. The bound is the hop's patience (Section 12) and the hop's option to stop serving; the protocol does not tell a lie from the path's loss and does not try to.
- **A hop that over-claims** is paid at the payer's count. **A hop that pads** is bounded by the lock of Section 3.6, which in profile 1 is the invoice: it is paid at most one window's value before the payer stops opening windows with it.
- **What is counted** is what crosses the metered interface at each end. The tunnel's headers and every retransmission are the hop's cost and in its price; the margin is the link's and says only what is written. On a relayed path the two counts differ by the path's loss, which the first night off the bench measured at a tenth (paper, Section 9).
- **The clock.** The payer's timestamp is a claim, checked against the hop's clock under the tolerance; a node whose clock has drifted past it is declined and its window lost. A Mint's `t` is a record, not a claim, and is outside every hash and signature.
- **Replay.** A window is unique by its key, a Mint refuses a second receipt for a key under a different signature, and a repeat of a signed record is answered as a duplicate and stored once. A signed record is valid for as long as the announcement's `receipt_lifetime_s` allows, which in profile 1 is without limit.
- **Attribution.** A hop that gives a payer endpoint service puts the payer's traffic on the public internet under the hop's address. The receipt chain records which payer bought the window; what a hop may refuse and how it announces that are not written (paper, Section 10).
- **The Mint.** A Mint cannot forge a receipt or an acknowledgement, cannot settle a window the two parties did not both sign, and cannot settle one against the rule without the rebuild showing it. It can omit a window, which the parties' copies and the commitment show (Section 11), and it can refuse service, which is visible. Its only penalty in profile 1 is its standing with its members.
- **Announcements** are trusted for nothing but the parameters of a window the payer chose to open; a backdated announcement is refused, so the parameters of a settled window never change.
- **The transport** is plain HTTP on a private network. Confidentiality of the records is not a goal of profile 1; their integrity rests on the signatures alone.
- **Keys.** A node's private key is made on the node and never leaves it. The vectors' keys are published and MUST NOT be used on any node.

## 14. Versioning

Every envelope and record this profile writes carries `"proto": 1`. A reader MUST accept a record without `proto` as profile 1 (every record the 0.2.0 lab wrote stays valid and is read under this profile), and MUST refuse a record whose `proto` names a profile it does not accept. A Mint announces the profiles it accepts in `GET /status` as `proto`.

Profile 2 may change any encoding, including the canonical form of numbers (Section 3) and the shape of the settlement record, and will carry `"proto": 2`. What profile 1 fixes for good is what is already signed: the receipt's eight fields and their signed form, the commitment's signed form, the leaf and the tree.

The reference implementation's version is informational (`impl`), and is not the profile.

## 15. Extension: purchases

Not implemented in the reference implementation at this version; defined here so that the Steward's work conforms when it is. The paper calls both the Merkle commitment (Section 10) and the bounty of Section 5.1 a commitment; in records the bounty is a **purchase**.

**The record**, signed by its poster over the domain string `bandwidthswap/purchase/v1`, a newline, and its canonical JSON without `sig`:

```
{"kind": "purchase", "proto": 1,
 "id": "<hash of the canonical record without sig>",
 "poster": "<NYM>",                          a payer, or the Steward's own key
 "program": "payer" | "match" | "second_bridge" | "first_link",
 "route": {"from": <NYM or place id, or null>, "to": <NYM or place id>, "not_via": [place ids]},
 "min_bytes": <integer>,                     the minimum rate, as bytes per window
 "premium_per_gb": <number>,                 the premium above the posted price, in `unit`
 "windows": <integer>,                       how many windows it pays for
 "deadline": <time>,
 "proving": null | {"windows_per_period": <integer>, "period_s": <integer>},
 "escrow": <number>,                         the prepaid value in `unit`; in the pilot a line on the invoice
 "unit": "USD/GB",
 "posted_at": <time>,
 "sig": "<base64url>"}
```

`route.from` is null when any relay may serve it (a first link). The Steward's postings are signed by the Steward's own key; `program` says which rule posted it.

**Lifecycle.** A purchase is `open` from `posted_at`; windows settle against it; it is `closed` when its windows are spent or its deadline passes, and the unspent escrow returns to the poster.

**Settlement against a purchase** (paper, Section 5.1). When a window's receipt matches an open purchase (its payer is the poster, or the poster is the Steward and the route matches), the Mint applies one more rule after the smaller count. The rate is `settled_bytes` over the window; at or above `min_bytes` per window the relay is paid the full premium, `settled_bytes / 10^9 × premium_per_gb`; below it, the premium in proportion to `settled_bytes / min_bytes`, and the rest of that window's premium returns to the poster. The settlement record then carries `purchase` (the id), `premium_usd`, `refund_usd`, and who paid the premium, `from_poster_usd` and `from_steward_usd`. *f* is taken from the premium as from any fee. The invoice gains a premium column and a refund line.

**The interface.** `POST /purchase` registers a purchase with the Mint (200 `{"stored": id}`); a Mint without the extension answers 404. The Steward's per-payer cap, its programs and its map are the Steward's rules, not this profile's, and are specified with it.

## Appendix A. Where this profile departs from the paper

Each is marked *Note (paper)* above.

1. `merkle_root` is the payer's commitment to its meter readings, not a root over chunks (Section 4).
2. The hop checks the payer's `t_last` against its own clock and signs `t_first` as given; it does not compare the window's bounds with what it observed (Section 5).
3. `prev_sig` chains the acknowledgement to its receipt; receipts are not chained across windows (Section 6).
4. Announcements are registered with the Mint, not gossiped; carry, light time, contact plans, enforced receipt lifetimes and other units are not in profile 1 (Section 7).
5. The lock of a window's value is the invoice; the fee reaches the Steward as a line on it (Section 8).
6. A commitment is made after every settlement rather than once per closed hour, is keyed by the hour the window started in, and leaves the Mint's settlement time out of the leaf (Section 10).
7. Patience declines the payer's next receipt and keeps declining until cleared; the decline has no teeth beyond the operator's hook (Section 12).
8. The window key has five fields, not three (Section 6, against the build plan rather than the paper).
9. Amounts are doubles rounded to six places, with the number encoding of Section 3; integer micro-units would be cleaner and are a matter for profile 2.

## Appendix B. The pilot's values

The defaults that govern a window with no announcement in force, and the domain's parameters:

| Parameter | Value | Where |
|---|---|---|
| `price` | 0.1 USD/GB | announcement, or the Mint's configured price |
| `carry` | 0 | announcement |
| `clock_tolerance_s` | 5 | announcement; the Mint uses the same figure for `since` |
| `margin` | 0.01 | announcement |
| `min_bytes` | 65536 | announcement |
| `patience_windows` | 0 (never decline) | announcement |
| `window` | `hour` | announcement |
| `light_time_s` | 0 | announcement |
| `contact_plan` | null | announcement |
| `receipt_lifetime_s` | null (without limit) | announcement |
| `sig_scheme` | `ed25519` | announcement |
| *f*, the Steward's fee | 0.05 | the Mint's configuration (`steward_fee`); the rebuild's `--fee` |
| envelopes per `POST /receipt` | 16 | the Mint |
| request body | 1 MB | the Mint and the hop |
