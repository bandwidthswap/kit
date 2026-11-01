#!/bin/sh
# The dispute tests, from the Mac. Needs the tunnel and bwkit running on
# the payer and the hop. Two windows, both forced now rather than at the
# top of the hour:
#
#   1. MB megabytes through the tunnel, then a receipt: both meters agree -> settled.
#   2. MB more, then the tunnel interface is restarted on one node so its
#      counters start over, then a receipt: the side that restarted signs
#      fewer bytes than the other -> disputed, settled at the smaller count,
#      by the Mint; then again offline on the Mint from the two signed files
#      and the announcement in force; then again on the hop from its own
#      copies alone (`bwkit settle --window`).
#
#   phase0/dispute-test.sh hop      the hop under-claims: the interface restarts on the hop (the first test, Oct 16)
#   phase0/dispute-test.sh payer    the hop over-claims: the interface restarts on the payer (the second test, Oct 16)
#   phase0/dispute-test.sh resolve  no new window: only the last two steps, on the latest disputed window the Mint holds
#
# The link is named by variables, with the bench as the default:
#
#   HOP=bw-forest        the hop's host name (ssh target, with SSH_SUFFIX added)
#   IFACE=wg0            the tunnel interface the kit meters on both ends; tailscale0 for the tailnet
#   TARGET=10.77.0.2     the hop's address on that tunnel, where the push lands
#   PAYER=bw-house  MINT=bw-mint  SSH_SUFFIX=  MB=20
#
# The neighbor link, from a Mac with the -ts aliases:
#   HOP=bw-neighbor IFACE=tailscale0 TARGET=100.71.120.70 SSH_SUFFIX=-ts phase0/dispute-test.sh
#
# Restarting wg0 is `systemctl restart wg-quick@wg0`; restarting tailscale0 is
# `systemctl restart tailscaled`, which also drops this script's own ssh
# session when it goes over the tailnet, so that step runs detached, and the
# script waits for the node and then for the hop to answer the payer before
# it asks for the receipt (a receipt the hop never acks is a lost window).
# Run it away from the top of the hour and from the payer's purchase (the
# fetch timer): a restart under the hourly receipt or the download would
# take them with it.
#
# Nothing is faked in the daemon and no count is set by hand: the meter that
# restarted really did see fewer bytes. The hop has no hook for signing a
# count of its own choosing, deliberately, so "the hop over-claims" is made
# by giving the payer the smaller honest count. The rule is the same either
# way: min(payer, hop).
set -eu
SSH="ssh -o ConnectTimeout=8 -o BatchMode=yes"
MB="${MB:-20}"
HOP="${HOP:-bw-forest}"; IFACE="${IFACE:-wg0}"; TARGET="${TARGET:-10.77.0.2}"
PAYER="${PAYER:-bw-house}"; MINT="${MINT:-bw-mint}"; SSH_SUFFIX="${SSH_SUFFIX:-}"
mode="${1:-hop}"
case "$mode" in
  hop)     reset=$HOP;   side="the hop under-claims";  wins="hop" ;;
  payer)   reset=$PAYER; side="the hop over-claims";   wins="payer" ;;
  resolve) reset=;       side=;                        wins="smaller side" ;;
  *) echo "usage: [HOP=.. IFACE=.. TARGET=.. SSH_SUFFIX=..] $0 [hop|payer|resolve]" >&2; exit 2 ;;
esac
case "$IFACE" in
  tailscale*) restart="sudo systemctl restart tailscaled" ;;
  *)          restart="sudo systemctl restart wg-quick@$IFACE" ;;
esac

say() { printf '\n== %s\n' "$*"; }
host_of() { printf '%s%s' "$1" "$SSH_SUFFIX"; }
counters() { $SSH "$(host_of "$1")" "cat /sys/class/net/$IFACE/statistics/rx_bytes /sys/class/net/$IFACE/statistics/tx_bytes | tr '\n' ' '; echo"; }

push() {   # $1 megabytes from the payer to the hop over the tunnel
  $SSH "$(host_of "$HOP")" "python3 - <<'EOP' > /tmp/bwkit-recv.log 2>&1 &
import socket
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('$TARGET', 9009)); s.listen(1); c, _ = s.accept(); n = 0
while True:
    b = c.recv(1 << 16)
    if not b: break
    n += len(b)
print('received', n)
EOP
sleep 1"
  $SSH "$(host_of "$PAYER")" "python3 -c \"
import socket, sys
c = socket.create_connection(('$TARGET', 9009), timeout=30); chunk = b'x' * (1 << 16)
left = $1 * 1000 * 1000
while left > 0:
    c.sendall(chunk[:min(left, len(chunk))]); left -= min(left, len(chunk))
c.close(); print('sent', $1, 'MB')\""
  sleep 2
  $SSH "$(host_of "$HOP")" 'cat /tmp/bwkit-recv.log'
}

wait_for() {   # $1 host: until ssh answers again, up to 90 s
  i=0
  until $SSH "$(host_of "$1")" true 2>/dev/null; do
    i=$((i + 1)); [ "$i" -lt 30 ] || { echo "$1 did not come back"; exit 1; }
    sleep 3
  done
}

wait_for_hop() {   # until the hop answers the payer over the tunnel, up to 90 s
  i=0
  until $SSH "$(host_of "$PAYER")" "python3 -c \"
import json, urllib.request
u = json.load(open('/etc/bwkit/config.json'))['hop_url'].rstrip('/') + '/status'
urllib.request.urlopen(u, timeout=5).read()\"" 2>/dev/null; do
    i=$((i + 1)); [ "$i" -lt 30 ] || { echo "the hop does not answer $PAYER over the tunnel"; exit 1; }
    sleep 3
  done
}

receipt() { $SSH "$(host_of "$PAYER")" 'sudo -u bwkit bwkit receipt' | sed 's/^/  payer: /'; }

if [ "$mode" != resolve ]; then
say "window 1: $MB MB from $PAYER to $HOP at $TARGET over $IFACE, then a receipt"
push "$MB"
receipt

say "window 2 ($side): $MB MB more, then $IFACE restarts on $reset (its counters start over), then a receipt"
push "$MB"
printf '  %s counters before: ' "$reset"; counters "$reset"
$SSH "$(host_of "$reset")" "nohup sh -c '$restart' >/dev/null 2>&1 &"
sleep 4
wait_for "$reset"
printf '  %s counters after:  ' "$reset"; counters "$reset"
wait_for_hop
receipt
fi

say "the Mint's view (expected: DISPUTED, settled at the $wins's count)"
$SSH "$(host_of "$MINT")" 'sudo -u bwkit bwkit ledger | tail -6'
say "the dispute, resolved again on the Mint from the two signed files and the announcement in force"
$SSH "$(host_of "$MINT")" 'sudo -u bwkit bwkit ledger --json | python3 -c "
import importlib.machinery, importlib.util, json, sys
ld = importlib.machinery.SourceFileLoader(\"bwkit\", \"/usr/local/bin/bwkit\"); bw = importlib.util.module_from_spec(importlib.util.spec_from_loader(\"bwkit\", ld)); ld.exec_module(bw)
rows = [json.loads(l) for l in sys.stdin]
s = [r for r in rows if r[\"kind\"] == \"settlement\" and r[\"status\"] == \"disputed\"][-1]
rec = [r for r in rows if r.get(\"sig\") == s[\"receipt_sig\"]][0]
ack = [r for r in rows if r.get(\"sig\") == s[\"ack_sig\"]][0]
json.dump({\"kind\": \"receipt\", \"receipt\": rec[\"receipt\"], \"sig\": rec[\"sig\"]}, open(\"/tmp/r.json\", \"w\"))
json.dump({\"kind\": \"ack\", \"receipt\": ack[\"receipt\"], \"sig\": ack[\"sig\"]}, open(\"/tmp/a.json\", \"w\"))
links = [l for l in json.load(__import__(\"urllib.request\").request.urlopen(\"http://127.0.0.1:8710/links\", timeout=5)) if bw.link_hash(l) == s.get(\"link\")] if s.get(\"link\") else []
open(\"/tmp/l.json\", \"w\").write(json.dumps(links[0]) if links else \"\")
open(\"/tmp/w\", \"w\").write(str(s[\"t_first\"]))
print(\"payer signed\", rec[\"receipt\"][\"bytes\"], \"hop signed\", ack[\"receipt\"][\"bytes\"], \"mint settled\", s[\"settled_bytes\"], s[\"status\"], \"margin\", s.get(\"margin\"), \"window\", s[\"t_first\"])
" && bwkit verify /tmp/r.json /tmp/a.json && if [ -s /tmp/l.json ]; then bwkit settle /tmp/r.json /tmp/a.json --link /tmp/l.json; else bwkit settle /tmp/r.json /tmp/a.json; fi | grep -E "settled_bytes|status|rule|margin"'
say "and once more on $HOP, from the hop's own copies alone"
w=$($SSH "$(host_of "$MINT")" 'cat /tmp/w')
$SSH "$(host_of "$HOP")" "sudo -u bwkit bwkit settle --window $w"
