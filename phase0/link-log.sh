#!/bin/sh
# A ten-minute log of the neighbor's link, kept on both ends, so a week of it answers what the paper calls unmeasured:
# how far apart the two sites' clocks drift (Section 3.5, Section 10), and how many bytes each side's uplink spends
# per byte that crosses the metered interface (encapsulation and the retransmissions of the relayed path).
#
# On bw-house and bw-neighbor a `bwkit-linklog` timer appends one line every ten minutes, on the same minute at
# both sites, to /var/lib/bwkit/linklog.jsonl:
#
#   {"t":1792305600,"offset":-0.000603,"root_delay":0.045329,"stratum":3,"tailscale0":[rx,tx],"eth0":[rx,tx]}
#
# `t` is Unix seconds; `offset` is this clock minus NTP time in seconds as chrony estimates it (positive: the clock
# is ahead), `root_delay` and `stratum` are chrony's; then the rx and tx byte counters of the metered interface and
# of the uplink (eth0 on bw-house, wlan0 on bw-neighbor), raw, so the file holds no addresses and nothing derived.
# About 140 bytes a line, 144 lines a day, 600 KB a month. phase0/link-report.sh reads both files from the Mac.
#
#   phase0/link-log.sh            install the timer on bw-house (uplink eth0) and bw-neighbor (uplink wlan0)
#   phase0/link-log.sh status     the timers, and the last line on each node
#
# Environment:
#   SSH_SUFFIX     appended to every node name for SSH (default -ts: the tailnet aliases in ~/.ssh/config)
#   NEIGH_SSH      another alias for bw-neighbor, as in neighbor.sh
#   UPLINK_HOUSE / UPLINK_NEIGHBOR   the uplink interface on each (default eth0 / wlan0)
#   NODES          which nodes to install on (default: bw-house bw-neighbor)
set -eu
SSH="ssh -o ConnectTimeout=8 -o BatchMode=yes"
SSH_SUFFIX="${SSH_SUFFIX:--ts}"
N="bw-neighbor"
UPLINK_HOUSE="${UPLINK_HOUSE:-eth0}"
UPLINK_NEIGHBOR="${UPLINK_NEIGHBOR:-wlan0}"

say() { printf '\n== %s\n' "$*"; }
host_of() { if [ "$1" = "$N" ] && [ -n "${NEIGH_SSH:-}" ]; then echo "$NEIGH_SSH"; else echo "$1$SSH_SUFFIX"; fi; }
run() { h="$(host_of "$1")"; shift; $SSH "$h" "$@"; }
sudo_sh() { h="$(host_of "$1")"; shift; $SSH "$h" sudo sh -s -- "$@"; }   # script on stdin, arguments as $1..

uplink_of() { case "$1" in bw-house) echo "$UPLINK_HOUSE";; "$N") echo "$UPLINK_NEIGHBOR";; *) echo eth0;; esac; }

step_install() {
  for h in ${NODES:-bw-house $N}; do
    say "$h: bwkit-linklog every ten minutes, metered tailscale0, uplink $(uplink_of "$h")"
    sudo_sh "$h" tailscale0 "$(uplink_of "$h")" <<'EOS'
set -e
metered="$1"; uplink="$2"
cat > /usr/local/bin/bwkit-linklog <<'EOC'
#!/bin/sh
# One line every ten minutes: this clock against NTP as chrony sees it, and the byte counters of the metered
# interface and the uplink. phase0/link-log.sh in the kit installs it; phase0/link-report.sh reads it.
metered="${1:-tailscale0}"; uplink="${2:-eth0}"
out="${BWKIT_DIR:-/var/lib/bwkit}/linklog.jsonl"
tracking="$(chronyc -c tracking 2>/dev/null)" || { logger -t bwkit-linklog "chronyc tracking failed; counters only"; tracking=""; }
counters() { cat "/sys/class/net/$1/statistics/rx_bytes" "/sys/class/net/$1/statistics/tx_bytes" 2>/dev/null | tr '\n' ' '; }
# chronyc -c tracking: ref_id, ref_ip, stratum, ref_time, system_time, last_offset, rms_offset, freq, resid_freq, skew,
# root_delay, root_dispersion, update_interval, leap. Its system_time is positive when the clock is slow of NTP;
# the line keeps clock minus NTP, so the sign is flipped and positive means ahead.
printf '%s\n' "$tracking" | awk -F, -v t="$(date +%s)" -v m="$metered" -v mc="$(counters "$metered")" -v u="$uplink" -v uc="$(counters "$uplink")" '
  function num(s) { return (s == "") ? "null" : s }
  {
    off = "null"; rd = "null"; st = "null"
    if (NF >= 11) { off = (substr($5, 1, 1) == "-") ? substr($5, 2) : "-" $5; rd = $11; st = $3 }
    split(mc, a, " "); split(uc, b, " ")
    printf "{\"t\":%s,\"offset\":%s,\"root_delay\":%s,\"stratum\":%s,\"%s\":[%s,%s],\"%s\":[%s,%s]}\n", t, off, rd, st, m, num(a[1]), num(a[2]), u, num(b[1]), num(b[2])
  }' >> "$out"
EOC
chmod 755 /usr/local/bin/bwkit-linklog
cat > /etc/systemd/system/bwkit-linklog.service <<EOC
[Unit]
Description=bwkit: one line of this clock against NTP and the link's byte counters

[Service]
Type=oneshot
User=bwkit
Group=bwkit
ExecStart=/usr/local/bin/bwkit-linklog $metered $uplink
EOC
cat > /etc/systemd/system/bwkit-linklog.timer <<'EOC'
[Unit]
Description=bwkit: the link's clock and counters every ten minutes, on the minute at both sites

[Timer]
OnCalendar=*:0/10
AccuracySec=1s
Persistent=false

[Install]
WantedBy=timers.target
EOC
systemctl daemon-reload
systemctl enable -q --now bwkit-linklog.timer
systemctl start bwkit-linklog.service
echo "$(hostname): bwkit-linklog.timer enabled (metered $metered, uplink $uplink); first line:"
tail -1 /var/lib/bwkit/linklog.jsonl
EOS
  done
}

step_status() {
  for h in ${NODES:-bw-house $N}; do
    say "$h"
    run "$h" 'systemctl list-timers bwkit-linklog.timer --no-pager | head -2; sudo wc -l /var/lib/bwkit/linklog.jsonl; sudo tail -1 /var/lib/bwkit/linklog.jsonl'
  done
}

case "${1:-install}" in
  install) step_install ;;
  status)  step_status ;;
  *) sed -n '2,24p' "$0"; exit 2 ;;
esac
