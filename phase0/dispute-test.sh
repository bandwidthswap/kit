#!/bin/sh
# The first dispute test, from the Mac. Needs the tunnel and bwkit running
# (phase0/provision.sh all). Two windows, both forced now rather than at the
# top of the hour:
#
#   1. 20 MB through the tunnel, then a receipt: both meters agree -> settled.
#   2. 20 MB more, then wg0 is restarted on bw-forest so its counters start
#      over, then a receipt: the hop signs fewer bytes than the payer ->
#      disputed, settled at the hop's count, by the Mint and again offline
#      from the two signed files alone.
#
# Nothing is faked in the daemon: the hop's meter really did see fewer bytes.
set -eu
SSH="ssh -o ConnectTimeout=8 -o BatchMode=yes"
MB="${MB:-20}"

say() { printf '\n== %s\n' "$*"; }

push() {   # $1 megabytes from bw-house to bw-forest over wg0
  $SSH bw-forest "python3 - <<'EOP' > /tmp/bwkit-recv.log 2>&1 &
import socket
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('10.77.0.2', 9009)); s.listen(1); c, _ = s.accept(); n = 0
while True:
    b = c.recv(1 << 16)
    if not b: break
    n += len(b)
print('received', n)
EOP
sleep 1"
  $SSH bw-house "python3 -c \"
import socket, sys
c = socket.create_connection(('10.77.0.2', 9009), timeout=30); chunk = b'x' * (1 << 16)
left = $1 * 1000 * 1000
while left > 0:
    c.sendall(chunk[:min(left, len(chunk))]); left -= min(left, len(chunk))
c.close(); print('sent', $1, 'MB')\""
  sleep 2
  $SSH bw-forest 'cat /tmp/bwkit-recv.log'
}

receipt() { $SSH bw-house 'sudo -u bwkit bwkit receipt' | sed 's/^/  house: /'; }

say "window 1: $MB MB through the tunnel, then a receipt"
push "$MB"
receipt

say "window 2: $MB MB more, then wg0 restarts on bw-forest (its counters start over), then a receipt"
push "$MB"
$SSH bw-forest 'sudo systemctl restart wg-quick@wg0 && sleep 3 && sudo wg show wg0 transfer'
receipt

say "the Mint's view"
$SSH bw-mint 'sudo -u bwkit bwkit ledger | tail -6'
say "the dispute, resolved again from the two signed files alone"
$SSH bw-mint 'sudo -u bwkit bwkit ledger --json | python3 -c "
import json, sys
rows = [json.loads(l) for l in sys.stdin]
s = [r for r in rows if r[\"kind\"] == \"settlement\" and r[\"status\"] == \"disputed\"][-1]
rec = [r for r in rows if r.get(\"sig\") == s[\"receipt_sig\"]][0]
ack = [r for r in rows if r.get(\"sig\") == s[\"ack_sig\"]][0]
json.dump({\"kind\": \"receipt\", \"receipt\": rec[\"receipt\"], \"sig\": rec[\"sig\"]}, open(\"/tmp/r.json\", \"w\"))
json.dump({\"kind\": \"ack\", \"receipt\": ack[\"receipt\"], \"sig\": ack[\"sig\"]}, open(\"/tmp/a.json\", \"w\"))
print(\"payer signed\", rec[\"receipt\"][\"bytes\"], \"hop signed\", ack[\"receipt\"][\"bytes\"], \"mint settled\", s[\"settled_bytes\"], s[\"status\"])
" && bwkit verify /tmp/r.json /tmp/a.json && bwkit settle /tmp/r.json /tmp/a.json | grep -E "settled_bytes|status|rule"'
