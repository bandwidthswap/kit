#!/bin/sh
# Phase 0 node provisioning, run from the Mac over SSH (hosts bw-house, bw-forest,
# bw-mint from ~/.ssh/config). Each step is idempotent and ends with its check.
#
#   phase0/provision.sh upgrade    apt full-upgrade and reboot, all three in parallel
#   phase0/provision.sh time       chrony: bw-house from the pool, the others from bw-house
#   phase0/provision.sh tunnel     WireGuard wg0, bw-house 10.77.0.1 <-> bw-forest 10.77.0.2
#   phase0/provision.sh logs       journald in RAM on all three
#   phase0/provision.sh kit        install bwkit, exchange NYMs, baseline, enable the services
#   phase0/provision.sh update     copy a newer bwkit.py and units to the nodes and restart the services
#   phase0/provision.sh status     bw-mint: the status reporter: status_url in the Mint's config (the feed's /report;
#                                  BWKIT_STATUS_URL overrides the pilot's), the code and units, the old token file
#                                  removed, the ten-minute timer enabled and a signed report sent
#   phase0/provision.sh map        bw-mint: sign the membership's map and post it to the feed (bwkit map post), which
#                                  then lists the Mint
#   phase0/provision.sh radio      the radio carrier (radio/PLAN.md) on RADIO_NODES (default: bw-house bw-neighbor), each
#                                  with a T-Beam on USB: the udev rule for /dev/bwradio, the bwkit user in dialout, the
#                                  venv at /opt/bwradio with the Meshtastic API, bwradio and its unit; prints each board's
#                                  radio id. With RADIO_IDS="bw-house=!a1b2c3d4 bw-neighbor=!e5f6a7b8 ..." it also writes
#                                  the `radio` object into each node's config (the gateway is RADIO_GATEWAY, bw-house) and
#                                  starts the carrier
#   phase0/provision.sh steward    bw-mint: the Network Steward (PLAN.md 2.4): the map (phase0/map.json to /etc/bwkit/map.json),
#                                  the Steward's key, the Mint restarted so it accepts the Steward's postings, and the hourly
#                                  bwkit-steward.timer (five past the hour, after the commitment); run once at the end
#   phase0/provision.sh check      the four verifications, nothing changed
#   phase0/provision.sh all        upgrade, time, tunnel, logs and kit, in order
#
# From the tailnet (the Mac away from the house LAN): SSH_SUFFIX=-ts reaches bw-house and bw-mint by the -ts aliases in
# ~/.ssh/config, and FOREST_JUMP=bw-house-ts reaches bw-forest, which is not on the tailnet, through bw-house:
#   SSH_SUFFIX=-ts FOREST_JUMP=bw-house-ts phase0/provision.sh update
#
# Nothing here prints or copies a private key: WireGuard keys are made on the
# nodes and only the public halves cross the LAN. Since bwkit 0.5.0 there is no
# status token: the Mint signs its reports with its own key.
set -eu
kit="$(cd "$(dirname "$0")/.." && pwd)"
HOSTS="bw-house bw-forest bw-mint"
LAN="${LAN:-192.168.0.0/24}"
SSH="ssh -o ConnectTimeout=8 -o BatchMode=yes"
SSH_SUFFIX="${SSH_SUFFIX:-}"

say() { printf '\n== %s\n' "$*"; }
ssh_to() {   # ssh_to <node> [command...]: by its alias plus SSH_SUFFIX, or through FOREST_JUMP for bw-forest
  h="$1"; shift
  if [ "$h" = bw-forest ] && [ -n "${FOREST_JUMP:-}" ]; then $SSH -J "$FOREST_JUMP" bw-forest "$@"; else $SSH "$h$SSH_SUFFIX" "$@"; fi
}
run() { ssh_to "$@"; }
sudo_sh() { ssh_to "$1" sudo sh -s; }   # script on stdin

wait_up() {
  for h in "$@"; do
    printf 'waiting for %s ' "$h"
    i=0
    until ssh_to "$h" true 2>/dev/null; do
      i=$((i+1)); [ $i -gt 90 ] && { echo " gave up after 15 min"; exit 1; }
      printf .; sleep 10
    done
    echo " up"
  done
}

step_upgrade() {
  say "upgrade and reboot (the three in parallel; 5 to 15 minutes)"
  for h in $HOSTS; do
    ( ssh_to "$h" 'sudo apt-get -o DPkg::Lock::Timeout=600 update -q && sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 full-upgrade -y -q && sudo apt-get -o DPkg::Lock::Timeout=600 autoremove -y -q && sudo reboot' > "/tmp/bwkit-upgrade-$h.log" 2>&1; echo "$h: exit $?" ) &
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
    COPYFILE_DISABLE=1 tar --no-xattrs -C "$kit" -cf - bwkit.py steward_rules.py install.sh systemd dashboard phase0/map.json | ssh_to "$h" 'mkdir -p ~/kit && tar -C ~/kit -xf -'
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

step_update() {
  # The Mint first, so that every record the other nodes send is read by a Mint that already knows the new shapes;
  # UPDATE_HOSTS narrows it (UPDATE_HOSTS=bw-mint for the Mint alone). The payer's timer runs the new code at the next
  # hour; its bwkit.service serves only its own page and status, and is restarted too.
  hosts="${UPDATE_HOSTS:-bw-mint bw-house bw-forest}"
  say "bwkit.py and the units to $hosts, services restarted"
  for h in $hosts; do
    COPYFILE_DISABLE=1 tar --no-xattrs -C "$kit" -cf - bwkit.py steward_rules.py install.sh systemd dashboard phase0/map.json | ssh_to "$h" 'mkdir -p ~/kit && tar -C ~/kit -xf - && sudo install -m 0755 ~/kit/bwkit.py /usr/local/bin/bwkit && sudo install -m 0644 ~/kit/steward_rules.py /usr/local/bin/steward_rules.py && sudo install -d -m 0755 /usr/local/share/bwkit/dashboard && sudo install -m 0644 ~/kit/dashboard/index.html ~/kit/dashboard/*.css ~/kit/dashboard/*.js /usr/local/share/bwkit/dashboard/ && sudo rm -f /usr/local/share/bwkit/dashboard.html /usr/local/share/bwkit/dashboard/dashboard.html && sudo install -m 0644 ~/kit/systemd/*.service ~/kit/systemd/*.timer /etc/systemd/system/ && sudo systemctl daemon-reload && echo "$(hostname): updated to bwkit $(grep -m1 "^VERSION" /usr/local/bin/bwkit | cut -d\" -f2)"'
    case "$h" in bw-mint|bw-forest) run "$h" 'sudo systemctl restart bwkit && sleep 1 && echo "$(hostname): bwkit $(systemctl is-active bwkit)"' ;;
                 bw-house) run "$h" 'sudo systemctl enable -q --now bwkit && sudo systemctl restart bwkit && sleep 1 && echo "$(hostname): bwkit $(systemctl is-active bwkit) (the payer serves its own page at /)"' ;; esac
  done
  check_kit
}

check_kit() {
  say "bwkit status"
  run bw-forest 'curl -s http://127.0.0.1:8710/status; echo'
  run bw-mint 'curl -s http://127.0.0.1:8710/status; echo'
  run bw-house 'sudo -u bwkit bwkit status'
}

step_status() {
  say "status reporter on bw-mint"
  url="${BWKIT_STATUS_URL:-https://status.bandwidthswap.com/report}"
  run bw-mint "sudo python3 - <<'PY'
import json; p='/etc/bwkit/config.json'; c=json.load(open(p)); c['status_url']='$url'; json.dump(c,open(p,'w'),indent=2); print('bw-mint: status_url', c['status_url'])
PY"
  COPYFILE_DISABLE=1 tar --no-xattrs -C "$kit" -cf - bwkit.py steward_rules.py install.sh systemd dashboard phase0/map.json | ssh_to bw-mint 'mkdir -p ~/kit && tar -C ~/kit -xf - && sudo install -m 0755 ~/kit/bwkit.py /usr/local/bin/bwkit && sudo install -m 0644 ~/kit/steward_rules.py /usr/local/bin/steward_rules.py && sudo install -d -m 0755 /usr/local/share/bwkit/dashboard && sudo install -m 0644 ~/kit/dashboard/index.html ~/kit/dashboard/*.css ~/kit/dashboard/*.js /usr/local/share/bwkit/dashboard/ && sudo rm -f /usr/local/share/bwkit/dashboard.html /usr/local/share/bwkit/dashboard/dashboard.html && sudo install -m 0644 ~/kit/systemd/*.service ~/kit/systemd/*.timer /etc/systemd/system/ && sudo rm -f /etc/bwkit/status.env && sudo systemctl daemon-reload && sudo systemctl restart bwkit && sleep 1 && echo "bw-mint: bwkit $(grep -m1 "^VERSION" /usr/local/bin/bwkit | cut -d\" -f2), server $(systemctl is-active bwkit), the token file is gone"'
  # The timer is restarted so the new schedule applies; the first post may meet the feed's one-a-minute rate (429) right
  # after the old timer's last post, and the journal says so; the next heartbeat carries it.
  run bw-mint 'sudo systemctl enable -q --now bwkit-status.timer && sudo systemctl restart bwkit-status.timer && (sudo systemctl start bwkit-status.service || true) && echo "bw-mint: bwkit-status.timer enabled (every ten minutes, five past); the first signed report:" && journalctl -u bwkit-status -n 1 --no-pager -o cat'
  check_status
}

step_radio() {
  nodes="${RADIO_NODES:-bw-house bw-neighbor}"
  gateway="${RADIO_GATEWAY:-bw-house}"
  say "the radio carrier on $nodes"
  for h in $nodes; do
    COPYFILE_DISABLE=1 tar --no-xattrs -C "$kit" -cf - radio/bwradio.py radio/wire.py systemd/bwkit-radio.service | ssh_to "$h" 'mkdir -p ~/kit && tar -C ~/kit -xf - && sudo install -d -m 0755 /usr/local/lib/bwradio && sudo install -m 0644 ~/kit/radio/wire.py /usr/local/lib/bwradio/wire.py && sudo install -m 0755 ~/kit/radio/bwradio.py /usr/local/bin/bwradio && sudo install -m 0644 ~/kit/systemd/bwkit-radio.service /etc/systemd/system/ && sudo usermod -aG dialout bwkit && (test -x /opt/bwradio/bin/python || sudo python3 -m venv --system-site-packages /opt/bwradio || (sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -q python3-venv >/dev/null && sudo python3 -m venv --system-site-packages /opt/bwradio)) && sudo sed -i "s/^include-system-site-packages = false/include-system-site-packages = true/" /opt/bwradio/pyvenv.cfg && sudo /opt/bwradio/bin/pip install -q --upgrade meshtastic 2>&1 | grep -vi 'notice' | tail -2 && sudo systemctl daemon-reload && echo "'"$h"': bwradio installed, meshtastic $(/opt/bwradio/bin/pip show meshtastic 2>/dev/null | sed -n "s/^Version: //p")"'
    # The T-Beam Supreme is an ESP32-S3 on native USB (Espressif 303a:1001). Another board: `udevadm info -q property -n /dev/ttyACM0 | grep ID_`
    sudo_sh "$h" <<'EOS'
set -e
cat > /etc/udev/rules.d/80-bwradio.rules <<'RULE'
# bwkit: the node's LoRa board (LILYGO T-Beam Supreme, ESP32-S3 native USB) as /dev/bwradio
SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", ATTRS{idProduct}=="1001", SYMLINK+="bwradio", MODE="0660", GROUP="dialout"
RULE
udevadm control --reload-rules && udevadm trigger --subsystem-match=tty
sleep 1
if [ -e /dev/bwradio ]; then echo "$(hostname): /dev/bwradio -> $(readlink /dev/bwradio)"; else echo "$(hostname): no /dev/bwradio yet (is the board plugged in? its ids: udevadm info -q property -n /dev/ttyACM0 | grep ID_)"; fi
EOS
    run "$h" 'test -e /dev/bwradio && sudo -u bwkit /opt/bwradio/bin/meshtastic --port /dev/bwradio --info 2>/dev/null | grep -E "^Owner|myNodeNum" | head -2 | sed "s/^/$(hostname): /" || true'
    # the radio id is !<myNodeNum in hex>: RADIO_IDS takes it as "bw-house=!a1b2c3d4"
  done
  if [ -n "${RADIO_IDS:-}" ]; then
    say "the radio object in each node's config"
    for h in $nodes; do
      gw=false; [ "$h" = "$gateway" ] && gw=true
      run "$h" "sudo python3 - <<'PY'
import json, os
ids = dict(x.split('=', 1) for x in '$RADIO_IDS'.split())
p = '/etc/bwkit/config.json'; c = json.load(open(p))
peers = json.load(open('/var/lib/bwkit/peers.json')) if os.path.exists('/var/lib/bwkit/peers.json') else {}
radio = c.get('radio') or {}
gw = '$gw' == 'true'
radio.update({'port': '/dev/bwradio', 'gateway': gw, 'peers': {nym: ids[name] for nym, name in peers.items() if name in ids}})
if gw: radio.pop('gateway_id', None)
else: radio['gateway_id'] = ids.get('$gateway')
c['radio'] = radio; json.dump(c, open(p, 'w'), indent=2); print('$h: radio', json.dumps(radio))
PY"
      run "$h" 'sudo systemctl enable -q --now bwkit-radio && sleep 2 && systemctl is-active bwkit-radio && journalctl -u bwkit-radio -n 2 --no-pager -o cat'
    done
  else
    echo "no RADIO_IDS given: the configs are untouched; run again with RADIO_IDS="bw-house=!... bw-neighbor=!..." from the ids printed above"
  fi
}

step_map() {
  say "the membership's map, signed by bw-mint, posted to the feed"
  run bw-mint 'sudo -u bwkit bwkit map post'
}

step_steward() {
  say "the Network Steward on bw-mint: the map, the key, the timer"
  COPYFILE_DISABLE=1 tar --no-xattrs -C "$kit" -cf - bwkit.py steward_rules.py install.sh systemd dashboard phase0/map.json | ssh_to bw-mint 'mkdir -p ~/kit && tar -C ~/kit -xf - && sudo install -m 0755 ~/kit/bwkit.py /usr/local/bin/bwkit && sudo install -m 0644 ~/kit/steward_rules.py /usr/local/bin/steward_rules.py && sudo install -d -m 0755 /usr/local/share/bwkit/dashboard && sudo install -m 0644 ~/kit/dashboard/index.html ~/kit/dashboard/*.css ~/kit/dashboard/*.js /usr/local/share/bwkit/dashboard/ && sudo rm -f /usr/local/share/bwkit/dashboard.html /usr/local/share/bwkit/dashboard/dashboard.html && sudo install -m 0644 ~/kit/systemd/*.service ~/kit/systemd/*.timer /etc/systemd/system/ && sudo install -m 0644 ~/kit/phase0/map.json /etc/bwkit/map.json && sudo systemctl daemon-reload && echo "bw-mint: bwkit $(grep -m1 "^VERSION" /usr/local/bin/bwkit | cut -d\" -f2), map installed"'
  run bw-mint 'sudo -u bwkit test -s /var/lib/bwkit/steward.priv && echo "bw-mint: the Steward already has a key, $(sudo -u bwkit bwkit steward nym)" || echo "bw-mint: Steward NYM $(sudo -u bwkit bwkit steward keygen)"'
  run bw-mint 'sudo systemctl restart bwkit && sleep 1 && echo "bw-mint: bwkit $(systemctl is-active bwkit), accepts postings by $(curl -s http://127.0.0.1:8710/status | python3 -c "import json,sys; print(json.load(sys.stdin)[\"steward\"])")"'
  run bw-mint 'sudo systemctl enable -q --now bwkit-steward.timer && sudo -u bwkit bwkit steward map && sudo -u bwkit bwkit steward run && sudo -u bwkit bwkit steward book'
  run bw-mint 'systemctl list-timers bwkit-steward.timer --no-pager | head -2'
}

check_status() {
  say "status reporter"
  run bw-mint 'systemctl list-timers bwkit-status.timer --no-pager | head -2; journalctl -u bwkit-status -n 2 --no-pager -o cat'
}

case "${1:-}" in
  upgrade) step_upgrade ;;
  time) step_time ;;
  tunnel) step_tunnel ;;
  logs) step_logs ;;
  kit) step_kit ;;
  update) step_update ;;
  status) step_status ;;
  map) step_map ;;
  radio) step_radio ;;
  steward) step_steward ;;
  check) check_time; check_tunnel; check_kit; check_status ;;
  all) step_upgrade; step_time; step_tunnel; step_logs; step_kit ;;
  *) sed -n '2,26p' "$0"; exit 2 ;;
esac
