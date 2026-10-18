# Bandwidth Swap kit

The code for the Phase 0 lab: three Raspberry Pi 3 B nodes on a home LAN that keep time, tunnel traffic and sign receipts. The site, the paper and the backlog are in [bandwidthswap.github.io](https://github.com/bandwidthswap/bandwidthswap.github.io); this repo is what runs on the nodes.

| Path | What it is |
|---|---|
| `bwkit.py` | The receipt daemon and the Mint, one file, with the Mint's hourly commitment and the ledger rebuild. Python 3.9+ and `python3-cryptography`. |
| `test_bwkit.py` | Runs a payer, a hop and a Mint in one process. `python3 test_bwkit.py` (on the Mac: `python3 -m venv venv && venv/bin/pip install cryptography`, then `venv/bin/python`; `venv/` is ignored) |
| `install.sh` | Installs `bwkit` on one node: `sudo ./install.sh payer\|hop\|mint` |
| `systemd/` | The hop's and Mint's service, the payer's hourly timer, the Mint's status timer (every two minutes). |
| `phase0/provision.sh` | The whole lab from the Mac over SSH: upgrade, chrony, WireGuard, logs to RAM, bwkit; `status` installs the status reporter on `bw-mint`. |
| `phase0/neighbor.sh`, `phase0/NEIGHBOR.md` | A fourth node at a neighbor's house, the first operated by someone else: provisioned on the house LAN, meeting the house on a Tailscale tailnet because neither house can open a port, then carried next door. `NEIGHBOR.md` is the whole procedure. |
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

Neither party is bound beyond what it signed: the payer never pays for more than it committed to, the hop is never paid for more than it acknowledged. The settlement says `settled` when the two counts are within 1% (or 64 KiB) of each other, `disputed` otherwise, and the rule is the same in both cases. `bwkit settle receipt.json ack.json` does the same computation offline from the two files, which is the pilot's test: a dispute resolves from the receipt data alone.

Dollars, not tokens: the Mint prices settled bytes at `price_usd_per_gb` from its config (the lab's posted price, 10 cents a gigabyte) and `bwkit invoice 2026-10` sums a month. There is no payment rail; the invoice is the pilot's settlement (backlog E, B8 default d).

## Commitments and the rebuild

Section 4 of the paper asks two things of a Mint, and Section 9 names them as the tests Phase 0 runs before a second operator signs anything: the Mint publishes a Merkle commitment over each hour's settlements, so anyone holding a receipt can prove it was included or omitted; and the ledger is reconstructible from the parties' signed copies alone, so a Mint whose rebuilt ledger differs from its published commitment has signed its own conviction.

**The commitment.** After every settlement the Mint takes that hour's settlements (the hour the window started in: a window from 21:00:03 to 22:00:02 is hour 21), sorts them by window start, hashes each, builds the tree, signs the root with its own NYM key and appends the commitment to the ledger, right after the settlement:

```
{"kind": "commitment", "t": …, "mint": "ed25519:…", "window": 1792188000, "root": "…64 hex…", "count": 1, "signature": "…"}
```

A leaf is SHA-256 over the canonical JSON of the settlement entry (sorted keys, no spaces) without `t`, the Mint's own clock, which nobody else can know; everything else in the entry follows from the two signed receipts and the posted price, so a ledger rebuilt elsewhere makes the same leaf. The tree has RFC 6962's shape (a leaf is hashed with a `00` prefix and an inner node with `01`, an odd count splits at the largest power of two, one leaf's root is that leaf). The signed bytes are `bandwidthswap/commitment/v1\n` and the JSON array `[mint, window, root, count]`. An hour with several windows (the forced tests, a late window from an outbox) gets a new commitment after each, and the newest one for an hour is the Mint's current word on it; the ledger keeps them all, since nothing is ever rewritten. A ledger from before 0.2.0 starts committing with the next settled window: an hour settled before that has no commitment, and `bwkit prove` says so.

`GET /commitments` on the Mint lists the current commitment of every hour, oldest first. The status report carries the latest one as `commitment` (its hour, root, count and signature; the Mint's NYM stays out, and the Worker takes the field as it takes any other). Both parties hold the Mint's signed commitment the moment it is made: the Mint returns it with the settlement, and `sent.jsonl` on the payer and the hop records the Mint's answer to every envelope.

`bwkit prove <hour>` on the Mint (`2026-10-17T21` in the node's local time, or any Unix second within the hour) prints the inclusion proofs for every settlement of that hour: the commitment, and for each settlement its leaf, its index and the sibling hashes up to the root. `bwkit verify-proof proof.json [--mint ed25519:…]` checks the file with nothing else: that the Mint signed the root, and that every settlement's leaf climbs its path to it. One changed byte in a settlement and the line says `NOT in the commitment`; `--mint` refuses a proof signed by any other key.

**The rebuild.** `bwkit rebuild receipts.jsonl acks.jsonl` takes the payer's `receipts.jsonl` and the hop's `acks.jsonl` and nothing else. It checks every signature (a line that fails is named and left out), pairs each receipt with the ack that chains to it (`prev_sig` = the receipt's signature), settles each pair by the Mint's own rule (`resolve`: the smaller count, disputed over 1%), leaves a receipt the hop declined without an ack, and computes each hour's commitment, unsigned, since only the Mint holds its key. It prints the ledger, or writes it with `--ledger path`; a Mint that lost its own can start from that file. With `--compare ledger.jsonl` it diffs the rebuilt settlements and commitments against the Mint's ledger and says in one line whether they agree (`AGREE: …`, or `DIFFER in window … and hour …`), then how, one line each: a window settled differently (each field that differs), a window on one side only, an hour whose root differs, a commitment not signed by the Mint, an hour where the Mint's own ledger no longer matches the commitment it signed, and the hours the Mint has not committed to, which is a note, not a difference. The exit code is 0 only when nothing was left out and they agree.

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
ssh bw-forest 'sudo cat /var/lib/bwkit/acks.jsonl'     > acks.jsonl
ssh bw-mint   'sudo cat /var/lib/bwkit/ledger.jsonl'   > ledger.jsonl
python3 bwkit.py rebuild receipts.jsonl acks.jsonl --compare ledger.jsonl
ssh bw-mint 'sudo systemctl start bwkit'
```

The second is the test the paper names: the Mint's word was never the proof. The copies the parties kept rebuild the same settlements and the same roots, or they show which window differs.

## The status page

[bandwidthswap.com](https://bandwidthswap.com/#lab) shows which nodes are up. The nodes have no inbound ports and that stays so, so the Mint pushes: `bwkit report` summarises the ledger per node and POSTs it, with a bearer token, to a small Cloudflare Worker at `status.bandwidthswap.com` (in the site repo under `worker/`), and the page reads it from there. The `bwkit-status.timer` runs it every two minutes on `bw-mint` (two, not one: the Worker's KV allows 1,000 writes a day on the free plan).

The report is **names and roles only**: for each node its name (`bw-house`, `bw-forest`, `bw-mint`), its role (`payer`, `hop`, `mint`; `peer` for a peer with nothing on the ledger yet), when its last receipt (the hop's last ack) reached the Mint, the bytes it signed for its latest window and that window's bounds, how many receipts it has signed, how many of its windows settled and how many of those were disputed, its running total of bytes (`total_bytes`: signed by the payer, counted by the hop, settled by the Mint) and its `history`, the windows of the last twelve hours (`HISTORY_HOURS`, at most `HISTORY_MAX` = 36), each with the window's bounds, its bytes, the settled bytes and the disputed flag (both `null` while the window waits for its other half); plus `since` (when the first entry reached the ledger), a `ledger` summary (receipts, acks, settlements, disputed, receipts without ack, acks without receipt), the Mint's latest `commitment` (its hour, root, count and signature, `null` before the first), whether the Mint's own server answers (`service`) and `generated_at`. The page draws the map from the first part and the ledger strip from the history, one column per hour. No NYM, no address, no hostname beyond those names. `assert_public` in `bwkit.py` refuses a report carrying an IPv4 or MAC address before it is sent, the Worker refuses one on arrival, and the test checks both the shape and the absence of keys. `bwkit report --print` on `bw-mint` prints what would be sent.

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
sudo -u bwkit bwkit ledger [--disputes]      # on bw-mint, readable
sudo -u bwkit bwkit invoice 2026-10          # on bw-mint
sudo -u bwkit bwkit prove 2026-10-17T21      # on bw-mint: the inclusion proofs for that hour
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
| `price_usd_per_gb` | mint | The posted price |
| `status_url` | mint | Where `bwkit report` posts; usually set as `BWKIT_STATUS_URL` in `/etc/bwkit/status.env` instead. The token is only ever in that file. |

What is deliberately not here: prepayment of a window (backlog B2 b), a payment rail, more than one hop, onion routing, a token, a chain. The outbox means a node that cannot reach the Mint keeps its receipts and sends them next hour; a hop that was down for a window acks the next one with both windows' bytes, and the Mint settles that window at the payer's count, so the hop loses the window it missed. The HTTP is plain, on a private LAN; the signatures are what protect the receipts.

## Keys

`install.sh` makes each node's ed25519 key in `/var/lib/bwkit/key.priv` (mode 600, owner `bwkit`). WireGuard keys are made on the nodes by `provision.sh` and stay in `/etc/wireguard`. Nothing in this repo is secret and nothing secret should be added to it.

## License

MIT, see `LICENSE`.
