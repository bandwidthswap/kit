# Prepayment of a window: a design (draft, not built)

*Written Nov 1, 2026, as the operator track closed. This is backlog B2, option (b), the paper's Section 3.6 as a mechanism, drafted as a future Section 16 of `PROTOCOL.md`. Nothing here runs. The decisions it leaves to the owner are in the last section; the build order is the one before it.*

## 1. The gap it closes

The paper says that before a window's bytes move the payer locks the window's value, so a relay never fronts more than one window of bandwidth on credit, and a payer who consumes a window and leaves forfeits what was locked (Section 3.6). The pilot has no payment rail, so the paper lets the invoice be the lock and membership bound the exposure (Section 2): the relay lends every window for up to a month and an hour, and the only remedy against a payer who leaves is that it is out (Section 4). The kit implements exactly that: a receipt, an acknowledgement, a settlement, an invoice, and a hop's patience that can decline a receipt but, as Section 12 says plainly, has no teeth. `PROTOCOL.md` Appendix A, item 5, marks the lock as the one place the pilot departs from the paper's mechanism rather than its words.

The backlog's default for B2 is (b) with hourly windows, and its own note is that the throughput cost of (b) can only be measured by running it. This design is the smallest thing that runs it on the lab's links without a rail: value held at the Mint, a signed lock per window registered before the window opens, a hop that serves a window only when it holds the Mint's word that the window is covered, a settlement that draws on the lock, and a claim that lets a hop keep what a vanished payer locked. Each piece is a record in the registry the purchases already use, so the rebuild (Section 11) reads them the same way.

## 2. What is already there

- **A pool the Mint draws on.** A purchase (Section 15) holds `escrow`, "the prepaid value; in the pilot a line on the invoice", and the Mint draws a settlement's premium from it and returns the rest when it closes. The accounting a lock needs, a balance, draws, returns, is the same shape with one more kind of record.
- **The hop's gate.** The hop already declines a receipt (409 `patience`) and runs `on_decline` the first time it does, so an operator can withdraw the service (`bwkit-on-decline` drops the exit node). A lock gives that gate a reason that is not a count after the fact: no cover, no window.
- **The payer's hourly job.** `receipt_run` makes the receipt, gets the ack, flushes to the Mint. The lock for the next window belongs at the end of it, where the payer already knows the Mint answered.
- **The registry.** `POST /purchase` takes three kinds (`purchase`, `top-up`, `close`), checks each by its signature and its `posted_at` against the clock, appends to `purchases.jsonl`, and `GET /purchases` serves the file. Locks are a fourth and fifth kind in the same file, or a sibling file with the same rules; the draft takes a sibling, `prepay.jsonl`, so the purchase book stays what Section 15 says it is.

## 3. The records

All four are signed over a domain string, a newline, and the canonical JSON of the record without `id` and `sig`, with `id` the SHA-256 of those bytes, as purchases are. Amounts are in `unit`, USD only in profile 1, doubles rounded to six places (Appendix A, item 9 stands; the switch to integer micro-units is a profile 2 question this does not reopen).

**Deposit** (`bandwidthswap/deposit/v1`), signed by the Mint. Value entering a payer's balance at the Mint.

```
{"kind": "deposit", "proto": 1, "id", "mint": "<the Mint's NYM>", "payer": "<NYM>",
 "amount": <number>, "unit": "USD", "posted_at": <time>, "note": "<how it came: the parties' words>", "sig"}
```

The Mint signs it because in the pilot the money moves outside the protocol, as the invoice is paid outside it: the payer hands the Mint's operator dollars, and the Mint writes that it holds them. The note carries the parties' words, as the paid mark of an invoice does. A deposit never carries an address or a name, only NYMs; `assert_public` applies to the report that mentions it.

**Lock** (`bandwidthswap/lock/v1`), signed by the payer, countersigned by the Mint. The payer's reservation of one window's value with one hop.

```
{"kind": "lock", "proto": 1, "id", "payer", "hop", "t_first": <the window's start>,
 "amount": <number>, "unit": "USD", "carry": 0, "link": "<hash of the announcement in force, or null>",
 "posted_at": <time>, "sig": "<the payer's>", "mint": "<NYM>", "mint_sig": "<the Mint's, over the record with sig>"}
```

`t_first` names the window: the moment the payer's next receipt will carry as its `t_first`, which is the `t_last` of the receipt it has just made (Section 6; windows are back to back). `amount` is the payer's estimate of the window's value at the link's price, plus the carry for the time it stays locked; on the pilot's links the carry is zero (paper, 3.6) and the field is there for the link whose window closes a day after it opens. `link` names the announcement so the lock and the settlement are priced under the same terms.

**Claim** (`bandwidthswap/claim/v1`), signed by the hop. A hop's count for a window the payer never closed.

```
{"kind": "claim", "proto": 1, "id", "hop", "payer", "lock": "<lock id>", "bytes": <integer>,
 "meter": {t0, rx0, tx0, t1, rx1, tx1, reset}, "posted_at": <time>, "sig"}
```

**Release** is not a record. What a lock holds and the settlement did not draw returns to the balance when the window settles, or when the lock expires unclaimed, by the Mint's own arithmetic, exactly as a purchase's unspent pool returns.

## 4. The Mint

**Registration.** `POST /prepay` takes a deposit, a lock or a claim. For each the Mint checks the shape and `id`, the signature, and `posted_at` against its clock less its tolerance (409 `since`), as `POST /purchase` does. For a deposit, that it is the Mint's own (403); the operator posts it with `bwkit deposit <payer-name> <amount> "<note>"` on the Mint's host, which signs and appends in one step. For a lock, that `payer` and `hop` are peers (403), that `t_first` is not earlier than the clock less the tolerance and not later than the clock plus one window (409 `since`: a lock covers the window about to open, not a window past or far off), that no lock from this payer for this hop and this `t_first` is held (409 `duplicate`), that `link` is the hash of the announcement in force at (`payer`, `hop`, `t_first`) or null when none is (409 `route`), and that the payer's balance covers `amount` (409 `balance`, with the balance in `detail`). It countersigns, appends, and answers 200 with the countersigned lock, which is what the payer carries to the hop. For a claim, that it holds the lock, that the lock's window has had no settlement, that `posted_at` is at least `lost_after_s` (two windows) after the lock's `t_first` (409 `since`: the payer's receipt may still be in its outbox), and that no claim is held for that lock (409 `duplicate`).

**The balance.** `balance(payer) = Σ deposits − Σ amounts of open locks − Σ debits`, where a debit is what a settlement or a claim drew. `GET /balance?payer=<NYM>` answers `{"payer", "deposits_usd", "locked_usd", "debited_usd", "balance_usd", "open_locks": [{"hop", "t_first", "amount"}]}`; `GET /prepay` serves `prepay.jsonl` as `GET /purchases` serves its file; a hop and a payer proxy both at `/mint/balance` and `/mint/prepay`, as they proxy the ledger.

**Settlement.** Section 8 is unchanged in what it computes. After it, the Mint finds the lock held for (`payer`, `hop`, `t_first`): with one, the settlement gains `lock` (its id), `debit_usd = amount_usd + premium_usd` drawn from the balance (the lock is released; the payer's balance may go below zero when the window was worth more than it locked, and the next lock then fails), and `covered: true`; with none, `lock: null`, `debit_usd: 0`, `covered: false`, and the window is on credit as every window is today. The invoice gains, per payer, a prepayment block: deposits in the month, debits, the balance at the month's end, and the carriage not covered by a lock, which is what the payer owes beyond its balance. `bwkit owed` compares that block too.

**Expiry and the claim.** A lock whose window has no settlement two windows after its `t_first` (`lost_after_s`, the same clock the health check uses for a lost window) is expired: without a claim its amount returns to the balance, the payer locked and nothing moved. With a claim the Mint writes a settlement of kind `settlement` with `status: "claimed"`, `payer_bytes: null`, `hop_bytes` the claim's, `settled_bytes = min(hop_bytes, amount / price × 10^9)` (the hop is paid for what it counted, up to what was locked), the fee as on any settlement, `receipt_sig: null`, `ack_sig: null`, `claim_sig` the claim's, and `debit_usd` its amount. This is the forfeiture the paper promises: a payer who consumes a window and leaves pays for it, up to the lock, on the hop's signed count alone. It is also the one place the rule "neither party is bound beyond what it signed" bends, since the payer signed the lock and not the count; the lock's amount is the bound, and the paper's 3.6 is the warrant.

**The rebuild.** `rebuild` takes `prepay.jsonl` beside `purchases.jsonl`, checks every record by its signature and the Mint's countersignature, and recomputes balances, debits, expiries and claimed settlements in window order, so a Mint that lost its ledger recovers the prepayment book from the parties' copies and the registry. A payer keeps its own locks in `locks.jsonl` (its copy, with the Mint's countersignature); a hop keeps the locks it was shown and its claims.

**The report.** `status_record` gains, per payer node, `prepaid: {balance_usd, open_locks}` (names only; `assert_public` already refuses an address), and the fleet's Position gains "locked ahead" beside what was paid.

## 5. The payer

At the end of `receipt_run`, after the flush: estimate the next window's value, `amount = round6(estimate_bytes / 10^9 × price) + carry`, with `estimate_bytes` the larger of the last settled window's bytes and the purchase minimum in force (`min_bytes` of the governing purchase, 50 MB on the house's hourly fetch), times a headroom the config sets (`prepay.headroom`, 1.25 by default), and `price` the announcement's; post the lock to the Mint; on 200, present the countersigned lock to the hop (`POST /lock` on `hop_url`); on 409 `balance`, log the shortfall, run `on_short` if configured, and send no lock, so the hop declines the next window if it requires one. `bwkit lock --print` shows what would be locked; `bwkit balance` reads the Mint's `/balance` through the proxy and prints deposits, locks, debits and the balance; `bwkit locks` lists the payer's own copies.

The hourly job is `bwkit receipt`, so the lock rides on the timer that exists: one more POST to the Mint and one to the hop per window, about 600 bytes each. On the tailnet that is two round trips of about 100 ms; by radio (the carrier, `radio/wire.py`) a lock is one more packet per window per link, about two seconds of airtime at LongFast, and the countersigned lock back is another, which the carrier delivers as it delivers an ack. That is the throughput cost B2 asks to measure, and the lab's two paths measure it.

## 6. The hop

`POST /lock` takes a countersigned lock: the hop checks the payer's signature, the Mint's countersignature under the Mint's NYM it already holds (`mint_nym`), that `hop` is itself and `t_first` is the window about to open, keeps it (`locks.jsonl`), and answers 200 `{"held": id}`. `GET /status` gains `prepay: {"require_lock": bool, "covered": {"<payer NYM>": <t_first of the lock held for the coming window, or null>}}`.

With `require_lock` false, the default, the hop serves as it does today and the locks are bookkeeping; the pilot's links keep running through the rollout. With it true, at each window's start the hop that holds no lock for a payer declines that payer's next receipt (409 `lock`) and runs `on_decline` with the payer's NYM, the hook that already withdraws the exit node, and offers it again when a lock arrives (`on_cover`, a second hook; the sudoers line gains `--advertise-exit-node=true`). A hop announces `prepay: true` in its link block (a new announcement parameter, profile 1 constants unchanged) so a payer knows the link wants locks before it is declined.

The claim: `bwkit claim <t_first>` on the hop, or the daemon by itself two windows after a lock's window with no receipt (`prepay.claim_after_s`, `lost_after_s` by default), posts the hop's own count for that window from its meter, as its ack would have carried it. The hop's patience (Section 12) is untouched; a lock and a claim are what give its decline the teeth Section 12 says it lacks.

## 7. The dashboard

The node view's My month gains the balance and the lock for the coming window on a payer (`locked ahead: $0.0125 for the 19:00 window`), and on a hop which payers are covered for the coming window; a settlement row shows `covered` or `on credit` beside its status; the Mint's view gains a Prepayment section (balances per payer, open locks, claims) and the invoice its block. Everything draws from `/balance`, `/prepay` and the settlement's new fields; a snapshot carries `prepay` as it carries `purchases`.

## 8. What it answers, and what it does not

It answers B2(b) as the backlog asks: the lock runs, the cost per window is two small round trips, the relay's exposure is one window plus whatever the window ran over its lock, and a payer who leaves forfeits what it locked. It gives Section 12's patience its remedy. It does not answer B8: value at the Mint is custody, and the paper's Section 2 reserves the question of whether a Mint can be a hub without becoming a custodian for the day a second party that does not trust the first joins. On the pilot the deposit is dollars between neighbors held by the house, which runs the Mint; the design says so and does not dress it up. Nor does it bond the payer: the paper's "bounded by the payer's bond" is Section 4's membership, which is still the word of the people in the room.

## 9. Build order

Each step is one PR to `main` with its tests, on the convention of this repo, and the lab runs through all of them because every new behavior is off by default.

1. **The records and the registry.** `spec/PROTOCOL.md` Section 16 from this draft, the four domain strings, `check_*`/`verify_*` for each record, `POST /prepay`, `GET /prepay`, `GET /balance`, `bwkit deposit` on the Mint, `prepay.jsonl`; vectors in `spec/vectors/` and the site's `check-vectors.mjs` extended. Tests: shape, signature, clock, duplicate, balance refusals; a deposit and a lock round trip.
2. **Settlement and the invoice.** The debit and release in `receive`, `lock`/`debit_usd`/`covered` on the settlement, the expiry, the claimed settlement, the prepayment block on the invoice, `bwkit owed` comparing it, and `rebuild` over `prepay.jsonl`. Tests: a covered window, an uncovered one, a window over its lock, an expiry that returns, a claim that pays the smaller of count and lock; the rebuild matches the Mint.
3. **The payer.** The lock at the end of `receipt_run`, the estimate and headroom, `bwkit lock --print`, `bwkit balance`, `bwkit locks`, the payer's `locks.jsonl`. Tests on the lab's payer: a lock after every receipt, none when the balance is short, the hop shown the countersigned lock.
4. **The hop.** `POST /lock`, `locks.jsonl`, the `prepay` status, `require_lock` and the two hooks, the `prepay` announcement parameter, the claim command and the daemon's own claim. Tests: declined without a lock when required, served with one, the claim two windows after a receipt that never came.
5. **The page and the report.** Section 7 above, with `dashboard/test.mjs` over a fixture that carries locks; `status_record`'s `prepaid`.
6. **The lab.** Deposit a dollar for `bw-house` on `bw-mint`, roll the kit with `require_lock` off and watch a day of locks beside the receipts (the balance falls by the hourly carriage, the invoice's block matches), then turn `require_lock` on at the neighbor's with the `prepay: true` announcement, then the forfeiture test: stop the house's receipt timer for two windows and watch the hop's claim settle. The paper's 3.6 and the backlog's B2 get their numbers from `phase0/link-report.sh` with the lock's round trips added to its columns.

## 10. Decisions for the owner before step 1

*One of the four is decided; the other three are open.*

- **The lock's size.** The draft's estimate (the larger of the last window and the purchase minimum, times 1.25) is a guess; too small and every window runs over its lock onto credit, too large and the payer's dollar locks more than it needs. The link report's week of window sizes says what headroom the house's fetch needs.
- **Custody. Decided Nov 1, 2026: dollars at the Mint.** A dollar handed to the house and written by `bw-mint` as a deposit is the pilot; the mechanism runs on real value, and the Mint is a custodian of it, which Section 8 says and the paper's B8 keeps open for the day a second party joins. (The alternative was a signed promise with nothing held, the invoice as lock as now, which would have measured the same cost and raised no custody question.)
- **The claim's bend.** A claimed settlement pays a hop on its own count, bounded by the lock. If that bend to "neither party is bound beyond what it signed" is not wanted, the alternative is forfeiture without payment: the lock's amount goes to the Steward as a fee, and the hop is still paid nothing for a window it carried, which is what the paper's 3.3 would say about a window with no countersignature.
- **Units.** Six-place doubles are kept. If integer micro-units are ever to come, a registry that holds balances is the moment, and this draft would change with them.
