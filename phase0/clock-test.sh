#!/bin/sh
# The clock rule on real hardware, from the Mac (backlog B9: 5 seconds, decline).
# Needs the lab running (phase0/provision.sh all).
#
#   1. chronyc tracking on the three nodes, before.
#   2. On bw-forest: chrony stopped and the clock stepped STEP seconds ahead (10).
#   3. A receipt from bw-house: the hop declines with `clock`, the payer's
#      log and the hop's journal both say so, and the Mint holds the receipt
#      with no ack (it stays that way: the window is never settled).
#   4. chrony back on bw-forest, and how long until it has stepped the clock back.
#   5. One more receipt: settled. The hop's window is the two windows together
#      (its sample did not move when it declined), so with real traffic in the
#      declined window this one would be disputed and settled at the payer's
#      count: the hop loses the window it declined.
#
# chrony is started again on bw-forest however the script ends.
set -eu
SSH="ssh -o ConnectTimeout=8 -o BatchMode=yes"
STEP="${STEP:-10}"

say() { printf '\n== %s\n' "$*"; }
tracking() {
  for h in bw-house bw-forest bw-mint; do
    printf '%s: ' "$h"
    $SSH "$h" 'chronyc tracking | grep -E "^(Stratum|System time|Last offset|RMS offset|Frequency|Skew)" | sed -E "s/ +: /=/; s/ seconds.*//; s/ +/ /g" | tr "\n" ";"'
    echo
  done
}
restore() { $SSH bw-forest 'sudo systemctl start chrony' 2>/dev/null || true; }
trap restore EXIT

say "before"
tracking

say "bw-forest: chrony stopped, clock stepped $STEP s ahead"
$SSH bw-forest "sudo systemctl stop chrony && sudo date -s @\$((\$(date +%s) + $STEP)) >/dev/null && printf 'forest %s\n' \$(date +%s.%N)"
$SSH bw-house "printf 'house  %s\n' \$(date +%s.%N)"

say "a receipt from bw-house: the hop should decline with clock"
$SSH bw-house 'sudo -u bwkit bwkit receipt' | sed 's/^/  house: /'
say "the hop's journal"
$SSH bw-forest 'journalctl -u bwkit --no-pager -n 2 -o short-iso | grep -v "^--"'
say "the Mint: the receipt is held, no ack"
$SSH bw-mint 'sudo -u bwkit bwkit status | tail -1'

say "chrony back on bw-forest; seconds until it steps the clock back"
$SSH bw-forest 'sudo systemctl start chrony
i=0
while [ $i -lt 90 ]; do
  if journalctl -u chrony --since "-3min" --no-pager -q 2>/dev/null | grep -q "clock was stepped"; then break; fi
  i=$((i+1)); sleep 1
done
journalctl -u chrony --since "-3min" --no-pager -q -o short-iso | grep -E "stepped|Selected source" | sed "s/^/  /"
echo "  stepped back after about $i s"
sleep 3'
say "after"
tracking

say "one more receipt: settled"
$SSH bw-house 'sudo -u bwkit bwkit receipt' | sed 's/^/  house: /'
say "the Mint's view"
$SSH bw-mint 'sudo -u bwkit bwkit ledger | tail -4; sudo -u bwkit bwkit status | tail -1'
