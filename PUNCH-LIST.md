# Punch list

Work that is open in the kit and the lab, grouped by when it can be done. Each item says where it stands and how to pick it up. Work set aside in the site repo is in its own `PUNCH-LIST.md`.

*Started Oct 18, 2026.*

## Due on a date

- **Oct 25: set the neighbor link's margin from a week of data. Done Oct 30, 2026, 19:39 UTC: bw-neighbor announces a 3% margin** (link hash `75dc4fbceb2e8cf7`, since 1793389176; price, floor, patience, medium and range unchanged, clock tolerance still 15 s; `phase0/neighbor.sh announce` with `LINK_MARGIN=0.03 LINK_CLOCK_TOL=15`, and those are now the script's defaults). Read from the Mint's `/ledger` over the tailnet, section 4 of `link-report.sh` by hand (the clocks, section 1, need the linklog files over SSH and were not read; next time). The week this item named, 130 windows from Oct 18 00:00 to Oct 23 13:21 UTC: the hop's count over the payer's by a median 7.4%, p95 10.1%, max 10.95%, climbing day by day (median 3.7% on Oct 18, 5.8, 6.6, 7.8, 9.0% on Oct 22), so the 10% guess fit that path and would have marked one window in 130 (the Oct 19 07:00 hop counter reset, −100%); 8% would have marked 38, 5% 103. That path is gone: since the download sources changed on Oct 24, the 141 full windows to Oct 30 have a median gap of 0.9%, p99 1.5%, max 1.8%, so 10% marked nothing. The loss was the download path's through the relay, not the link's, and the margin follows it. 3% is about twice the worst normal window on the path as it runs; it still marks the real events in the link's history (the Oct 19 reset; the Oct 26 10:00 window where the hop counted two hours during the bw-cabin outage, +102%) and no normal one; the Oct 26 14:00 radio-test window (+16%, 6 KB) is under the 64 KB floor and written settled under any margin. The 30 outage windows of Oct 23 to 24 (14 to 26 KB, tunnel chatter) ran the other way, payer over hop by about 26%, all under the floor. If a source brings the old loss back, the marks will say so and the hop re-announces; patience is 0, so no payer is declined for it. The first settlement under the new terms is the 20:00 UTC window, closing at 21:00 UTC. The same numbers are in the site's `OPEN-QUESTIONS.md`, B11. The dashboard's learned baselines were not looked at.
- **Oct 25, 04:06: the house's purchase runs out.** Purchase `60162767`: 168 windows, at least 50 MB a window, two cents a gigabyte premium, a dollar of escrow. Check that it closes on its own, the unspent escrow returns to bw-house, and `bwkit steward book`, `bwkit invoice` and the dashboard agree on what was paid.
- **Nov 1: the first invoice with the neighbor.** `sudo -u bwkit bwkit invoice 2026-10` on bw-mint, settled the way neighbors do; there is no payment rail. `phase0/NEIGHBOR.md` names November as the first full month.

## In the lab

The first two are the month's tests from `phase0/NEIGHBOR.md`, not run yet. The third is a chore.

- **The outage.** Take the neighbor's internet down for an hour. Receipts and acks should wait in the outbox and settle when it returns, with no window lost. This is the claim in the paper's Section 1, and the dashboard's outbox section has never drawn a real one.
- **A forced dispute across the link.** Run `phase0/dispute-test.sh` with the hop at the neighbor's, and resolve it from the two signed receipts alone.

- **DHCP reservations on the gateway** for the three bench nodes, so the addresses in the site repo's `PHASE0-SETUP.md` stay true. Open since Oct 16 and never recorded as done. It is hands-on; the clicks are in that file, under "DHCP reservations on the gateway".

## Phase 1: the radios

The plan is `radio/PLAN.md`. Where it stood on Oct 26, 2026, 14:10 UTC; the site repo's `CLAUDE.md`, "Pick up here", has the next steps in order with their checks.

- **Done.** The `bwl/1` codec, the carrier `bwradio` 0.2.0 (payer, hop, gateway and, since Oct 26, relay) and its fake-mesh tests (15, among them the relayed outage); `provision.sh radio` with `RADIO_RELAY`, `radio-config.sh` (`FOREST_JUMP=bw-house-ts` for `bw-forest`), `neighbor.sh announce` with `LINK_CLOCK_TOL`. bwkit 0.5.0 everywhere but `bw-forest`. Every Pi but `bw-neighbor` has a board on `bwlab`, verified by key hash: three at the house (`bw-house` `!435b0188`, `bw-mint` `!435b01c4`, `bw-forest` `!435b4f4c`), two next door on the neighbor's Wi-Fi (`bw-cabin` `!435af8ac`, `bw-mint2` `!435b4d68`). Carriers: `bw-house` (payer, the Mint's gateway), `bw-mint2` (relay for `bw-neighbor`), `bw-cabin` (hop, still 0.1.0). `bw-neighbor`'s link announces a 15 s clock tolerance. **The test, in its control-plane form, passed on Oct 26 at 14:03 UTC**: the tunnel to `bw-neighbor` blocked on `bw-house`, the receipt by radio to `bw-mint2`, the hop's ack back by radio through it, the Mint settled and committed; the numbers are in the plan's status line.
- **Not done.** Step 7 in full: the house's uplink pulled at five to the hour, so `bw-house` also loses its time source and the feed stops; then plugged back in, the report posts and the page verifies the window. Step 2, the range numbers (RSSI and SNR over an hour: house to next door reads −5 to +7 dB at LongFast on the stronger board, −17 dB on `bw-cabin`'s). `bw-cabin` off the air since some time before Oct 26 morning (not on the tailnet nor the neighbor's LAN): check its power and Wi-Fi, then `provision.sh radio` with it in `RADIO_NODES` brings its carrier to 0.2.0. The carrier's CPU: `bw-house`'s used 3 h 17 min in about two days (the half-second poll with the Meshtastic library); worth a look before the forest node runs on a battery. The gateway posts an ack it took by radio twice (its own outbox flush, then `deliver_to_mint`; the second is a duplicate the Mint refuses): harmless, untidy.
- **The two new nodes.** `bw-cabin` (hop, 100.93.14.101, `bw-cabin-ts`) and `bw-mint2` (a second Mint, 100.125.246.56, `bw-mint2-ts`, NYM `ed25519:T3sNbihh_zuolcTJ03MxvfNtuXguKz5By-9brQfmXO8`, an empty ledger and no peers, now the relay) were made on Oct 19 from the `isiah` Mac with the site repo's `tools/card/apply-card.sh` and `N=<node> [ROLE=mint] SSH_SUFFIX=-ts phase0/neighbor.sh`. Both sit next door on the neighbor's Wi-Fi, not at the house; `bw-cabin` is on the map at `house` until it moves to `the-cabin`, and before it carries windows by radio, `neighbor.sh announce` with `medium`, `range_class` and `LINK_CLOCK_TOL=15`. Both still have Tailscale key expiry on (April 17, 2027): turn it off in the admin console. `bw-mint2` as a Mint waits: its own payer and hop, `status_url` to the feed, the map listing it, and the reconciliation between two Mints, the next plan.
- **After the test**: the HaLow pair (step 8) and `bw-mint2` as a Mint (step 9).

## Kit fixes

- **The page cannot verify a proof when it is opened at a node's tailnet address.** On Oct 18, "verify here" on the Mint's page and "verify the latest hour here" on bw-neighbor's both said "the signature does not verify" for every hour. The proofs are good. `bwkit verify-proof` passes them, and the same page passes them in two other places: at `http://localhost:8711/` through `ssh -N -L 8711:127.0.0.1:8710 bw-mint-ts` (13:00, 14:00 and 15:00 verified), and in a saved `/snapshot` opened as a file. The cause is the browser. A plain-http page at an address other than localhost is not a secure context, so it has no `crypto.subtle`. `dashboard/bsp.js` needs it for SHA-256 (the leaves) and for Ed25519. Its fallback, noble, needs it for SHA-512. The resulting error is caught and reported as a bad signature. Until it is fixed, verify through the tunnel or from a saved snapshot. To pick it up:
  1. Give `bsp.js` its own SHA-256 and SHA-512 in plain JavaScript, used when `crypto.subtle` is missing, and hand the SHA-512 to noble (`etc.sha512Async`).
  2. Vendor noble into `dashboard/` instead of importing it from jsDelivr. A node's page loads nothing from anywhere else (`dashboard/PLAN.md`, principle 1), and a node on the tailnet may have no internet.
  3. When verification cannot run, say so in the page ("this browser has no WebCrypto on a plain-http page") instead of reporting a bad signature.
  4. Add the no-WebCrypto case to `dashboard/test.mjs` and to the site's vector runner, then vendor the new `bsp.js` into the site.
  Serving the nodes over HTTPS with `tailscale serve` would also give a secure context, but the page should not depend on it.
- **One test is flaky.** `test_bwkit.py` failed once in four runs on Oct 18, before `bwkit.py` changed, and the name of the failing test was not captured. Run it in a loop until it fails, then fix the cause.
- **bw-forest is not on the tailnet.** It runs 0.4.0 on the bench and answers on the house LAN, but a fleet page reading over the tailnet cannot reach it. Join it with `NODES=bw-forest phase0/neighbor.sh tailnet`, which needs a fresh pre-authentication key in `~/.config/bwkit/neighbor.env`. Or decide that it waits for Phase 1, when it becomes the radio hop.

## Dashboard, after the serving work

These are the follow-ups in `dashboard/PLAN.md` Sections 7 and 8. Start them once bwkit serves the three views on all four nodes.

- **"Verify the last day"**: one button that checks every committed hour of the last 24, not one hour at a time.
- **The Steward's history in the as-of control.** The control moves every record but not the Steward's balances, which exist only as of now. `steward.jsonl` already holds every run, so the page can redraw from it.
- **A hop's own invoice.** "Owed to me" is the page's arithmetic over the Mint's settlements. A hop should get the invoice itself for its own windows, through the Mint proxy.
- **A per-node "mine" switch in the fleet list**, so watching a counterparty's node does not count toward your own position.

## Protocol, not yet built

- **Prepayment of a window** (backlog B2, option b). The receipt and the monthly invoice are the settlement today (B8, option d).
- **What profile 1 left out:** announcements over a gossip layer, places that buy service through an anchor, proving allowances that pay, and a second Mint. `spec/PROTOCOL.md` marks each as not in profile 1.

## Waiting on the site repo

- **The payer pays for some of the path's loss.** On Oct 18 the house's own meter counted 1.102 bytes on `tailscale0` for each byte of payload, because TCP's retransmissions travel inside the tunnel and arrive. Whitepaper v0.6 Section 3.6 says every retransmission is the hop's cost. A week of `phase0/link-report.sh` data should size the effect. The correction to the paper then goes on a proof sheet for approval.
