# The views: the Mint's, the node's, the fleet's

Written Oct 18, 2026, after the Mint's dashboard went live on all four nodes (kit 34064df). It asks who reads a dashboard, finds three readers, and plans one page that serves all three. The last sections ask whether each part, and the whole, is the best we can do, and record what changed because of the answer.

## 0. Three readers, not three pages

- **The Mint's operator** keeps the books. They read settlements, disputes, commitments, purchases and the Steward, and their question is "are the books right and closing on time?"
- **A node's operator** runs a hop or a payer, usually one, on their own connection. The neighbor is one. They read their link: what they carried or bought, what they are owed or owe, whether the other side's count agrees with theirs, whether the Mint has their windows, and what waits in the outbox when the connection is down. Their question is "am I being paid for what I did?"
- **A fleet's operator** runs several nodes, perhaps several Mints. They read health and position across all of it, and their question is "is anything wrong anywhere, and where do I stand?"

One codebase serves the three, because they share the data model (receipts, acks, settlements, commitments, purchases, the map) and the rules (what counts as late, lost, disputed, beyond the margin). What differs is which records a node holds and which questions come first.

## 1. Principles that hold for every view

1. **The node serves its own page**, offline, with nothing loaded from anywhere else. A fleet view runs in the operator's browser against the nodes' JSON, with no server of its own.
2. **Now, then Attention, then the books.** Every view opens with what the node sees now and what needs attention, in that order, before any table.
3. **Attention is computed once, by bwkit, and shown everywhere.** The checks (a stale service, a clock off its source, low disk, a receipt waiting on its ack, a lost window, an hour settled but not committed, a link whose gap runs past its margin, a public status not posted, duplicate standing offers, an outbox that is not draining) live in `bwkit.py` and are exposed as `/status.health`, so the node's own page and a fleet page read the same verdicts, and a timer can ping an operator without a browser.
4. **Baselines are learned from the node's own history.** A receipt that usually arrives one second past the hour and now takes a minute, a gap that usually sits at six percent and now sits at twenty, a purchase rate that falls by half: each is an attention item computed against the last week, not against a fixed number.
5. **Everything drills down.** A window opens to its receipt and its ack, the two signed records side by side; a commitment opens to its proof, verified in the page with the same code as the public verifier; a purchase opens to the settlements it paid.
6. **Time is a control.** An "as of" slider redraws every section as the books stood at any hour of the last month, so a page is an investigation tool, not only a status board. A snapshot is that control frozen and saved to a file.
7. **Money is shown in the unit the amounts deserve**: cents to three places under a dollar, dollars to two above, the way the public Steward page does, with the unit named once per section.
8. **Names, never addresses**, on every page and in every JSON a node serves.
9. **Phone width works**, with charts that drop detail rather than shrink text.

## 2. What each node must serve

Today a Mint serves `/status`, `/ledger`, `/commitments`, `/purchases`, `/steward`, `/map`, `/proof`, `/snapshot` and the page; a hop and a payer serve `/status` and the page. For the three views, bwkit adds:

- **`health` in `/status`** for every role: `{items: [{level, text, since}], checked_at}`, the rules of Section 1.3 and 1.4, computed from the node's own records and, where a hop or payer can reach it, the Mint's ledger for its own windows.
- **CORS on every GET** (`Access-Control-Allow-Origin: *`): the node's JSON is public within the network already, carries no secret, and the fleet view and a hop's view of its Mint need it.
- **`/ledger?since=<unix>`** on the Mint, so a page keeps what it has and asks for the rest; the whole file is 2 KB an hour and would be 17 MB in a year.
- **A hop serves** `/acks` (what it countersigned, its counts beside the payer's), `/links` (its announcements), `/outbox` (what waits, with ages); **a payer serves** `/receipts`, `/acks` (the countersignatures it holds), `/purchases` (its own), `/outbox`.
- **The page as a directory**: `dashboard/` holds `index.html`, `style.css`, `data.js`, `charts.js`, `rules.js`, `view-mint.js`, `view-node.js`, `view-fleet.js` and `bsp.js` (the verifier, moved here from the site, which then vendors it from the kit). bwkit serves the directory at `/dash/` and `index.html` at `/`, `/fleet` and `/snapshot`; `install.sh` and `provision.sh update` install the directory.

## 3. The Mint's view, second pass

What it has now stays. It gains: the drill-down of Section 1.5 on windows, commitments and purchases; `health` from `/status` in its Attention section, with the baselines; the as-of control; paging through `/ledger?since=`; money by magnitude; a "time to close" chart (receipt to commitment, per hour, the last day) beside the hour timeline, which shows a degrading path before the margin does.

## 4. The node's view (a hop or a payer)

Sections, in order:

- **Now:** service, clock, disk, the outbox (count and oldest), the last receipt signed or acked, the Mint reachable or not, the link's state (announced, the parameters in force; for a hop, each payer's standing against its patience; for a payer, whether the hop answered its last receipt).
- **Attention**, from `health`.
- **This hour**, as the node saw it: the sample, the receipt made or acked, the Mint's answer, the outbox if any.
- **My link:** the gap chart from the node's own records (a hop holds both counts at ack time; a payer holds both once the ack returns), the rate chart with the purchase minimum, the traffic strip.
- **My windows:** what I signed or acked, joined to the Mint's settlement when reachable (settled bytes, status, paid, fee, premium), so a hop sees what it earned and a payer what it spent, and each sees whether its window is in a committed hour, verified in the page. That is "the receipt is the proof" from the node's side.
- **Purchases:** a payer's own, with progress; for a hop, the ones that govern its link.
- **The outbox**, when the connection is down: what waits, how old, when the next flush is, and the plain sentence that nothing is lost.
- **The Mint:** which one, its NYM, when it last answered, its last commitment that covers me.

## 5. The fleet's view

Opened at `/fleet` on any node, or from a saved file; the operator keeps a list of node URLs in the browser (and can paste one in). The page fetches `/status` from every node, and `/steward`, `/map` and the last day of `/ledger` from each Mint. It draws:

- **The fleet grid:** one card per node: name, role, place, up or stale, clock, disk, outbox, last receipt, the count of attention items, and a link to the node's own page.
- **Attention across the fleet**, merged and sorted by level, each item naming its node.
- **The map across every Mint**, as the Mint's view draws it, with the operator's own nodes marked.
- **Position:** for the operator's nodes this month, what its payers paid, what its hops earned, the net; per Mint, the month's carriage, fees and premiums; per domain, the Steward's balances.
- **The hour across the fleet:** for each Mint, whether the last hour committed and when.

No node knows the fleet; the list lives in the browser, and nothing about the fleet is posted anywhere.

## 6. Order and sessions

A: bwkit, in the kit: `health`, CORS, `/ledger?since=`, the hop's and payer's endpoints, serving the directory; tests for each; rollout. B: the page, in the kit: the split into the directory, the three views with Section 1's principles, the snapshot mode for each, the fleet mode; checked on real snapshots of bw-mint, bw-neighbor and bw-house. A before B's rollout, since B reads what A serves; B's front end can be built on snapshots while A runs, so they run in parallel in two sessions and B installs once A has landed. C: the site vendors `bsp.js` from the kit and the Lab status links each node's page by name where the operator allows it.

## 7. Is this the best we can do? Each part

*Filled in after the mockups, one entry per view.*

## 8. Is this the best we can do? The whole

*Filled in after the parts.*
