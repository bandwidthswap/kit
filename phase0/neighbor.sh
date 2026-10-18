#!/bin/sh
# A fourth node at a neighbor's house: bw-neighbor, the hop that sells its own connection to the house.
# Run from the Mac over SSH while bw-neighbor is still on the house LAN (bw-neighbor.local), so that
# when it is carried next door it needs only power and the neighbor's Wi-Fi. See phase0/NEIGHBOR.md.
#
# Neither house can open a port (Starlink's carrier-grade NAT), so the nodes meet on a Tailscale tailnet:
# bw-house, bw-neighbor and bw-mint each join it, every one of them connects outward, and Tailscale's
# relays carry the packets when no direct path can be punched. The metered interface is tailscale0 on
# both ends. The house buys the neighbor's connection by using bw-neighbor as its exit node for one
# download an hour.
#
#   TS_AUTHKEY=tskey-auth-... phase0/neighbor.sh prep
#                       everything, in order: upgrade, time, logs, wifi, tailnet (three nodes), kit, the house side, check
#   phase0/neighbor.sh check    after the move: tailnet status, ping, the hop's status over the tailnet, the Mint's view
#   phase0/neighbor.sh bench    put bw-house's payer back on bw-forest over wg0 (the bench as it was)
#
# Environment:
#   TS_AUTHKEY       required for prep: a Tailscale pre-authentication key from the admin console (one-off keys are best;
#                    it goes to each node once on stdin and is never echoed). Disable key expiry for the three nodes afterwards.
#   WIFI_SSID / WIFI_PSK   the neighbor's Wi-Fi, written as a NetworkManager profile that connects when the network is seen
#   FETCH_URL        the house's hourly purchase from the neighbor (default: 100 MB from Cloudflare's speed test)
#   MINT_LAN         the Mint's LAN address, which bw-house keeps using (default 192.168.0.61)
#
# Nothing here prints or copies a private key. Tailscale's keys are made on each node; the node's NYM key is made by
# install.sh and stays on the node, which is the neighbor's. The Mac keeps SSH access through bw-house (phase0/NEIGHBOR.md).
set -eu
kit="$(cd "$(dirname "$0")/.." && pwd)"
SSH="ssh -o ConnectTimeout=8 -o BatchMode=yes"
N="bw-neighbor"
MINT_LAN="${MINT_LAN:-192.168.0.61}"
FETCH_URL="${FETCH_URL:-https://speed.cloudflare.com/__down?bytes=100000000}"
ts_ip() { run "$1" 'tailscale ip -4' | head -1; }

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

step_tailnet() {
  [ -n "${TS_AUTHKEY:-}" ] || { echo "TS_AUTHKEY=tskey-auth-... is required (a pre-authentication key from the Tailscale admin console)"; exit 2; }
  say "tailnet: bw-house, $N and bw-mint join; $N offers itself as an exit node"
  for h in bw-house $N bw-mint; do
    run "$h" 'command -v tailscale >/dev/null || (curl -fsSL https://tailscale.com/install.sh | sudo sh >/dev/null); tailscale version | head -1'
  done
  sudo_sh $N <<'EOS'
set -e
printf 'net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 1\n' > /etc/sysctl.d/99-bwkit-exit.conf
sysctl -q -p /etc/sysctl.d/99-bwkit-exit.conf
echo "$(hostname): forwarding on, for exit-node service"
EOS
  for h in bw-house bw-mint; do
    printf '%s\n' "$TS_AUTHKEY" | $SSH "$h" 'read k; sudo tailscale up --authkey "$k" --hostname "$(hostname)" >/dev/null && echo "$(hostname): on the tailnet at $(tailscale ip -4)"'
  done
  printf '%s\n' "$TS_AUTHKEY" | $SSH $N 'read k; sudo tailscale up --authkey "$k" --hostname "$(hostname)" --advertise-exit-node >/dev/null && echo "$(hostname): on the tailnet at $(tailscale ip -4), exit node offered (approve it in the admin console)"'
  sleep 3
  run bw-house "ping -c 3 -W 3 $(ts_ip $N) | tail -1"
  run $N "ping -c 3 -W 3 $(ts_ip bw-mint) | tail -1"
}

step_kit() {
  say "bwkit on $N as the hop; NYMs exchanged with bw-house and bw-mint"
  COPYFILE_DISABLE=1 tar --no-xattrs -C "$kit" -cf - bwkit.py install.sh systemd | $SSH $N 'mkdir -p ~/kit && tar -C ~/kit -xf -'
  run $N 'cd ~/kit && sudo ./install.sh hop'
  mint_ts="$(ts_ip bw-mint)"
  run $N "sudo -u bwkit python3 - <<'PY'
import json; p='/etc/bwkit/config.json'; c=json.load(open(p)); c['mint']='http://$mint_ts:8710'; c['iface']='tailscale0'; json.dump(c,open(p,'w'),indent=2); print('hop config:',c)
PY"
  neigh_nym="$(run $N 'sudo -u bwkit bwkit nym')"
  house_nym="$(run bw-house 'sudo -u bwkit bwkit nym')"
  run $N "sudo -u bwkit bwkit peer add bw-house '$house_nym'"
  for h in bw-house bw-mint; do run "$h" "sudo -u bwkit bwkit peer add $N '$neigh_nym'"; done
  run $N 'sudo systemctl enable -q --now bwkit && sleep 1 && systemctl is-active bwkit'
}

step_house() {
  say "the house buys from the neighbor: payer on tailscale0, hop $N, an hourly purchase through the exit node"
  neigh_ts="$(ts_ip $N)"
  run bw-house "sudo cp -n /etc/bwkit/config.json /etc/bwkit/config.json.bench; sudo -u bwkit python3 - <<'PY'
import json; p='/etc/bwkit/config.json'; c=json.load(open(p)); c['hop_name']='$N'; c['hop_url']='http://$neigh_ts:8710'; c['iface']='tailscale0'; json.dump(c,open(p,'w'),indent=2); print('payer config:',c)
PY"
  sudo_sh bw-house <<EOS
set -e
cat > /usr/local/bin/bwkit-fetch <<'EOC'
#!/bin/sh
# The house's hourly purchase from the neighbor: one download with bw-neighbor as the exit node, so real bytes cross
# the metered interface on both ends. The LAN stays reachable meanwhile (the Mint, the other nodes); afterwards the
# exit node is dropped so the house's own traffic goes its own way for the rest of the hour.
url="\$1"; exit_node="\$2"
tailscale set --exit-node="\$exit_node" --exit-node-allow-lan-access=true || exit 1
curl -s -o /dev/null --max-time 900 "\$url" && logger -t bwkit-fetch "fetched \$url through the neighbor"
tailscale set --exit-node=
EOC
chmod 755 /usr/local/bin/bwkit-fetch
cat > /etc/systemd/system/bwkit-fetch.service <<EOC
[Unit]
Description=Bandwidth Swap: the house's hourly purchase from the neighbor
[Service]
Type=oneshot
ExecStart=/usr/local/bin/bwkit-fetch $FETCH_URL $neigh_ts
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
  say "the tailnet, as bw-house sees it"
  run bw-house 'tailscale status | head -5'
  neigh_ts="$(ts_ip $N)"; mint_ts="$(ts_ip bw-mint)"
  run bw-house "ping -c 3 -W 3 $neigh_ts | tail -1"
  say "the hop, over the tailnet"
  run bw-house "curl -s --max-time 5 http://$neigh_ts:8710/status || echo 'hop not answering'"
  say "the neighbor's clock and its path to the Mint"
  run $N "chronyc tracking | grep -E 'Stratum|System time'; curl -s --max-time 5 http://$mint_ts:8710/status | head -c 300; echo"
  say "the Mint's view"
  run bw-mint 'sudo -u bwkit bwkit status'
}

step_bench() {
  say "bw-house's payer back on bw-forest over wg0"
  run bw-house 'sudo cp /etc/bwkit/config.json.bench /etc/bwkit/config.json && sudo systemctl disable -q --now bwkit-fetch.timer; sudo tailscale set --exit-node=; sudo -u bwkit bwkit sample; echo restored'
  run bw-forest 'sudo -u bwkit bwkit sample'
}

case "${1:-}" in
  prep)  step_upgrade; step_time; step_logs; step_wifi; step_tailnet; step_kit; step_house; step_check ;;
  check) step_check ;;
  bench) step_bench ;;
  upgrade|time|logs|wifi|tailnet|kit|house) "step_$1" ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
