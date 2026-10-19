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
#                       everything, in order: upgrade, time, logs, tailnet (three nodes), kit, the house side, check, and the
#                       neighbor's wifi last, with priority: if their network is in range from the house the Pi may switch
#                       to it at that point, which is harmless once everything else is set
#   phase0/neighbor.sh check    after the move: tailnet status, ping, the hop's status over the tailnet, the Mint's view
#   phase0/neighbor.sh fetch    (re)install the hourly purchase script and its timer on bw-house, nothing else
#   phase0/neighbor.sh update   copy a newer bwkit.py and units to bw-neighbor and restart its hop (the Mint first:
#                               SSH_SUFFIX=-ts UPDATE_HOSTS=bw-mint phase0/provision.sh update)
#   phase0/neighbor.sh announce write the link's terms (LINK_* below) and the on_decline hook into the hop's config, then
#                               `bwkit announce`: the signed announcement is registered with the Mint and governs every
#                               window that starts from now
#   phase0/neighbor.sh bench    put bw-house's payer back on bw-forest over wg0 (the bench as it was)
#
# Environment:
#   TS_AUTHKEY       required for prep: a Tailscale pre-authentication key from the admin console (one-off keys are best;
#                    it goes to each node once on stdin and is never echoed). Disable key expiry for the three nodes afterwards.
#   WIFI_SSID / WIFI_PSK   the neighbor's Wi-Fi, written as a NetworkManager profile that connects when the network is seen
#   FETCH_URLS       the house's hourly purchase from the neighbor: sources tried in order until one serves 100 MB
#                    (default: Cloudflare's speed test, then tele2's, then Hetzner's; Cloudflare answered 403 once)
#   MINT_LAN         the Mint's LAN address, which bw-house keeps using (default 192.168.0.61)
#   NODES            which nodes the tailnet step joins (default: bw-house bw-neighbor bw-mint); the Pi can be joined on
#                    its own from the neighbor's network, and the bench nodes later from the house LAN
#   SSH_SUFFIX       appended to every node name for SSH, e.g. "-ts" to use the tailnet aliases in ~/.ssh/config
#                    (bw-house-ts and the rest) once the Mac is on the tailnet and the nodes are reachable from anywhere
#   MINT_TS / NEIGH_TS   the Mint's and the neighbor's tailnet addresses, when the script cannot ask them itself
#   NEIGH_SSH        the SSH alias to reach bw-neighbor by when it is not on the Mac's network: bw-neighbor-away in
#                    ~/.ssh/config goes to its tailnet address through bw-house, once bw-house is on the tailnet
#   LINK_PRICE LINK_MARGIN LINK_MIN_BYTES LINK_PATIENCE LINK_MEDIUM LINK_RANGE
#                    the link's terms for `announce` (defaults: 0.10 USD/GB, 0.10, 65536, 0, wifi, building): the price per
#                    gigabyte, the agreement margin as a fraction, its floor in bytes, how many windows short of the hop's
#                    count beyond the margin it bears before declining a payer (0: never), the medium and the range class
#
# Steps, each usable on its own: upgrade time logs tailnet kit peers house fetch update announce check wifi bench
#
# Any other node away from the bench is provisioned the same way (Oct 19, 2026): N names it and ROLE its role, e.g.
#   N=bw-cabin SSH_SUFFIX=-ts phase0/neighbor.sh upgrade      (then time, logs, kit, peers, check; not house, which
#   repoints bw-house's payer at N, and not tailnet when the card joined the tailnet by itself: the site repo's
#   tools/card/apply-card.sh does that from ~/.config/bwkit/<node>.env). ROLE=mint installs a second Mint (bw-mint2),
#   whose peers and feed are its own and are not this script's.
#
# Nothing here prints or copies a private key. Tailscale's keys are made on each node; the node's NYM key is made by
# install.sh and stays on the node, which is the neighbor's. The Mac keeps SSH access through bw-house (phase0/NEIGHBOR.md).
set -eu
kit="$(cd "$(dirname "$0")/.." && pwd)"
SSH="ssh -o ConnectTimeout=8 -o BatchMode=yes"
N="${N:-bw-neighbor}"
ROLE="${ROLE:-hop}"
MINT_LAN="${MINT_LAN:-192.168.0.61}"
FETCH_URLS="${FETCH_URLS:-https://speed.cloudflare.com/__down?bytes=100000000 http://speedtest.tele2.net/100MB.zip https://speed.hetzner.de/100MB.bin}"
ts_ip() {
  case "$1" in bw-mint) [ -n "${MINT_TS:-}" ] && { echo "$MINT_TS"; return; } ;; "$N") [ -n "${NEIGH_TS:-}" ] && { echo "$NEIGH_TS"; return; } ;; esac
  run "$1" 'tailscale ip -4' | head -1
}

say() { printf '\n== %s\n' "$*"; }
host_of() { if [ "$1" = "$N" ] && [ -n "${NEIGH_SSH:-}" ]; then echo "$NEIGH_SSH"; else echo "$1$SSH_SUFFIX"; fi; }
run() { h="$(host_of "$1")"; shift; $SSH "$h" "$@"; }
shq() { printf "'%s'" "$(printf %s "$1" | sed "s/'/'\\\\''/g")"; }
sudo_sh() { h="$(host_of "$1")"; shift; a=""; for x in "$@"; do a="$a $(shq "$x")"; done; $SSH "$h" "sudo sh -s --$a"; }   # script on stdin, never as an ssh argument; the rest as $1..
SSH_SUFFIX="${SSH_SUFFIX:-}"

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
  printf '%s\n%s\n' "$WIFI_SSID" "${WIFI_PSK:-}" | $SSH "$(host_of $N)" 'read ssid; read psk; sudo nmcli connection delete neighbor >/dev/null 2>&1; sudo nmcli connection add type wifi con-name neighbor ifname wlan0 ssid "$ssid" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$psk" connection.autoconnect yes connection.autoconnect-priority 10 >/dev/null && echo "wifi profile written for $ssid (priority over the house network, so it wins when both are in range)"'
}

step_tailnet() {
  [ -n "${TS_AUTHKEY:-}" ] || { echo "TS_AUTHKEY=tskey-auth-... is required (a pre-authentication key from the Tailscale admin console)"; exit 2; }
  nodes="${NODES:-bw-house $N bw-mint}"
  say "tailnet: $nodes join; $N offers itself as an exit node"
  for h in $nodes; do
    run "$h" 'command -v tailscale >/dev/null || (curl -fsSL https://tailscale.com/install.sh | sudo sh >/dev/null); tailscale version | head -1'
  done
  case " $nodes " in *" $N "*) sudo_sh $N <<'EOS'
set -e
printf 'net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 1\n' > /etc/sysctl.d/99-bwkit-exit.conf
sysctl -q -p /etc/sysctl.d/99-bwkit-exit.conf
echo "$(hostname): forwarding on, for exit-node service"
EOS
  ;; esac
  for h in $nodes; do
    extra=""; [ "$h" = "$N" ] && extra="--advertise-exit-node"
    printf '%s\n' "$TS_AUTHKEY" | $SSH "$(host_of "$h")" "read k; sudo tailscale up --authkey \"\$k\" --hostname \"\$(hostname)\" $extra >/dev/null && echo \"\$(hostname): on the tailnet at \$(tailscale ip -4)\""
  done
  case " $nodes " in *" $N "*) echo "$N: exit node offered; approve it in the admin console, and disable key expiry for each node there";; esac
}

step_kit() {
  say "bwkit on $N as the $ROLE"
  COPYFILE_DISABLE=1 tar --no-xattrs -C "$kit" -cf - bwkit.py steward_rules.py install.sh systemd dashboard | $SSH "$(host_of $N)" 'mkdir -p ~/kit && tar -C ~/kit -xf -'
  run $N "cd ~/kit && sudo ./install.sh $ROLE"
  if [ "$ROLE" != hop ]; then
    run $N 'sudo systemctl enable -q --now bwkit && sleep 1 && systemctl is-active bwkit && sudo -u bwkit bwkit nym'
    return 0
  fi
  mint_ts="$(ts_ip bw-mint 2>/dev/null || true)"
  [ -n "$mint_ts" ] || { echo "the Mint's tailnet address is not known yet (bw-mint not reachable and MINT_TS not set); the hop's config gets it in the peers step"; mint_ts="MINT_TS_PENDING"; }
  run $N "sudo python3 - <<'PY'
import json; p='/etc/bwkit/config.json'; c=json.load(open(p)); c['mint']='http://$mint_ts:8710'; c['iface']='tailscale0'; json.dump(c,open(p,'w'),indent=2); print('hop config:',c)
PY"
  run $N 'sudo systemctl enable -q --now bwkit && sleep 1 && systemctl is-active bwkit'
}

step_peers() {
  say "NYMs exchanged among bw-house, $N and bw-mint; the hop's Mint address"
  mint_ts="$(ts_ip bw-mint)"
  run $N "sudo python3 - <<'PY'
import json; p='/etc/bwkit/config.json'; c=json.load(open(p)); c['mint']='http://$mint_ts:8710'; json.dump(c,open(p,'w'),indent=2); print('hop config:',c)
PY"
  neigh_nym="$(run $N 'sudo -u bwkit bwkit nym')"
  house_nym="$(run bw-house 'sudo -u bwkit bwkit nym')"
  run $N "sudo -u bwkit bwkit peer add bw-house '$house_nym'"
  for h in bw-house bw-mint; do run "$h" "sudo -u bwkit bwkit peer add $N '$neigh_nym'"; done
  run $N 'sudo systemctl restart bwkit && sleep 1 && systemctl is-active bwkit'
}

step_house() {
  say "the house buys from the neighbor: payer on tailscale0, hop $N, an hourly purchase through the exit node"
  neigh_ts="$(ts_ip $N)"
  run bw-house "sudo cp -n /etc/bwkit/config.json /etc/bwkit/config.json.bench; sudo python3 - <<'PY'
import json; p='/etc/bwkit/config.json'; c=json.load(open(p)); c['hop_name']='$N'; c['hop_url']='http://$neigh_ts:8710'; c['iface']='tailscale0'; json.dump(c,open(p,'w'),indent=2); print('payer config:',c)
PY"
  step_fetch
  say "baseline sample on payer and hop (the first window starts now)"
  run bw-house 'sudo -u bwkit bwkit sample'
  run $N 'sudo -u bwkit bwkit sample'
  run bw-house 'sudo systemctl restart bwkit-receipt.timer; systemctl list-timers bwkit-receipt.timer bwkit-fetch.timer --no-pager | head -3'
}

step_fetch() {
  say "the purchase script and its timer on bw-house (sources, in order: $FETCH_URLS)"
  neigh_ts="$(ts_ip $N)"
  # The script and the units go over verbatim (the outer heredoc is quoted); the exit node and the sources arrive as $1..
  sudo_sh bw-house "$neigh_ts" $FETCH_URLS <<'EOS'
set -e
neigh_ts="$1"; shift; urls="$*"
cat > /usr/local/bin/bwkit-fetch <<'EOC'
#!/bin/sh
# The house's hourly purchase from the neighbor: one download with bw-neighbor as the exit node, so real bytes cross
# the metered interface on both ends. Sources are tried in order until one serves the file; the LAN stays reachable
# meanwhile, and the exit node is dropped afterwards whatever happened. A purchase counts only on HTTP 200 with more
# than 1 MB to show for it. Around each try the byte counters of the metered interface and of the uplink are read, so
# the purchase line in the journal shows the rate the download got and how many uplink bytes each metered byte cost
# (encapsulation and the relayed path's retransmissions); the same goes as one JSON line to purchases.jsonl under
# /var/lib/bwkit, which outlives the journal (in RAM on this node). phase0/link-report.sh in the kit reads both.
exit_node="$1"; shift
metered="${METERED:-tailscale0}"; uplink="${UPLINK:-eth0}"
record="${BWKIT_DIR:-/var/lib/bwkit}/purchases.jsonl"
counters() { cat "/sys/class/net/$metered/statistics/rx_bytes" "/sys/class/net/$metered/statistics/tx_bytes" "/sys/class/net/$uplink/statistics/rx_bytes" "/sys/class/net/$uplink/statistics/tx_bytes" 2>/dev/null | tr '\n' ' '; }
tailscale set --exit-node="$exit_node" --exit-node-allow-lan-access=true || { logger -t bwkit-fetch "purchase skipped: could not set the exit node $exit_node"; exit 1; }
ok=1
for url in "$@"; do
  before="$(counters)"; t0="$(date +%s)"
  out="$(curl -s -o /dev/null -A "bwkit-fetch/0.1 (+https://bandwidthswap.com)" --max-time 900 -w '%{http_code} %{size_download} %{time_total}' "$url")"; rc=$?
  after="$(counters)"
  set -- $out; code="${1:-?}"; bytes="${2:-0}"; secs="${3:-0}"
  set -- $before; m_rx0="${1:-0}"; m_tx0="${2:-0}"; u_rx0="${3:-0}"; u_tx0="${4:-0}"
  set -- $after; m_rx=$(( ${1:-0} - m_rx0 )); m_tx=$(( ${2:-0} - m_tx0 )); u_rx=$(( ${3:-0} - u_rx0 )); u_tx=$(( ${4:-0} - u_tx0 ))
  if [ "$rc" = 0 ] && [ "$code" = 200 ] && [ "$bytes" -gt 1000000 ]; then
    rate="$(awk -v b="$bytes" -v s="$secs" 'BEGIN { printf "%.2f", (s > 0) ? b * 8 / s / 1e6 : 0 }')"
    ratio="$(awk -v a="$u_rx" -v b="$m_rx" 'BEGIN { printf "%.3f", (b > 0) ? a / b : 0 }')"
    logger -t bwkit-fetch "purchase: $bytes bytes in $secs s, $rate Mbit/s, through $exit_node from $url; $metered rx +$m_rx tx +$m_tx, $uplink rx +$u_rx tx +$u_tx, uplink/metered $ratio"
    [ -f "$record" ] || install -o bwkit -g bwkit -m 0644 /dev/null "$record"
    printf '{"t":%s,"bytes":%s,"seconds":%s,"mbit_s":%s,"source":"%s","%s":{"rx":%s,"tx":%s},"%s":{"rx":%s,"tx":%s},"uplink_per_metered":%s}\n' \
      "$t0" "$bytes" "$secs" "$rate" "$url" "$metered" "$m_rx" "$m_tx" "$uplink" "$u_rx" "$u_tx" "$ratio" >> "$record"
    ok=0; break
  else
    logger -t bwkit-fetch "source failed: $url (curl $rc, http $code, $bytes bytes)"
  fi
done
tailscale set --exit-node=
exit $ok
EOC
chmod 755 /usr/local/bin/bwkit-fetch
cat > /etc/systemd/system/bwkit-fetch.service <<EOC
[Unit]
Description=Bandwidth Swap: the house's hourly purchase from the neighbor
[Service]
Type=oneshot
ExecStart=/usr/local/bin/bwkit-fetch $neigh_ts $urls
EOC
cat > /etc/systemd/system/bwkit-fetch.timer <<'EOC'
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
echo "bw-house: bwkit-fetch.timer enabled ($urls)"
EOS
}

step_update() {
  say "bwkit.py and the units to $N, the hop restarted (the Mint should already run the newer kit)"
  COPYFILE_DISABLE=1 tar --no-xattrs -C "$kit" -cf - bwkit.py steward_rules.py install.sh systemd dashboard | $SSH "$(host_of $N)" 'mkdir -p ~/kit && tar -C ~/kit -xf - && sudo install -m 0755 ~/kit/bwkit.py /usr/local/bin/bwkit && sudo install -m 0644 ~/kit/steward_rules.py /usr/local/bin/steward_rules.py && sudo install -d -m 0755 /usr/local/share/bwkit/dashboard && sudo install -m 0644 ~/kit/dashboard/index.html ~/kit/dashboard/*.css ~/kit/dashboard/*.js /usr/local/share/bwkit/dashboard/ && sudo rm -f /usr/local/share/bwkit/dashboard.html /usr/local/share/bwkit/dashboard/dashboard.html && sudo install -m 0644 ~/kit/systemd/*.service ~/kit/systemd/*.timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart bwkit && sleep 1 && echo "$(hostname): bwkit $(grep -m1 "^VERSION" /usr/local/bin/bwkit | cut -d\" -f2), server $(systemctl is-active bwkit)"'
  run $N 'curl -s http://127.0.0.1:8710/status | head -c 400; echo'
}

step_announce() {
  price="${LINK_PRICE:-0.10}"; margin="${LINK_MARGIN:-0.10}"; minb="${LINK_MIN_BYTES:-65536}"; patience="${LINK_PATIENCE:-0}"
  medium="${LINK_MEDIUM:-wifi}"; range="${LINK_RANGE:-building}"
  say "the link's terms on $N: $price USD/GB, margin $margin, min_bytes $minb, patience $patience, $medium/$range; the on_decline hook"
  # The hook: declining a receipt costs the hop the window and the payer nothing, so the remedy with teeth is to stop
  # offering the exit node. It runs as the bwkit user, so one sudoers line lets it run that one command and no other.
  sudo_sh $N "$price" "$margin" "$minb" "$patience" "$medium" "$range" <<'EOS'
set -e
price="$1"; margin="$2"; minb="$3"; patience="$4"; medium="$5"; range="$6"
ts="$(command -v tailscale || echo /usr/bin/tailscale)"
cat > /usr/local/bin/bwkit-on-decline <<EOC
#!/bin/sh
# bwkit's on_decline hook: the hop has stopped serving the payer named in \$1 (its patience is spent). A declined receipt
# costs the hop the window and the payer nothing, so this withdraws the service the receipts pay for: the exit node.
logger -t bwkit-decline "payer \$1 declined; withdrawing the exit node"
sudo -n $ts set --advertise-exit-node=false
EOC
chmod 755 /usr/local/bin/bwkit-on-decline
printf 'bwkit ALL=(root) NOPASSWD: %s set --advertise-exit-node=false\n' "$ts" > /etc/sudoers.d/bwkit-decline
chmod 440 /etc/sudoers.d/bwkit-decline
visudo -cf /etc/sudoers.d/bwkit-decline >/dev/null
python3 - "$price" "$margin" "$minb" "$patience" "$medium" "$range" <<'PYC'
import json, sys
p = '/etc/bwkit/config.json'
c = json.load(open(p))
c['link'] = {'price': float(sys.argv[1]), 'margin': float(sys.argv[2]), 'min_bytes': int(sys.argv[3]), 'patience_windows': int(sys.argv[4]),
             'medium': sys.argv[5], 'range_class': sys.argv[6]}
c['on_decline'] = '/usr/local/bin/bwkit-on-decline'
json.dump(c, open(p, 'w'), indent=2)
print('hop config: link', c['link'], 'on_decline', c['on_decline'])
PYC
systemctl restart bwkit; sleep 1; echo "$(hostname): bwkit $(systemctl is-active bwkit)"
EOS
  run $N 'sudo -u bwkit bwkit announce'
  say "the Mint's registry"
  run bw-mint 'sudo -u bwkit bwkit links'
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
  prep)  step_upgrade; step_time; step_logs; step_tailnet; step_kit; step_peers; step_house; step_check; step_wifi ;;
  check) step_check ;;
  bench) step_bench ;;
  upgrade|time|logs|wifi|tailnet|kit|peers|house|fetch|update|announce) "step_$1" ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
