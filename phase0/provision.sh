#!/bin/sh
# Phase 0 node provisioning, run from the Mac over SSH (hosts bw-house, bw-forest,
# bw-mint from ~/.ssh/config). Each step is idempotent and ends with its check.
#
#   phase0/provision.sh upgrade    apt full-upgrade and reboot, all three in parallel
#   phase0/provision.sh time       chrony: bw-house from the pool, the others from bw-house
#   phase0/provision.sh tunnel     WireGuard wg0, bw-house 10.77.0.1 <-> bw-forest 10.77.0.2
#   phase0/provision.sh logs       journald in RAM on all three
#   phase0/provision.sh kit        install bwkit, exchange NYMs, baseline, enable the services
#   phase0/provision.sh check      the four verifications, nothing changed
#   phase0/provision.sh all        the steps above in order
#
# Nothing here prints or copies a private key: WireGuard keys are made on the
# nodes and only the public halves cross the LAN.
set -eu
kit="$(cd "$(dirname "$0")/.." && pwd)"
HOSTS="bw-house bw-forest bw-mint"
LAN="${LAN:-192.168.0.0/24}"
SSH="ssh -o ConnectTimeout=8 -o BatchMode=yes"

say() { printf '\n== %s\n' "$*"; }
run() { h="$1"; shift; $SSH "$h" "$@"; }
sudo_sh() { h="$1"; $SSH "$h" sudo sh -s; }   # script on stdin

wait_up() {
  for h in "$@"; do
    printf 'waiting for %s ' "$h"
    i=0
    until $SSH "$h" true 2>/dev/null; do
      i=$((i+1)); [ $i -gt 90 ] && { echo " gave up after 15 min"; exit 1; }
      printf .; sleep 10
    done
    echo " up"
  done
}

step_upgrade() {
  say "upgrade and reboot (the three in parallel; 5 to 15 minutes)"
  for h in $HOSTS; do
    ( $SSH "$h" 'sudo apt-get -o DPkg::Lock::Timeout=600 update -q && sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 full-upgrade -y -q && sudo apt-get -o DPkg::Lock::Timeout=600 autoremove -y -q && sudo reboot' > "/tmp/bwkit-upgrade-$h.log" 2>&1; echo "$h: exit $?" ) &
  done
  wait
  sleep 20
  wait_up $HOSTS
  for h in $HOSTS; do printf '%s: ' "$h"; run "$h" 'uname -r; cat /etc/os-release | grep PRETTY'; done
}

step_time() {
  say "time: chrony"
  for h in $HOSTS; do run "$h" 'sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -q chrony >/dev/null && echo "$(hostname): chrony installed"'; done
  sudo_sh bw-house <<EOS
set -e
cat > /etc/chrony/conf.d/bwkit.conf <<EOC
# Bandwidth Swap lab: serve the LAN, and keep serving at stratum 10 when the internet is away
allow $LAN
local stratum 10
EOC
systemctl restart chrony
echo "bw-house: serving $LAN"
EOS
  house="$(run bw-forest 'getent hosts bw-house.local | cut -d" " -f1' || true)"
  if [ -z "$house" ]; then
    house="$(dscacheutil -q host -a name bw-house.local | awk '/ip_address/ {print $2; exit}')"
    echo "bw-forest cannot resolve bw-house.local; using the address $house (a DHCP change would break this)"
    src="server $house iburst prefer"
  else
    src="server bw-house.local iburst prefer"
  fi
  for h in bw-forest bw-mint; do
    sudo_sh "$h" <<EOS
set -e
sed -i "s/^pool /#pool /" /etc/chrony/chrony.conf
printf '# Bandwidth Swap lab: bw-house is the only source\n%s\n' "$src" > /etc/chrony/sources.d/bwkit.sources
systemctl restart chrony
echo "$h: source is '$src'"
EOS
  done
  sleep 8
  check_time
}

check_time() {
  say "chronyc tracking"
  for h in $HOSTS; do printf '%s: ' "$h"; run "$h" 'chronyc tracking | grep -E "^(Reference ID|Stratum|System time|Leap status)" | tr "\n" ";"'; echo; done
}

step_tunnel() {
  say "tunnel: WireGuard wg0 between bw-house and bw-forest"
  for h in bw-house bw-forest; do
    sudo_sh "$h" <<'EOS'
set -e
DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -q wireguard-tools >/dev/null
install -d -m 0700 /etc/wireguard
if [ ! -s /etc/wireguard/wg0.key ]; then (umask 077; wg genkey > /etc/wireguard/wg0.key); fi
wg pubkey < /etc/wireguard/wg0.key > /etc/wireguard/wg0.pub
echo "$(hostname): key present, public $(cat /etc/wireguard/wg0.pub)"
EOS
  done
  house_pub="$(run bw-house 'sudo cat /etc/wireguard/wg0.pub')"
  forest_pub="$(run bw-forest 'sudo cat /etc/wireguard/wg0.pub')"
  for pair in "bw-house 10.77.0.1 $forest_pub 10.77.0.2 bw-forest.local" "bw-forest 10.77.0.2 $house_pub 10.77.0.1 bw-house.local"; do
    set -- $pair
    h="$1"; addr="$2"; peer_pub="$3"; peer_addr="$4"; peer_host="$5"
    peer_ip="$(run "$h" "getent hosts $peer_host | cut -d' ' -f1" || true)"
    [ -z "$peer_ip" ] && peer_ip="$(dscacheutil -q host -a name "$peer_host" | awk '/ip_address/ {print $2; exit}')"
    sudo_sh "$h" <<EOS
set -e
umask 077
cat > /etc/wireguard/wg0.conf <<EOC
# Bandwidth Swap lab tunnel. The private key stays in /etc/wireguard/wg0.key.
[Interface]
Address = $addr/30
ListenPort = 51820
PostUp = wg set %i private-key /etc/wireguard/wg0.key

[Peer]
PublicKey = $peer_pub
AllowedIPs = $peer_addr/32
Endpoint = $peer_ip:51820
PersistentKeepalive = 25
EOC
systemctl enable -q wg-quick@wg0
systemctl restart wg-quick@wg0
echo "$h: wg0 $addr, peer $peer_ip"
EOS
  done
  sleep 3
  check_tunnel
}

check_tunnel() {
  say "wg show and a ping across the tunnel"
  for h in bw-house bw-forest; do printf '%s: ' "$h"; run "$h" 'sudo wg show wg0 | grep -E "latest handshake|transfer" | tr -s " " | tr "\n" ";"'; echo; done
  run bw-house 'ping -c 3 -W 2 10.77.0.2 | tail -2'
  run bw-forest 'ping -c 3 -W 2 10.77.0.1 | tail -1'
}

step_logs() {
  say "logs to RAM: journald volatile"
  for h in $HOSTS; do
    sudo_sh "$h" <<'EOS'
set -e
install -d /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/bwkit.conf <<EOC
# Bandwidth Swap lab: keep the journal in RAM so hourly logging never wears the card
[Journal]
Storage=volatile
RuntimeMaxUse=32M
EOC
systemctl restart systemd-journald
echo "$(hostname): $(journalctl --header 2>/dev/null | grep -m1 '^File path' || echo 'journal restarted')"
EOS
  done
}

step_kit() {
  say "bwkit on the three nodes"
  for h in $HOSTS; do
    tar -C "$kit" -cf - bwkit.py install.sh systemd | $SSH "$h" 'mkdir -p ~/kit && tar -C ~/kit -xf -'
  done
  run bw-house 'cd ~/kit && sudo ./install.sh payer'
  run bw-forest 'cd ~/kit && sudo ./install.sh hop'
  run bw-mint 'cd ~/kit && sudo ./install.sh mint'
  house_nym="$(run bw-house 'sudo -u bwkit bwkit nym')"
  forest_nym="$(run bw-forest 'sudo -u bwkit bwkit nym')"
  for h in $HOSTS; do
    run "$h" "sudo -u bwkit bwkit peer add bw-house '$house_nym' >/dev/null && sudo -u bwkit bwkit peer add bw-forest '$forest_nym'"
  done
  say "services"
  run bw-mint 'sudo systemctl enable -q --now bwkit && sleep 1 && systemctl is-active bwkit'
  run bw-forest 'sudo systemctl enable -q --now bwkit && sleep 1 && systemctl is-active bwkit'
  say "baseline sample on payer and hop (the first window starts now)"
  run bw-house 'sudo -u bwkit bwkit sample'
  run bw-forest 'sudo -u bwkit bwkit sample'
  run bw-house 'sudo systemctl enable -q --now bwkit-receipt.timer && systemctl list-timers bwkit-receipt.timer --no-pager | head -2'
  check_kit
}

check_kit() {
  say "bwkit status"
  run bw-forest 'curl -s http://127.0.0.1:8710/status; echo'
  run bw-mint 'curl -s http://127.0.0.1:8710/status; echo'
  run bw-house 'sudo -u bwkit bwkit status'
}

case "${1:-}" in
  upgrade) step_upgrade ;;
  time) step_time ;;
  tunnel) step_tunnel ;;
  logs) step_logs ;;
  kit) step_kit ;;
  check) check_time; check_tunnel; check_kit ;;
  all) step_upgrade; step_time; step_tunnel; step_logs; step_kit ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
