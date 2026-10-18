# Bandwidth Swap kit

The code for the Phase 0 lab: three Raspberry Pi 3 B nodes on a home LAN that keep time, tunnel traffic and sign receipts. The site, the paper and the backlog are in [bandwidthswap.github.io](https://github.com/bandwidthswap/bandwidthswap.github.io); this repo is what runs on the nodes.

| Path | What it is |
|---|---|
| `bwkit.py` | The receipt daemon and the Mint, one file: the reference implementation of the pilot profile (`spec/`), with link announcements, the Steward's fee, the Mint's hourly commitment and the ledger rebuild. Python 3.9+ and `python3-cryptography`. |
| `spec/PROTOCOL.md`, `spec/CONFORMANCE.md`, `spec/vectors/` | The Bandwidth Swap Protocol, pilot profile 1 (`bsp/1`), written from what the kit does; what a second implementation must reproduce; the test vectors it reproduces, from published test-only keys. "The specification" below. |
| `tools/vectors.py`, `tools/conform.py` | Write the vectors from the kit; check a directory in their layout. |
| `steward_rules.py` | The Network Steward's sizes and cap as pure functions, imported by `bwkit.py` and by `sim/steward.py`, so the simulator and the Mint run one rule. Installed beside `bwkit`. |
| `dashboard/dashboard.html` | The node's own page, one file for every role, served by `bwkit` at `/` (installed to `/usr/local/share/bwkit/`): what the node sees now, what needs attention, the windows, links, commitments, peers, announcements, purchases, the Steward and the month's invoice, read from its own origin; `/snapshot` is the same page with the node's data embedded, a self-contained file to save and send. |
| `phase0/map.json` | The lab's map for the Steward: domain `earth` (USD, `bw-mint`), places `house`, `next-door` and `the-cabin`, and which node is at which place. Installed as `/etc/bwkit/map.json` on `bw-mint`. |
| `test_bwkit.py` | Runs a payer, a hop and a Mint in one process. `python3 test_bwkit.py` (on the Mac: `python3 -m venv venv && venv/bin/pip install cryptography`, then `venv/bin/python`; `venv/` is ignored) |
| `install.sh` | Installs `bwkit` on one node: `sudo ./install.sh payer\|hop\|mint` |
| `systemd/` | The hop's and Mint's service, the payer's hourly timer, the Mint's status timer (every two minutes), the Steward's hourly timer (five past, after the commitment). |
| `phase0/provision.sh` | The whole lab from the Mac over SSH: upgrade, chrony, WireGuard, logs to RAM, bwkit; `status` installs the status reporter on `bw-mint`, `steward` the Steward (the map, its key, its timer). |
| `phase0/neighbor.sh`, `phase0/NEIGHBOR.md` | A fourth node at a neighbor's house, the first operated by someone else: provisioned on the house LAN, meeting the house on a Tailscale tailnet because neither house can open a port, then carried next door. `NEIGHBOR.md` is the whole procedure. |
| `phase0/link-log.sh`, `phase0/link-report.sh` | The neighbor's link measured: a ten-minute log on both ends (each clock against NTP, the metered and uplink byte counters), the purchase line carrying its rate and overhead, and the report from the Mac that reads them with the Mint's ledger: the clock between the sites per day, overhead and rate per purchase, the payer/hop gap per window. `NEIGHBOR.md`, "Measuring the link". |
| `phase0/dispute-test.sh [hop\|payer]` | The dispute tests from the Mac: an agreeing window, then one where one side's counters restart mid-window. `hop` (the default) is the hop under-claiming, `payer` the hop over-claiming; the Mint settles both at the smaller count. |
| `phase0/clock-test.sh` | The clock rule from the Mac: `bw-forest`'s clock stepped 10 s ahead, a receipt the hop declines, chrony restored, a receipt that settles. |
| `sim/` | A deterministic simulator of the protocol's money over scenarios (the bench, the street, the cabin, Earth–Moon, Earth–Mars–Jupiter) and attacks (a wash trader, a monopolist's second key, an under-remitting Mint), with the paper's ambiguities listed. `python3 -m sim scenarios/street.toml`; see `sim/README.md`. |

## What a receipt is

The paper's Figure 2, exactly:

```
(payer NYM, hop NYM, hop index, merkle root, bytes, t_first, t_last, prev receipt sig)  signed by payer
```

A NYM is an ed25519 public key (`ed25519:` and the key, base64url). The signed bytes are the eight fields as a JSON array in that order, prefixed with `bandwidthswap/receipt/v1\n`. Timestamps are the payer's, in Unix seconds, because the payer's clock is authoritative (paper 3.5).

Phase 0 has two nodes on a tunnel and nothing chunked, so:

- `merkle_root` is the payer's SHA-256 commitment to its own meter readings for the window (counters at `t_first` and `t_last`). It stands in for the Merkle root over payload chunks until there is a chunked data plane.
- The window is an hour. The payer's receipt counts the bytes that crossed its `wg0` in that hour, both directions.

Every hour, `bw-house` (the payer) signs its receipt and asks `bw-forest` (the hop) to acknowledge it. The hop checks the signature and the payer's clock (5 seconds, decline: backlog B9), reads its own `wg0` counters for the same window, and signs the same tuple with **its own** byte count and `prev_sig` set to the payer's signature. When the counts agree that is the countersignature that closes the chain. When they don't, it is the dispute, in the same format.

Both go to `bw-mint`. The Mint checks every signature, refuses NYMs it was not told about, appends each receipt and ack to `ledger.jsonl` (append only, nothing is ever rewritten), and once it holds both halves of a window writes a settlement:

```
settled_bytes = min(payer's count, hop's count)
```

Neither party is bound beyond what it signed: the payer never pays for more than it committed to, the hop is never paid for more than it acknowledged. The settlement says `settled` when the two counts are within the link's agreement margin of each other (1% or 64 KB, the pilot's defaults, unless the hop announced a link with its own; the lab's neighbor link runs at 10%), `disputed` otherwise, and the rule is the same in both cases. `bwkit settle receipt.json ack.json [--link link.json]` does the same computation offline from the files, which is the pilot's test: a dispute resolves from the receipt data alone.

**The link's terms.** A hop announces the parameters of its link (paper, Appendix A.1) by signing them and registering them with the Mint: `bwkit announce` on the hop reads the `link` object in its config (price, margin, `min_bytes`, patience, clock tolerance, medium, range class) and posts the signed record to `POST /link`; the Mint keeps it in `links.jsonl` and lists it at `GET /links`. The announcement governs every window that starts at or after its `since`; a window with none in force settles under the pilot's defaults and says so (`link: null`). The Mint refuses a backdated announcement, so the terms of a settled window never change. **Patience** is the hop's side of it: it counts, per payer, the windows whose payer count fell short of its own beyond the margin, and after `patience_windows` of them (0, never, on the lab) it declines that payer's next receipt and runs the `on_decline` command from its config with the payer's NYM, which on the neighbor's node withdraws the exit node, since declining a receipt by itself costs the hop the window and the payer nothing.

Dollars, not tokens: the Mint prices settled bytes at the announcement's price (or `price_usd_per_gb` from its config when there is none; the lab's posted price, 10 cents a gigabyte), takes the Network Steward's fraction `steward_fee` (5%, paper 3.3) of every settlement as `fee_usd`, and `bwkit invoice 2026-10` sums a month with the relay's share and the Steward's line apart. There is no payment rail; the invoice is the pilot's settlement (backlog E, B8 default d). Settlements written before 0.3.0 carry no fee and none is taken from them.

## The Steward and purchases

The paper's Network Steward (Section 7) is a rule, not a party: it receives the fraction `f` of every fee and can spend only by posting purchase commitments, which pay out against settled receipts and refund on shortfall. In the kit a bounty is a **purchase** (the Merkle commitment keeps the word commitment). Both halves run on the lab since October 18, 2026.

**A purchase** (spec, Section 15) is a signed record: a route (from a node or a place to a node or a place), a minimum rate in bytes per window, a premium per gigabyte above the posted price, a number of windows, a deadline and an escrow, which on the pilot is a line on the invoice. A payer posts one with `bwkit purchase post --to bw-neighbor --min-bytes 50000000 --premium 0.02 --windows 168 --escrow 1.00 --deadline +7d` and the Mint registers it (`POST /purchase`, `purchases.jsonl`, `GET /purchases`, `bwkit purchase list`). From then on every window the purchase governs (the window running when it was posted and the ones after, until its windows or its deadline are spent) settles with the rule of Section 5.1 after the smaller count: the premium on the bytes moved, in proportion below the minimum with the rest of that window's premium returned to the poster, drawn from the poster's escrow first and then from the Steward's match under the per-payer cap. The settlement carries `purchase`, `premium_usd`, `refund_usd`, `from_poster_usd`, `from_steward_usd` and `steward_held_usd`; the fee takes `f` of the premium as of any fee; the invoice shows the premium column and the refund line; the rebuild takes `purchases.jsonl` as its fourth input.

**The Steward** runs on `bw-mint` as `bwkit-steward.timer`, five minutes past every hour, with its own key (`bwkit steward keygen`, kept in `steward.priv` beside the Mint's) and the map in `/etc/bwkit/map.json`. Every balance it shows is read from the ledger and the purchases; `steward.jsonl` logs its runs. Each run applies the three programs of PLAN.md 2.4 through `steward_rules.py`: a **match** on a payer's open purchase, at `m` of the escrow within `m` of the Steward's whole income (on the lab a hundredth of a cent, which is the rule working); a **second-bridge** bounty for every link that carried fees between two different places in the program window, sized at the Steward's take of those fees with the link's effective price as the premium, routed from one place to the other through a third (nobody on the lab can collect it, since the lab has no third place; a link inside one place, like the bench wire, is skipped); and a standing **first-link** offer for every mapped place no settled window has an end at, topped up toward `first_link_usd` on every run from what is left after the match reserve. Escrow a closed posting did not spend returns to the Steward and is posted again. The Steward posts through the Mint's own `/purchase`, which accepts its NYM for its programs.

```
sudo -u bwkit bwkit steward status     # on bw-mint: the balances in a line
sudo -u bwkit bwkit steward book       # the book: balances, open offers, the links of the program window, the map
sudo -u bwkit bwkit steward run        # what the timer does
sudo -u bwkit bwkit steward feed       # the `steward` object the status report carries
sudo -u bwkit bwkit steward map        # the map, and which node is where
```

The status report carries the same book as `steward` (names and places only), the Mint's NYM as `mint`, and the committed hour's proof as `proof`, so the site's Steward's book page and its verifier draw from the feed. The Steward's parameters are the Mint's config: `steward: {m, margin, program_window_s, first_link_usd, first_link_premium, min_payer_age_s, bounty_deadline_s}`, defaults `0.04`, `0.01`, a week, `1.00`, `0.05`, a day and a week.

## The node's dashboard

Every node serves its own page at `http://<node>:8710/`: the payer too, whose service does nothing else (`bwkit.service` on `bw-house`). It reads `/status`, `/ledger`, `/commitments`, `/purchases`, `/steward` and `/map` from its own origin and draws what the node sees now (service, last receipt, windows waiting for an ack, the clock against its source, disk, when the status was last posted), what needs attention (a stale service, an uncommitted hour, a receipt waiting on its ack, a link whose gap runs past its margin, duplicate standing offers), the open and settled windows, each link's last 48 hours, the commitments with a link to each proof, the peers, the announcements, the purchases with their top-ups and closes, the Steward's book and the month's invoice. `/snapshot` is the same page with the node's current data embedded in it, one file an operator can save and send. `/status` now also carries `clock_offset_s` and `stratum` from chrony, `disk_free_bytes` and `last_report_at`.

## The specification

`spec/PROTOCOL.md` is the Bandwidth Swap Protocol, pilot profile 1 (`bsp/1`): the encodings, the receipt and the acknowledgement, the windows, the link announcement, the settlement rule with the fee, the Mint's interface, the commitment and its proof, the rebuild, patience, the security considerations, the versioning (`proto: 1` in every record; a record without it is profile 1, so every 0.2.0 record stays valid), and the purchase record the Steward's work will use. It is written from what `bwkit.py` does, and where the paper says otherwise it says so in a note and lists them in its Appendix A. The kit is the reference implementation; a second implementation holds to the same bytes through the vectors:

```
python3 tools/vectors.py              # rewrites spec/vectors/ from the kit (test-only keys, fixed clocks)
python3 tools/conform.py spec/vectors # the kit against its own vectors: every signature, settlement, root, proof and the rebuilt ledger
python3 tools/conform.py some/dir     # another implementation's output, in the same layout (spec/CONFORMANCE.md)
```

`test_bwkit.py` fails when the kit no longer writes exactly the committed vectors, so a change to an encoding is caught before it reaches a node. The vectors' keys are published and are never used on a node.

## Commitments and the rebuild

Section 4 of the paper asks two things of a Mint, and Section 9 names them as the tests Phase 0 runs before a second operator signs anything: the Mint publishes a Merkle commitment over each hour's settlements, so anyone holding a receipt can prove it was included or omitted; and the ledger is reconstructible from the parties' signed copies alone, so a Mint whose rebuilt ledger differs from its published commitment has signed its own conviction.

**The commitment.** After every settlement the Mint takes that hour's settlements (the hour the window started in: a window from 21:00:03 to 22:00:02 is hour 21), sorts them by window start, hashes each, builds the tree, signs the root with its own NYM key and appends the commitment to the ledger, right after the settlement:

```
{"kind": "commitment", "t": …, "mint": "ed25519:…", "window": 1792188000, "root": "…64 hex…", "count": 1, "signature": "…"}
```

A leaf is SHA-256 over the canonical JSON of the settlement entry (sorted keys, no spaces) without `t`, the Mint's own clock, which nobody else can know; everything else in the entry follows from the two signed receipts and the posted price, so a ledger rebuilt elsewhere makes the same leaf. The tree has RFC 6962's shape (a leaf is hashed with a `00` prefix and an inner node with `01`, an odd count splits at the largest power of two, one leaf's root is that leaf). The signed bytes are `bandwidthswap/commitment/v1\n` and the JSON array `[mint, window, root, count]`. An hour with several windows (the forced tests, a late window from an outbox) gets a new commitment after each, and the newest one for an hour is the Mint's current word on it; the ledger keeps them all, since nothing is ever rewritten. A ledger from before 0.2.0 starts committing with the next settled window: an hour settled before that has no commitment, and `bwkit prove` says so.

`GET /commitments` on the Mint lists the current commitment of every hour, oldest first, and `GET /proof?window=<unix seconds>` answers the proof document for the hour containing that moment. The status report carries the latest commitment as `commitment` (its hour, root, count, signature and the Mint's NYM) and that hour's proof document as `proof`, the same one `bwkit prove` prints, so a browser can check the root against the Mint's key with nothing else. Both parties hold the Mint's signed commitment the moment it is made: the Mint returns it with the settlement, and `sent.jsonl` on the payer and the hop records the Mint's answer to every envelope.

`bwkit prove <hour>` on the Mint (`2026-10-17T21` in the node's local time, or any Unix second within the hour) prints the inclusion proofs for every settlement of that hour: the commitment, and for each settlement its leaf, its index and the sibling hashes up to the root. `bwkit verify-proof proof.json [--mint ed25519:…]` checks the file with nothing else: that the Mint signed the root, and that every settlement's leaf climbs its path to it. One changed byte in a settlement and the line says `NOT in the commitment`; `--mint` refuses a proof signed by any other key.

**The rebuild.** `bwkit rebuild receipts.jsonl acks.jsonl [links.jsonl]` takes the payer's `receipts.jsonl`, the hop's `acks.jsonl` and the announcements (the Mint's `links.jsonl` or the hop's own copy; none means the pilot's defaults throughout) and nothing else. It checks every signature (a line that fails is named and left out), pairs each receipt with the ack that chains to it (`prev_sig` = the receipt's signature), settles each pair by the Mint's own rule (`resolve`: the smaller count, disputed beyond the margin of the announcement in force at the window's start, the fee at `--fee`), leaves a receipt the hop declined without an ack, and computes each hour's commitment, unsigned, since only the Mint holds its key. A settlement the Mint wrote before 0.3.0 has no `proto`, fee, margin or link; the comparison takes it as a profile-1 settlement under the defaults and compares the rebuilt one in that form, and the verdict says how many it compared that way. It prints the ledger, or writes it with `--ledger path`; a Mint that lost its own can start from that file. With `--compare ledger.jsonl` it diffs the rebuilt settlements and commitments against the Mint's ledger and says in one line whether they agree (`AGREE: …`, or `DIFFER in window … and hour …`), then how, one line each: a window settled differently (each field that differs), a window on one side only, an hour whose root differs, a commitment not signed by the Mint, an hour where the Mint's own ledger no longer matches the commitment it signed, and the hours the Mint has not committed to, which is a note, not a difference. The exit code is 0 only when nothing was left out and they agree.

On the bench, from the kit's checkout on the Mac (`python3` here is the venv's, see the table above):

```
# 1. The commitment: read it, prove an hour on the Mint, check the proof here against the Mint's NYM.
curl -s http://bw-mint.local:8710/commitments | python3 -m json.tool
ssh bw-mint 'sudo -u bwkit bwkit prove 2026-10-17T21' > proof.json
python3 bwkit.py verify-proof proof.json --mint "$(ssh bw-mint 'sudo -u bwkit bwkit nym')"

# 2. The rebuild: stop the Mint so nothing settles under the comparison, bring the two parties' copies
#    and the Mint's ledger here (/var/lib/bwkit is the bwkit user's, so sudo), rebuild, compare, start the Mint again.
ssh bw-mint 'sudo systemctl stop bwkit'
ssh bw-house  'sudo cat /var/lib/bwkit/receipts.jsonl' > receipts.jsonl
ssh bw-forest 'sudo cat /var/lib/bwkit/acks.jsonl'     > acks.jsonl     # bw-neighbor-ts once the hop is the neighbor's
ssh bw-mint   'sudo cat /var/lib/bwkit/links.jsonl'    > links.jsonl    # the announcements (the hop's copy serves as well)
ssh bw-mint   'sudo cat /var/lib/bwkit/purchases.jsonl' > purchases.jsonl
ssh bw-mint   'sudo cat /var/lib/bwkit/ledger.jsonl'   > ledger.jsonl
python3 bwkit.py rebuild receipts.jsonl acks.jsonl links.jsonl purchases.jsonl --compare ledger.jsonl
ssh bw-mint 'sudo systemctl start bwkit'
```

The second is the test the paper names: the Mint's word was never the proof. The copies the parties kept rebuild the same settlements and the same roots, or they show which window differs.

## The status page

[bandwidthswap.com](https://bandwidthswap.com/#lab) shows which nodes are up. The nodes have no inbound ports and that stays so, so the Mint pushes: `bwkit report` summarises the ledger per node and POSTs it, with a bearer token, to a small Cloudflare Worker at `status.bandwidthswap.com` (in the site repo under `worker/`), and the page reads it from there. The `bwkit-status.timer` runs it every two minutes on `bw-mint` (two, not one: the Worker's KV allows 1,000 writes a day on the free plan).

The report is **names and roles only**: for each node its name (`bw-house`, `bw-forest`, `bw-mint`), its role (`payer`, `hop`, `mint`; `peer` for a peer with nothing on the ledger yet), when its last receipt (the hop's last ack) reached the Mint, the bytes it signed for its latest window and that window's bounds, how many receipts it has signed, how many of its windows settled and how many of those were disputed, its running total of bytes (`total_bytes`: signed by the payer, counted by the hop, settled by the Mint) and its `history`, the windows of the last twelve hours (`HISTORY_HOURS`, at most `HISTORY_MAX` = 36), each with the window's bounds, its bytes, the settled bytes and the disputed flag (both `null` while the window waits for its other half); plus `since` (when the first entry reached the ledger), a `ledger` summary (receipts, acks, settlements, disputed, receipts without ack, acks without receipt), the Mint's NYM as `mint`, its latest `commitment` (its hour, root, count, signature and the Mint's NYM, `null` before the first) with that hour's `proof` (the settlements, their leaves and paths, as `bwkit prove` prints them), the Steward's book as `steward` (`null` until the Steward has a key), whether the Mint's own server answers (`service`) and `generated_at`. The page draws the map from the first part and the ledger strip from the history, one column per hour. NYMs are public keys and appear in the commitment and the proof; no address, no hostname beyond those names. `assert_public` in `bwkit.py` refuses a report carrying an IPv4 or MAC address before it is sent, the Worker refuses one on arrival, and the test checks the shape, the absence of addresses and the Worker's size limit. `bwkit report --print` on `bw-mint` prints what would be sent.

The URL and the token live in `/etc/bwkit/status.env` on `bw-mint`, root-only, read by systemd for the oneshot (`BWKIT_STATUS_URL`, `BWKIT_STATUS_TOKEN`); the token is never in the config file or in either repo. `phase0/provision.sh status` writes that file from `$BWKIT_STATUS_TOKEN` on the Mac, puts the current `bwkit.py` and units on `bw-mint`, enables the timer and sends a report; run with no token it keeps the file already there and only updates the code. The site repo's `worker/README.md` has the whole sequence, Worker side first.

## Running it

On a node, as root, from a checkout:

```
sudo ./install.sh payer     # bw-house
sudo ./install.sh hop       # bw-forest
sudo ./install.sh mint      # bw-mint
```

That installs `/usr/local/bin/bwkit`, the `bwkit` system user, `/var/lib/bwkit` (key, peers, state, ledger, outbox) and a first `/etc/bwkit/config.json` for the role. Then, once, tell every node the others' NYMs (`bwkit nym` prints a node's own):

```
sudo -u bwkit bwkit peer add bw-house  ed25519:...
sudo -u bwkit bwkit peer add bw-forest ed25519:...
```

Start the servers on the hop and the Mint, take the baseline sample on the payer and the hop at the same moment (the first window starts there), and enable the payer's timer:

```
sudo systemctl enable --now bwkit               # bw-forest and bw-mint
sudo -u bwkit bwkit sample                      # bw-house and bw-forest
sudo systemctl enable --now bwkit-receipt.timer # bw-house
```

`phase0/provision.sh kit` does all of that from the Mac. The first receipt comes at the top of the next hour; `sudo -u bwkit bwkit receipt` on `bw-house` makes one now.

Looking at it:

```
curl -s http://bw-mint.local:8710/status        # counts, pending halves, commitments
curl -s http://bw-mint.local:8710/ledger        # the ledger, one JSON object per line
curl -s http://bw-mint.local:8710/commitments   # the current commitment of every hour
curl -s http://bw-mint.local:8710/links         # the link announcements the Mint holds
curl -s http://bw-mint.local:8710/purchases     # the purchases it holds
curl -s "http://bw-mint.local:8710/proof?window=1792306800"   # the proof document for that hour
sudo -u bwkit bwkit ledger [--disputes]      # on bw-mint, readable
sudo -u bwkit bwkit links                    # on bw-mint: the announcements, readable
sudo -u bwkit bwkit invoice 2026-10          # on bw-mint, with the Steward's line
sudo -u bwkit bwkit prove 2026-10-17T21      # on bw-mint: the inclusion proofs for that hour
sudo -u bwkit bwkit announce                 # on a hop: sign the config's `link` and register it with the Mint
sudo -u bwkit bwkit purchase post ...        # on the payer: post a purchase on a route (see "The Steward and purchases")
sudo -u bwkit bwkit purchase list            # on the Mint or the payer: the purchases held
sudo -u bwkit bwkit steward book             # on bw-mint: the Steward's book
sudo -u bwkit bwkit patience [--reset NYM]   # on a hop: the payers counted short, and whether one is declined
sudo -u bwkit bwkit status                   # on any node
journalctl -u bwkit -u bwkit-receipt -f      # the daemon's log
```

Config (`/etc/bwkit/config.json`):

| Key | Who | Meaning |
|---|---|---|
| `role` | all | `payer`, `hop` or `mint` |
| `name` | all | The node's name on the ledger; the hostname by default |
| `iface` | payer, hop | The metered interface, `wg0`. `file:/path.json` with `{"rx","tx"}` is the test meter. |
| `hop_name`, `hop_url` | payer | The hop to ask for acks, by its peers.json name and its URL on the tunnel |
| `mint` | payer, hop | The Mint's URL |
| `port` | hop, mint | Listen port, 8710 |
| `link` | hop | The link's terms, announced by `bwkit announce`: `price` (USD/GB), `margin` (a fraction), `min_bytes`, `patience_windows`, `clock_tolerance_s`, `medium`, `range_class`, `payer` (`*` or a NYM). Any left out takes the pilot default (spec, Appendix B). |
| `on_decline` | hop | A command run, with the payer's NYM as its argument, the first time patience declines that payer |
| `mint_nym` | hop | The Mint's NYM for the announcement's `domain`; read from the Mint's `/status` when absent |
| `price_usd_per_gb` | mint | The posted price for a window with no announcement in force |
| `steward_fee` | mint | The Network Steward's fraction `f` of every settlement, 0.05 |
| `steward` | mint | The Steward's parameters: `m`, `margin`, `program_window_s`, `first_link_usd`, `first_link_premium`, `min_payer_age_s`, `bounty_deadline_s` |
| `steward_mint` | mint | Where the Steward posts its purchases; the Mint's own loopback by default |
| `map` | mint | The map's path; `/etc/bwkit/map.json` (or `BWKIT_MAP`) by default |
| `status_url` | mint | Where `bwkit report` posts; usually set as `BWKIT_STATUS_URL` in `/etc/bwkit/status.env` instead. The token is only ever in that file. |

What is deliberately not here: prepayment of a window (backlog B2 b), a payment rail, more than one hop, onion routing, a token, a chain. The outbox means a node that cannot reach the Mint keeps its receipts and sends them next hour; a hop that was down for a window acks the next one with both windows' bytes, and the Mint settles that window at the payer's count, so the hop loses the window it missed. The HTTP is plain, on a private LAN; the signatures are what protect the receipts.

## Keys

`install.sh` makes each node's ed25519 key in `/var/lib/bwkit/key.priv` (mode 600, owner `bwkit`). WireGuard keys are made on the nodes by `provision.sh` and stay in `/etc/wireguard`. Nothing in this repo is secret and nothing secret should be added to it.

## License

MIT, see `LICENSE`.
