#!/bin/sh
# A fourth node at a neighbor's house: bw-neighbor, the hop that sells its own connection to the house.
# Run from the Mac over SSH while bw-neighbor is still on the house LAN (bw-neighbor.local), so that
# when it is carried next door it needs only power and a network. See phase0/NEIGHBOR.md.
#
# Neither house can open a port (Starlink's carrier-grade NAT), so the two nodes meet at a rendezvous host:
# a small public machine running WireGuard (phase0/hub.sh), which bw-house and bw-neighbor each connect out to.
# The hub forwards between them and nothing else; the Mint stays on the house LAN, reached through bw-house.
#
#   HUB=user@host HUB_ENDPOINT=host:51820 phase0/neighbor.sh prep
#                       everything, in order: upgrade, time, logs, wifi, tunnel (both nodes and the hub's peer list), kit, the house side
#   phase0/neighbor.sh check    after the move: handshakes, pings, the hop's status through the tunnel, the Mint's view
#   phase0/neighbor.sh bench    put bw-house's payer back on bw-forest over wg0 (the bench as it was)
#
# Environment:
#   HUB              required for prep: the SSH target of the rendezvous host, e.g. root@hub.example.net (hub.sh prep has run there)
#   HUB_ENDPOINT     required for prep: the hub's public address and WireGuard port, e.g. hub.example.net:51820
#   WIFI_SSID / WIFI_PSK   the neighbor's Wi-Fi, written as a NetworkManager profile that connects when the network is seen
#   FETCH_URL        the house's hourly purchase from the neighbor (default: 100 MB from Cloudflare's speed test)
#   MINT_LAN         the Mint's LAN address (default 192.168.0.61)
#
# Tunnel addresses: hub 10.77.2.1, bw-house 10.77.2.2 (interface wg1), bw-neighbor 10.77.2.3 (interface wg0).
# Nothing here prints or copies a private key. WireGuard keys are made on each machine and only public halves cross;
# the node's NYM key is made by install.sh and stays on the node, which is the neighbor's. The Mac keeps SSH access
# through the tunnel (phase0/NEIGHBOR.md). The hub is a WireGuard peer, so it can read what crosses it; it is yours.
set -eu
kit="$(cd "$(dirname "$0")/.." && pwd)"
SSH="ssh -o ConnectTimeout=8 -o BatchMode=yes"
N="bw-neighbor"
MINT_LAN="${MINT_LAN:-192.168.0.61}"
FETCH_URL="${FETCH_URL:-https://speed.cloudflare.com/__down?bytes=100000000}"
NET="10.77.2.0/24"; HUB_WG="10.77.2.1"; HOUSE_WG1="10.77.2.2"; NEIGH_WG1="10.77.2.3"

say() { printf '\n== %s\n' "$*"; }
run() { h="$1"; shift; $SSH "$h" "$@"; }
sudo_sh() { h="$1"; $SSH "$h" sudo sh -s; }   # script on stdin, never as an ssh argument

step_upgrade() {
  say "upgrade and reboot $N"
  run $N 'sudo apt-get -o DPkg::Lock::Timeout=600 update -q && sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 full-upgrade -y -q && sudo apt-get -o DPkg::Lock::Timeout=600 autoremove -y -q && sudo reboot' || true
  sleep 20; i=0; until $SSH $N true 2>/dev/null; do i=$((i+1)); [ $i -gt 90 ] && { echo "gave up"; exit 1; }; printf .; sleep 10; done; echo " up"
  run $N 'uname -r; grep PRETTY /etc/os-release'
}

step_time() {
  say "time: chrony from the pool (the neighbor's own internet, not the house)"
  sudo_sh $N <<'EOS'
set -e
DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -q chrony >/dev/null
cat > /etc/chrony/conf.d/bwkit.conf <<EOC
# Bandwidth Swap lab, neighbor node: the pool, and a quick step after a long power-off
makestep 1 3
EOC
systemctl restart chrony; sleep 2; chronyc tracking | head -3
EOS
}

step_logs() {
  say "logs in RAM"
  sudo_sh $N <<'EOS'
set -e
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=32M\n' > /etc/systemd/journald.conf.d/bwkit.conf
systemctl restart systemd-journald; echo "journald: volatile"
EOS
}

step_wifi() {
  [ -n "${WIFI_SSID:-}" ] || { say "wifi: WIFI_SSID not set, skipping (Ethernet at the neighbor's)"; return 0; }
  say "wifi: a profile for the neighbor's network, activated when it is seen"
  printf '%s\n%s\n' "$WIFI_SSID" "${WIFI_PSK:-}" | $SSH $N 'read ssid; read psk; sudo nmcli connection add type wifi con-name neighbor ifname wlan0 ssid "$ssid" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$psk" connection.autoconnect yes >/dev/null && echo "wifi profile written for $ssid"'
}

step_tunnel() {
  [ -n "${HUB:-}" ] && [ -n "${HUB_ENDPOINT:-}" ] || { echo "HUB=user@host and HUB_ENDPOINT=host:port are required (phase0/hub.sh prep must have run on the hub)"; exit 2; }
  say "tunnel: bw-house wg1 ($HOUSE_WG1) and $N wg0 ($NEIGH_WG1), both out to the hub $HUB_ENDPOINT ($HUB_WG)"
  for h in bw-house $N; do
    sudo_sh "$h" <<EOS
set -e
DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -q wireguard-tools iptables >/dev/null
install -d -m 0700 /etc/wireguard
k=/etc/wireguard/\$( [ "\$(hostname)" = bw-house ] && echo wg1 || echo wg0 ).key
if [ ! -s "\$k" ]; then (umask 077; wg genkey > "\$k"); fi
wg pubkey < "\$k" > "\${k%.key}.pub"
echo "\$(hostname): key present, public \$(cat "\${k%.key}.pub")"
EOS
  done
  hub_pub="$($SSH "$HUB" 'sudo cat /etc/wireguard/wg0.pub')"
  house_pub="$(run bw-house 'sudo cat /etc/wireguard/wg1.pub')"
  neigh_pub="$(run $N 'sudo cat /etc/wireguard/wg0.pub')"
  say "the hub's peer list: bw-house (and the Mint through it) and $N"
  "$kit/phase0/hub.sh" peer bw-house "$house_pub" "$HOUSE_WG1/32, $MINT_LAN/32"
  "$kit/phase0/hub.sh" peer $N "$neigh_pub" "$NEIGH_WG1/32"
  # the house: out to the hub; the whole tunnel network routed through it; the neighbor's packets forwarded to the Mint behind NAT
  sudo_sh bw-house <<EOS
set -e
umask 077
lan="\$(ip route show default | awk '{print \$5; exit}')"
cat > /etc/wireguard/wg1.conf <<EOC
# Bandwidth Swap: the neighbor's tunnel, via the rendezvous host. Private key in /etc/wireguard/wg1.key.
[Interface]
Address = $HOUSE_WG1/24
PostUp = wg set %i private-key /etc/wireguard/wg1.key
PostUp = sysctl -q -w net.ipv4.ip_forward=1
PostUp = iptables -t nat -A POSTROUTING -s $NET -o \$lan -j MASQUERADE
PostDown = iptables -t nat -D POSTROUTING -s $NET -o \$lan -j MASQUERADE

[Peer]
PublicKey = $hub_pub
AllowedIPs = $NET
Endpoint = $HUB_ENDPOINT
PersistentKeepalive = 25
EOC
systemctl enable -q wg-quick@wg1
systemctl restart wg-quick@wg1
echo "bw-house: wg1 $HOUSE_WG1 -> $HUB_ENDPOINT, forwarding $NET to the LAN via \$lan"
EOS
  # the neighbor: out to the hub, the Mint routed through the tunnel, and its own connection shared with the house
  sudo_sh $N <<EOS
set -e
umask 077
cat > /etc/wireguard/wg0.conf <<'EOC'
# Bandwidth Swap: the tunnel to the house, via the rendezvous host. Private key in /etc/wireguard/wg0.key. Outbound only.
[Interface]
Address = $NEIGH_WG1/24
PostUp = wg set %i private-key /etc/wireguard/wg0.key
PostUp = sysctl -q -w net.ipv4.ip_forward=1
PostUp = sh -c 'iptables -t nat -A POSTROUTING -s $NET -o "\$(ip route show default | awk "{print \\\$5; exit}")" -j MASQUERADE'
PostDown = sh -c 'iptables -t nat -D POSTROUTING -s $NET -o "\$(ip route show default | awk "{print \\\$5; exit}")" -j MASQUERADE'

[Peer]
PublicKey = $hub_pub
AllowedIPs = $NET, $MINT_LAN/32
Endpoint = $HUB_ENDPOINT
PersistentKeepalive = 25
EOC
systemctl enable -q wg-quick@wg0
systemctl restart wg-quick@wg0
echo "$N: wg0 $NEIGH_WG1 -> $HUB_ENDPOINT, Mint $MINT_LAN routed through the tunnel"
EOS
  sleep 5
  run bw-house "ping -c 3 -W 3 $HUB_WG | tail -1; ping -c 3 -W 3 $NEIGH_WG1 | tail -1"
  run $N "ping -c 3 -W 3 $HOUSE_WG1 | tail -1; ping -c 2 -W 3 $MINT_LAN | tail -1"
}

step_kit() {
  say "bwkit on $N as the hop; NYMs exchanged with bw-house and bw-mint"
  COPYFILE_DISABLE=1 tar --no-xattrs -C "$kit" -cf - bwkit.py install.sh systemd | $SSH $N 'mkdir -p ~/kit && tar -C ~/kit -xf -'
  run $N 'cd ~/kit && sudo ./install.sh hop'
  run $N "sudo -u bwkit python3 - <<'PY'
import json; p='/etc/bwkit/config.json'; c=json.load(open(p)); c['mint']='http://$MINT_LAN:8710'; c['iface']='wg0'; json.dump(c,open(p,'w'),indent=2); print('hop config:',c)
PY"
  neigh_nym="$(run $N 'sudo -u bwkit bwkit nym')"
  house_nym="$(run bw-house 'sudo -u bwkit bwkit nym')"
  run $N "sudo -u bwkit bwkit peer add bw-house '$house_nym'"
  for h in bw-house bw-mint; do run "$h" "sudo -u bwkit bwkit peer add $N '$neigh_nym'"; done
  run $N 'sudo systemctl enable -q --now bwkit && sleep 1 && systemctl is-active bwkit'
}

step_house() {
  say "the house buys from the neighbor: payer on wg1, hop $N, an hourly purchase"
  run bw-house "sudo cp -n /etc/bwkit/config.json /etc/bwkit/config.json.bench; sudo -u bwkit python3 - <<'PY'
import json; p='/etc/bwkit/config.json'; c=json.load(open(p)); c['hop_name']='$N'; c['hop_url']='http://$NEIGH_WG1:8710'; c['iface']='wg1'; json.dump(c,open(p,'w'),indent=2); print('payer config:',c)
PY"
  sudo_sh bw-house <<EOS
set -e
cat > /usr/local/bin/bwkit-fetch <<'EOC'
#!/bin/sh
# The house's hourly purchase from the neighbor: one download routed through the tunnel, so real bytes cross the metered interface.
url="\$1"; host="\$(printf '%s' "\$url" | sed -E 's|^[a-z]+://([^/:]+).*|\\1|')"
for ip in \$(getent ahostsv4 "\$host" | awk '{print \$1}' | sort -u); do ip route replace "\$ip/32" via $NEIGH_WG1 dev wg1; done
curl -s -o /dev/null --max-time 900 "\$url" && logger -t bwkit-fetch "fetched \$url through the neighbor"
EOC
chmod 755 /usr/local/bin/bwkit-fetch
cat > /etc/systemd/system/bwkit-fetch.service <<EOC
[Unit]
Description=Bandwidth Swap: the house's hourly purchase from the neighbor
[Service]
Type=oneshot
ExecStart=/usr/local/bin/bwkit-fetch $FETCH_URL
EOC
cat > /etc/systemd/system/bwkit-fetch.timer <<EOC
[Unit]
Description=Bandwidth Swap: buy from the neighbor every hour, twenty minutes past
[Timer]
OnCalendar=*-*-* *:20:00
Persistent=true
[Install]
WantedBy=timers.target
EOC
systemctl daemon-reload
systemctl enable -q --now bwkit-fetch.timer
echo "bw-house: bwkit-fetch.timer enabled ($FETCH_URL)"
EOS
  say "baseline sample on payer and hop (the first window starts now)"
  run bw-house 'sudo -u bwkit bwkit sample'
  run $N 'sudo -u bwkit bwkit sample'
  run bw-house 'sudo systemctl restart bwkit-receipt.timer; systemctl list-timers bwkit-receipt.timer bwkit-fetch.timer --no-pager | head -3'
}

step_check() {
  say "the tunnel, both ends to the hub"
  run bw-house 'sudo wg show wg1 | grep -E "latest handshake|transfer" | tr -s " "'
  [ -n "${HUB:-}" ] && $SSH "$HUB" 'wg show wg0 | grep -E "peer|latest handshake" | tr -s " "' || true
  run bw-house "ping -c 3 -W 3 $NEIGH_WG1 | tail -1"
  say "the hop, through the tunnel"
  run bw-house "curl -s --max-time 5 http://$NEIGH_WG1:8710/status || echo 'hop not answering'"
  say "the neighbor's clock and its path to the Mint"
  run $N "chronyc tracking | grep -E 'Stratum|System time'; curl -s --max-time 5 http://$MINT_LAN:8710/status | head -c 300; echo"
  say "the Mint's view"
  run bw-mint 'sudo -u bwkit bwkit status'
}

step_bench() {
  say "bw-house's payer back on bw-forest over wg0"
  run bw-house 'sudo cp /etc/bwkit/config.json.bench /etc/bwkit/config.json && sudo systemctl disable -q --now bwkit-fetch.timer; sudo -u bwkit bwkit sample; echo restored'
  run bw-forest 'sudo -u bwkit bwkit sample'
}

case "${1:-}" in
  prep)  step_upgrade; step_time; step_logs; step_wifi; step_tunnel; step_kit; step_house; step_check ;;
  check) step_check ;;
  bench) step_bench ;;
  upgrade|time|logs|wifi|tunnel|kit|house) "step_$1" ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
