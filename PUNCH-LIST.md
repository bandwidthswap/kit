# Punch list

Work that is open in the kit and the lab, grouped by when it can be done. Each item says where it stands and how to pick it up. Work set aside in the site repo is in its own `PUNCH-LIST.md`.

*Started Oct 18, 2026.*

## Due on a date

- **Oct 25: set the neighbor link's margin from a week of data.** The week has a hole: the hourly purchase failed from Oct 23, 14:20 UTC, to Oct 24, 19:20 UTC (every download source died; new ones since), so use the windows from Oct 18 to Oct 23, 13:21 UTC, about 115 of them. The announcement says 10%, chosen from the first night's gaps (backlog B11 in the site repo). Run `phase0/link-report.sh`, take the week's gap distribution, and re-announce with `bwkit announce` on bw-neighbor. The same week tells whether the dashboard's learned baselines fire usefully; they stay silent until a node has a week of windows.
- **Oct 25, 04:06: the house's purchase runs out.** Purchase `60162767`: 168 windows, at least 50 MB a window, two cents a gigabyte premium, a dollar of escrow. Check that it closes on its own, the unspent escrow returns to bw-house, and `bwkit steward book`, `bwkit invoice` and the dashboard agree on what was paid.
- **Nov 1: the first invoice with the neighbor.** `sudo -u bwkit bwkit invoice 2026-10` on bw-mint, settled the way neighbors do; there is no payment rail. `phase0/NEIGHBOR.md` names November as the first full month.

## In the lab

The first two are the month's tests from `phase0/NEIGHBOR.md`, not run yet. The third is a chore.

- **The outage.** Take the neighbor's internet down for an hour. Receipts and acks should wait in the outbox and settle when it returns, with no window lost. This is the claim in the paper's Section 1, and the dashboard's outbox section has never drawn a real one.
- **A forced dispute across the link.** Run `phase0/dispute-test.sh` with the hop at the neighbor's, and resolve it from the two signed receipts alone.

- **DHCP reservations on the gateway** for the three bench nodes, so the addresses in the site repo's `PHASE0-SETUP.md` stay true. Open since Oct 16 and never recorded as done. It is hands-on; the clicks are in that file, under "DHCP reservations on the gateway".

## Phase 1: the radios

The plan is `radio/PLAN.md`. Where it stood on Oct 24, 2026, 21:50 UTC; the site repo's `CLAUDE.md`, "Pick up here", has the next steps in order with their checks.

- **Done.** The `bwl/1` codec, the carrier `bwradio` and its fake-mesh tests; `provision.sh radio` and `radio-config.sh` (`FOREST_JUMP=bw-house-ts` for `bw-forest`). bwkit 0.5.0 on every node except `bw-forest` (0.4.0, idle). **Every Pi at the house has a board on `bwlab`**, keys verified by hash, each reaching `bw-house`'s directly: `bw-house` `!435b0188`, `bw-cabin` `!435af8ac`, `bw-mint` `!435b01c4`, `bw-mint2` `!435b4d68`, `bw-forest` `!435b4f4c`. The carriers run on `bw-house` (the Mint's gateway) and `bw-cabin` (hop), and each hears the other's beacon by radio; the Mints and `bw-forest` have the tooling and relay, with no carrier.
- **Not done.** `bw-neighbor` has no board yet, so the house's receipts to next door have no radio path and the test cannot run; take a spare, or the board on `bw-mint2` or `bw-forest`. The cabin board's link reads weak for one house (SNR −17 dB and −9 dB, where the others read −5 to +7): check its antenna and pigtail. A running carrier holds its board's serial port, so stop `bwkit-radio` before using the Meshtastic tool on that node.
- **Next, in order**: a board onto `bw-neighbor`; `RADIO_NODES="bw-neighbor" SSH_SUFFIX=-ts phase0/radio-config.sh`; `provision.sh radio` with `RADIO_NODES="bw-house bw-neighbor bw-cabin"` and `RADIO_IDS="bw-house=!435b0188 bw-neighbor=!… bw-cabin=!435af8ac"`; `clock_tolerance_s` 15 on `bw-neighbor`'s link (`neighbor.sh announce`); then the range numbers (step 2) and the test (step 7).
- **The two new nodes.** `bw-cabin` (hop, 100.93.14.101, `bw-cabin-ts`) and `bw-mint2` (a second Mint, 100.125.246.56, `bw-mint2-ts`, NYM `ed25519:T3sNbihh_zuolcTJ03MxvfNtuXguKz5By-9brQfmXO8`, an empty ledger and no peers) were made on Oct 19 from the `isiah` Mac with the site repo's `tools/card/apply-card.sh` and `N=<node> [ROLE=mint] SSH_SUFFIX=-ts phase0/neighbor.sh`. `bw-cabin` is on the map at `house` until it moves to `the-cabin`; before it carries windows by radio, `neighbor.sh announce` with `medium`, `range_class` and `clock_tolerance_s` 15. Both still have Tailscale key expiry on (April 17, 2027): turn it off in the admin console. `bw-mint2` waits for the test; then its own payer and hop, `status_url` to the feed, the map listing it, and the reconciliation between two Mints, the next plan.
- **After the test**: the HaLow pair (step 8) and `bw-mint2` (step 9).

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
