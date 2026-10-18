# A node at the neighbor's: bw-neighbor

The first node that is not on the bench, and the first operated by someone who is not the authors. It is the hop: the neighbor's Pi sells the neighbor's internet connection to the house, which buys a fixed download from it every hour, and the receipts settle on the house's Mint. The paper's success criterion (Section 9) is a month of that, with at least one dispute resolved from the receipts alone.

Everything is set up while the Pi is still on the house LAN. When it is carried next door it needs only power and the neighbor's Wi-Fi. Neither house can open a port (Starlink's carrier-grade NAT has no inbound ports), so the nodes meet on a Tailscale tailnet: `bw-house`, `bw-neighbor` and `bw-mint` each join it, every one of them only connects outward, and Tailscale's relays carry the packets when no direct path can be punched. Underneath it is WireGuard; the kit meters `tailscale0` the way it meters `wg0` on the bench.

## What it takes

| Thing | Notes |
|---|---|
| A Raspberry Pi 3 Model B or newer, a microSD card of 16 GB or more (A1 or A2), a 2.5 A supply | The same as the bench. A phone charger under 2 A gives random reboots. |
| A Tailscale account | Free for personal use. From its admin console: a pre-authentication key (Settings, Keys; a one-off key is best, and the script sends it to each node once on stdin and never prints it), then after prep, approve `bw-neighbor` as an exit node (Machines, its row, Edit route settings) and disable key expiry on the three nodes so they stay joined for the month. Tailscale coordinates the connection and relays packets when it must; it cannot read them, since the WireGuard keys are made on the nodes. The alternative with no third party is a rented machine running plain WireGuard as a rendezvous host, which an earlier version of this procedure used; the kit's git history has it. |
| The neighbor's Wi-Fi | The Pi 3 is 2.4 GHz only. Give `neighbor.sh` the network's name and password at prep time and it writes a profile that connects when the network is seen. Nothing on the neighbor's side is configured or opened; the Pi only makes one outbound connection. |
| Their agreement | They hold the Pi and its keys. The house keeps SSH access through the tunnel for maintenance, and they can end that by removing the key, or end everything by unplugging it. |

## This Mac account, first

The lab's SSH key and the Imager live in the `Grace` account (`PHASE0-SETUP.md` in the site repo). From a different account, once:

1. Make this account's key: `ssh-keygen -t ed25519 -f ~/.ssh/bandwidthswap_ed25519 -N "" -C bandwidthswap-pis-<account>`. `tools/card/apply-card.sh` in the site repo reads its public half from that path.
2. Put its public line on the three bench nodes, from the `Grace` account, once each: `ssh bw-house 'cat >> ~/.ssh/authorized_keys'` with the line on stdin, and the same for `bw-mint` and `bw-forest`. Without that, `neighbor.sh` cannot reach `bw-house` and `bw-mint` to set up the house side.
3. Install the Raspberry Pi Imager from raspberrypi.com/software (the download, not Homebrew, which is broken on this Mac). Writing a card asks for an administrator's password.
4. Add the SSH config block below.

## Flashing the card

As in `PHASE0-SETUP.md` in the site repo, with the hostname `bw-neighbor`: Raspberry Pi OS Lite 64-bit, user `bw`, SSH on with public-key authentication only, both lab public keys pasted in, timezone `America/Chicago`. Then, before the first boot, `tools/card/apply-card.sh bw-neighbor` from the site repo on the mounted card, since the Imager's settings did not apply on October 16. Boot it on the house switch and check the router's device list shows `bw-neighbor`, not `raspberrypi`.

Add to `~/.ssh/config`, under the lab block:

```
Host bw-neighbor
  HostName bw-neighbor.local
  User bw
  IdentityFile ~/.ssh/bandwidthswap_ed25519
Host bw-neighbor-away
  HostName 100.x.y.z
  User bw
  IdentityFile ~/.ssh/bandwidthswap_ed25519
  ProxyJump bw-house
```

The first name reaches it on the house LAN; the second reaches it at the neighbor's, through `bw-house` and the tailnet, once `100.x.y.z` is replaced with the address `neighbor.sh prep` prints for it (or the Mac joins the tailnet itself and the jump is not needed).

## Provisioning, from wherever the Mac is

`prep` runs every step in order and assumes the Mac can reach all four nodes at once. It does not have to: each step runs on its own, so the Pi can be set up from the neighbor's own network while the bench waits for a visit to the house LAN. Three things make that work. `NODES=bw-neighbor neighbor.sh tailnet` joins only the Pi; `neighbor.sh kit` installs the hop without needing the Mint; and `neighbor.sh peers`, run once every node is on the tailnet, exchanges the NYMs and gives the hop the Mint's tailnet address. Once the Mac is on the tailnet too (install Tailscale on the Mac and sign in), `SSH_SUFFIX=-ts` makes every step reach the nodes through the tailnet aliases in `~/.ssh/config` (`bw-house-ts` and the rest, which resolve by MagicDNS), from anywhere.

The order that was actually used on October 17, with the Pi already at the neighbor's: `upgrade`, `time`, `logs` and `NODES=bw-neighbor tailnet` and `kit` from the neighbor's network; then, from the house LAN or the tailnet, `NODES="bw-house bw-mint" tailnet`, `peers`, `house`, `check`, and `wifi` last.

## Provisioning, all at once

With the Pi on the house LAN, every node reachable, and a pre-authentication key from the Tailscale admin console, from the kit's clone (the key is best kept in a private file and sourced, so it never appears in a shell history or a chat):

```
TS_AUTHKEY='tskey-auth-...' WIFI_SSID='their network' WIFI_PSK='their password' phase0/neighbor.sh prep
```

The script does, in order and each step ending with its check:

1. **Upgrade** and reboot.
2. **Time.** chrony from the pool, through the neighbor's own internet once it is there. The bench nodes take time from `bw-house`; this one cannot, so the clock rule of Section 3.5 is tested across two time sources for the first time.
3. **Logs** in RAM.
4. (The neighbor's **Wi-Fi** profile is written last, see 9.)
5. **Tailnet.** Tailscale is installed on `bw-house`, `bw-neighbor` and `bw-mint` (the official installer, fetched over HTTPS) and each joins with the key. `bw-neighbor` turns forwarding on and offers itself as an exit node, which is how it sells its connection: approve that in the admin console, and while there turn off key expiry on each machine (Machines, the row's menu, Disable key expiry), or they drop off the tailnet after 180 days. The script prints each node's tailnet address.
6. **Kit.** `bwkit` as the hop, metering `tailscale0`, its Mint reached at `bw-mint`'s tailnet address, NYMs exchanged with `bw-house` and `bw-mint`.
7. **The house side.** `bw-house`'s payer switches from `bw-forest` on `wg0` to `bw-neighbor` on `tailscale0` (the bench config is kept as `config.json.bench`; `neighbor.sh bench` puts it back) and keeps talking to the Mint on the LAN, so that traffic is not counted. A timer buys one download an hour, 100 MB, with `bw-neighbor` set as the exit node for the duration and the LAN still reachable, then drops the exit node again. The sources are tried in order until one serves the file (`FETCH_URLS`; by default Cloudflare's speed test, then tele2's, then Hetzner's), and the purchase counts only on HTTP 200 with the bytes to show for it: `journalctl -t bwkit-fetch` on `bw-house` says what each hour got. That is the house buying the neighbor's connection, and the bytes cross `tailscale0` on both ends, where both meters count them. At ten cents a gigabyte it is about a quarter a day.
8. **Check.** The tailnet as `bw-house` sees it, a ping to the neighbor, the hop's status over the tailnet, the neighbor's clock, the Mint's view.
9. **The neighbor's Wi-Fi**, last: a NetworkManager profile with priority over the house network. If their network is in range from the house the Pi may switch to it right then, which no longer matters: it stays on the tailnet either way.

On the bench all three nodes reach the tailnet over the house's connection, so the whole thing is tested before the Pi leaves the house. The one thing that cannot be tested there is the neighbor's Wi-Fi.

## The move

Unplug it, carry it over, plug in power, and let it find the Wi-Fi. Within a minute it is back on the tailnet. From the Mac:

```
phase0/neighbor.sh check
ssh bw-neighbor-away
```

The first receipt comes at the top of the next hour. `sudo -u bwkit bwkit ledger` on `bw-mint` shows the windows; the status page draws them.

## What the first hour showed (Oct 18, 00:23 CDT)

The first receipt across the link settled as a dispute: the house signed 21.99 MB for the window, the neighbor's Pi signed 24.12 MB, and the Mint paid the smaller count, $0.0022 at ten cents a gigabyte, and committed the hour. The gap is about ten percent and it is real: a 10 MB purchase measured on both ends crossed `tailscale0` as 10.65 MB received at the house and 11.73 MB sent by the neighbor, the retransmissions of a relayed path with an 87 ms round trip. The bench's one-percent agreement margin (paper, Section 3.6) was set on a wire; on this link every window will be marked disputed and settle at the payer's count, which is the rule doing exactly what it was written to do, and a number for the backlog.

The first full purchase (00:38 CDT) showed the other thing worth knowing. Cloudflare's speed test, which had served 100 MB to `bw-house` by hand all evening, answered 403 as soon as the request went out through the neighbor's exit node, with Cloudflare's own `curl` User-Agent and with a named one alike, and 1 MB of it would still come when asked for by range; it is their bot check deciding by path, not a fault of the link. The script now tries sources in order and checks what it got, and the first purchase that counted came from tele2: 104,857,600 bytes in 230 s, about 3.6 Mbit/s through a 2.4 GHz Wi-Fi, a Starlink uplink and a relayed tailnet path. The meters agreed with it on both ends.

## What to watch for a month

- The clock: `chronyc tracking` on both ends, and whether the hop ever declines a receipt (its journal says so since bwkit 0.1.1). Section 10 lists drift between sites as unmeasured; this is the measurement.
- The outbox when the neighbor's internet drops: receipts wait and settle when it returns, which is the claim in Section 1 of the paper.
- At least one dispute, forced the way the bench did it (`phase0/dispute-test.sh` works unchanged with the hop at the neighbor's), and resolved from the two signed receipts alone.
- The invoice at the end of the month: `sudo -u bwkit bwkit invoice 2026-11` on `bw-mint`. There is no payment rail; settle it the way neighbors do.

## What the neighbor needs to know

The box uses a little of their connection every hour, about the size of a short video, and nothing else. It only connects outward, to the house's private network, and listens for nothing from the internet. The house can log into it through that connection to maintain it; they can stop that by removing the house's key, and stop everything by unplugging it. The receipts it signs are its own, kept on the box, and are the proof of what it carried and what it is owed.
