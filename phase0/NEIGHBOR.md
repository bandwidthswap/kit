# A node at the neighbor's: bw-neighbor

The first node that is not on the bench, and the first operated by someone who is not the authors. It is the hop: the neighbor's Pi sells the neighbor's internet connection to the house, which buys a fixed download from it every hour, and the receipts settle on the house's Mint. The paper's success criterion (Section 9) is a month of that, with at least one dispute resolved from the receipts alone.

Everything is set up while the Pi is still on the house LAN. When it is carried next door it needs only power and the neighbor's Wi-Fi, because it makes its own connection out to a rendezvous host and the house does the same: neither house can open a port (Starlink's carrier-grade NAT has no inbound ports), so the two nodes meet on a small public machine that forwards between them and does nothing else.

## What it takes

| Thing | Notes |
|---|---|
| A Raspberry Pi 3 Model B or newer, a microSD card of 16 GB or more (A1 or A2), a 2.5 A supply | The same as the bench. A phone charger under 2 A gives random reboots. |
| A rendezvous host | The smallest public virtual machine you can rent, Debian or Ubuntu, with a fixed address: a few dollars a month. `phase0/hub.sh prep` turns it into a WireGuard hub at `10.77.2.1` that forwards between its peers. It is a WireGuard peer, so it can read what crosses it, which is your hourly download and the receipts, which are signed; it is your machine. Open UDP 51820 inbound on it if it has a firewall. The alternative with no machine to rent is Tailscale on the three nodes, which does the same meeting through its own relays; the kit's metered interface would then be `tailscale0` and the scripts here would need that change. |
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
  HostName 10.77.1.2
  User bw
  IdentityFile ~/.ssh/bandwidthswap_ed25519
  ProxyJump bw-house
```

The first name reaches it on the house LAN; the second reaches it at the neighbor's, through `bw-house` and the tunnel.

## The rendezvous host, once

With the machine rented and reachable as, say, `root@hub.example.net`:

```
HUB=root@hub.example.net phase0/hub.sh prep
```

It installs WireGuard, makes the hub's key there, brings up `wg0` at `10.77.2.1` on UDP 51820 with forwarding on, and prints the hub's public key. `neighbor.sh` adds the two peers to it itself.

## Provisioning, on the bench

With the hub up and the Pi on the house switch, from the kit's clone:

```
HUB=root@hub.example.net HUB_ENDPOINT=hub.example.net:51820 WIFI_SSID='their network' WIFI_PSK='their password' phase0/neighbor.sh prep
```

The script does, in order and each step ending with its check:

1. **Upgrade** and reboot.
2. **Time.** chrony from the pool, through the neighbor's own internet once it is there. The bench nodes take time from `bw-house`; this one cannot, so the clock rule of Section 3.5 is tested across two time sources for the first time.
3. **Logs** in RAM.
4. **Wi-Fi** profile, if given.
5. **Tunnel.** Both nodes connect out to the hub with a keepalive every 25 seconds, so the connections survive both routers: `bw-house` on a second interface, `wg1` at `10.77.2.2`, and `bw-neighbor` on `wg0` at `10.77.2.3`. The hub's peer list gets both, with the Mint's LAN address routed to the house, and `bw-house` forwards the neighbor's packets to the Mint behind NAT, so the Mint needs no route back. The neighbor's node forwards and NATs the house's traffic out of its own connection: that is the thing being sold. The path for every byte is house, hub, neighbor, and back.
6. **Kit.** `bwkit` as the hop, its Mint the house's through the tunnel, NYMs exchanged with `bw-house` and `bw-mint`.
7. **The house side.** `bw-house`'s payer switches from `bw-forest` on `wg0` to `bw-neighbor` on `wg1` (the bench config is kept as `config.json.bench`; `neighbor.sh bench` puts it back), and a timer buys one download an hour through the tunnel, 100 MB from Cloudflare's speed test by default, so real bytes cross the metered interface. At ten cents a gigabyte that is about a quarter a day.
8. **Check.** Handshakes at the hub, pings both ways, the hop's status through the tunnel, the neighbor's clock, the Mint's view.

On the bench both nodes reach the hub over the house's connection, so the whole thing is tested before the Pi leaves the house.

## The move

Unplug it, carry it over, plug in power, and let it find the Wi-Fi. Within a minute the keepalive brings the tunnel up through the hub. From the Mac:

```
phase0/neighbor.sh check
ssh bw-neighbor-away
```

The first receipt comes at the top of the next hour. `sudo -u bwkit bwkit ledger` on `bw-mint` shows the windows; the status page draws them.

## What to watch for a month

- The clock: `chronyc tracking` on both ends, and whether the hop ever declines a receipt (its journal says so since bwkit 0.1.1). Section 10 lists drift between sites as unmeasured; this is the measurement.
- The outbox when the neighbor's internet drops: receipts wait and settle when it returns, which is the claim in Section 1 of the paper.
- At least one dispute, forced the way the bench did it (`phase0/dispute-test.sh` works unchanged with the hop at the neighbor's), and resolved from the two signed receipts alone.
- The invoice at the end of the month: `sudo -u bwkit bwkit invoice 2026-11` on `bw-mint`. There is no payment rail; settle it the way neighbors do.

## What the neighbor needs to know

The box uses a little of their connection every hour, about the size of a short video, and nothing else. It makes one outbound connection to a machine the house rents and listens for nothing from the internet. The house can log into it through that connection to maintain it; they can stop that by removing the house's key, and stop everything by unplugging it. The receipts it signs are its own, kept on the box, and are the proof of what it carried and what it is owed.
