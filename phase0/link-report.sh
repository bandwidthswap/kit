#!/bin/sh
# What the neighbor's link has done, in one short table from the Mac. It pulls the ten-minute link logs from bw-house
# and bw-neighbor (phase0/link-log.sh), the house's purchase records and fetch journal (bwkit-fetch, neighbor.sh) and
# the Mint's ledger, and prints, for the last DAYS days (default 7):
#
#   1. the clock between the sites: house minus neighbor, from each side's chrony offset at the same minute
#   2. each purchase: bytes, seconds, Mbit/s, and uplink bytes per metered byte on the house side
#   3. per day and per site, uplink bytes per metered byte from the ten-minute counters
#   4. each window on the Mint: the payer's count against the hop's, and the gap
#
# Plain text, and nothing it prints is an address: the sources are named by host, the exit node not at all.
#
#   phase0/link-report.sh [DAYS]
#
# Environment:
#   SSH_SUFFIX     appended to every node name for SSH (default -ts: the tailnet aliases in ~/.ssh/config)
#   NEIGH_SSH      another alias for bw-neighbor, as in neighbor.sh
#   HOP            the hop's name on the ledger (default bw-neighbor)
#   KEEP           a directory to keep the pulled files in, instead of a temporary one
set -eu
SSH="ssh -o ConnectTimeout=8 -o BatchMode=yes"
SSH_SUFFIX="${SSH_SUFFIX:--ts}"
N="${HOP:-bw-neighbor}"
days="${1:-7}"

host_of() { if [ "$1" = "$N" ] && [ -n "${NEIGH_SSH:-}" ]; then echo "$NEIGH_SSH"; else echo "$1$SSH_SUFFIX"; fi; }
run() { h="$(host_of "$1")"; shift; $SSH "$h" "$@"; }
pull() { node="$1"; cmd="$2"; out="$3"; run "$node" "$cmd" > "$out" 2>/dev/null || echo "$node: not reachable, or nothing there yet" >&2; }

d="${KEEP:-$(mktemp -d)}"
[ -n "${KEEP:-}" ] || trap 'rm -rf "$d"' EXIT
mkdir -p "$d"
pull bw-house 'sudo cat /var/lib/bwkit/linklog.jsonl' "$d/house.jsonl"
pull "$N"     'sudo cat /var/lib/bwkit/linklog.jsonl' "$d/neighbor.jsonl"
pull bw-house 'sudo cat /var/lib/bwkit/purchases.jsonl' "$d/purchases.jsonl"
pull bw-house 'journalctl -t bwkit-fetch -o short-unix --no-pager -q' "$d/fetch.log"
pull bw-mint  'sudo -u bwkit bwkit ledger' "$d/ledger.txt"

python3 - "$d" "$days" "$N" <<'PY'
import collections, json, re, sys, time

d, days, hop = sys.argv[1], int(sys.argv[2]), sys.argv[3]
since = time.time() - days * 86400
MB = 1e6
ADDR = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\b([0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.I)

def out(s=""):
    print(ADDR.sub("<addr>", s))             # nothing printed carries an address, whatever the inputs hold

def day(t): return time.strftime("%Y-%m-%d", time.localtime(t))
def hm(t): return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))

def jsonl(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return sorted(rows, key=lambda e: e.get("t", 0))

def text(path):
    try:
        with open(path) as f:
            return f.read().splitlines()
    except OSError:
        return []

house = [e for e in jsonl(d + "/house.jsonl") if e.get("t", 0) >= since]
neigh = [e for e in jsonl(d + "/neighbor.jsonl") if e.get("t", 0) >= since]
out("The neighbor's link, last %d days (%s)" % (days, hm(time.time())))

# 1. the clock between the sites
def by_slot(rows):
    slots = {}
    for e in rows:
        slots[int(round(e["t"] / 600.0))] = e        # the timers fire on the same ten-minute mark at both sites
    return slots
H, G = by_slot(house), by_slot(neigh)
pairs = collections.defaultdict(list)
for k in sorted(set(H) & set(G)):
    a, b = H[k], G[k]
    if a.get("offset") is None or b.get("offset") is None:
        continue
    pairs[day(a["t"])].append(((a["offset"] - b["offset"]) * 1e3, a["offset"] * 1e3, b["offset"] * 1e3))
out()
out("1. Clocks: house minus neighbor, in ms, from each side's chrony offset against its own NTP source at the same minute")
out("   (the difference of two estimates against two different references, not a measurement across the link)")
out("   %-11s %6s %8s %8s %8s   %-16s %-16s" % ("day", "pairs", "mean", "min", "max", "house vs NTP", "neighbor vs NTP"))
for dy in sorted(pairs):
    v = [p[0] for p in pairs[dy]]
    hr = max(abs(p[1]) for p in pairs[dy]); nr = max(abs(p[2]) for p in pairs[dy])
    out("   %-11s %6d %8.2f %8.2f %8.2f   within %-9s within %-9s" % (dy, len(v), sum(v) / len(v), min(v), max(v), "%.2f ms" % hr, "%.2f ms" % nr))
if not pairs:
    out("   no paired samples yet (%d lines from the house, %d from the neighbor)" % (len(house), len(neigh)))
unsynced = sum(1 for e in house + neigh if e.get("offset") is None)
if unsynced:
    out("   %d lines without a chrony reading (chrony down or unsynchronised at the time)" % unsynced)

# 2. the purchases
records = [e for e in jsonl(d + "/purchases.jsonl") if e.get("t", 0) >= since]
ends = [e["t"] + float(e.get("seconds", 0)) for e in records]
old = re.compile(r"^(\d+)\.\d+ \S+ bwkit-fetch\[\d+\]: purchase: (\d+) bytes in ([\d.]+) s(?:,|) .*?from (\S+?)(?:;|$)")
failed = collections.Counter()
fail_re = re.compile(r"^(\d+)\.\d+ \S+ bwkit-fetch\[\d+\]: source failed: (\S+) \(curl (\d+), http (\S+),")
for line in text(d + "/fetch.log"):
    m = fail_re.match(line)
    if m and int(m.group(1)) >= since:
        host = re.sub(r"^\w+://([^/]+).*$", r"\1", m.group(2))
        failed[(day(int(m.group(1))), host, "http %s" % m.group(4) if m.group(3) == "0" else "curl %s" % m.group(3))] += 1
        continue
    m = old.match(line)
    if not m or int(m.group(1)) < since:
        continue
    t_end, b, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    if any(abs(t_end - te) < 60 for te in ends):
        continue                                       # the same purchase, already in purchases.jsonl
    records.append({"t": t_end - s, "bytes": b, "seconds": s, "mbit_s": b * 8 / s / 1e6 if s else 0, "source": m.group(4)})
records.sort(key=lambda e: e["t"])

def host_of(url): return re.sub(r"^\w+://([^/]+).*$", r"\1", url or "?")
out()
out("2. Purchases: the house's hourly download through the neighbor's exit node, as curl and the house's counters saw it")
out("   (house side: met/pay is tailscale0 bytes received per byte delivered, the retransmissions inside the tunnel;")
out("    up/met is eth0 bytes received per tailscale0 byte received, the encapsulation and the relay; '-' before the counters were logged)")
out("   %-16s %7s %7s %7s %13s %9s %8s %8s  %s" % ("start", "MB", "s", "Mbit/s", "tailscale0 rx", "eth0 rx", "met/pay", "up/met", "source"))
for e in records:
    m = e.get("tailscale0") or {}; u = e.get("eth0") or {}
    ts_rx = ("%.1f" % (m["rx"] / MB)) if "rx" in m else "-"
    up_rx = ("%.1f" % (u["rx"] / MB)) if "rx" in u else "-"
    pay = ("%.3f" % (m["rx"] / e["bytes"])) if "rx" in m and e.get("bytes") else "-"
    ratio = ("%.3f" % e["uplink_per_metered"]) if "uplink_per_metered" in e else "-"
    out("   %-16s %7.1f %7.1f %7.2f %13s %9s %8s %8s  %s" % (hm(e["t"]), e["bytes"] / MB, float(e["seconds"]), float(e["mbit_s"]), ts_rx, up_rx, pay, ratio, host_of(e.get("source"))))
if not records:
    out("   no purchase recorded in the period")
if records:
    rates = [float(e["mbit_s"]) for e in records]
    out("   %d purchases; rate mean %.2f, min %.2f, max %.2f Mbit/s" % (len(records), sum(rates) / len(rates), min(rates), max(rates)))
if failed:
    out("   sources that failed before one served: " + ", ".join("%s %s %s x%d" % (k[0], k[1], k[2], n) for k, n in sorted(failed.items())))

# 3. uplink bytes per metered byte, per day and site, from the ten-minute counters
def deltas(rows, metered, uplink):
    per = collections.defaultdict(lambda: [0, 0, 0])   # metered rx+tx, uplink rx+tx, intervals
    for a, b in zip(rows, rows[1:]):
        try:
            dm = (b[metered][0] - a[metered][0]) + (b[metered][1] - a[metered][1])
            du = (b[uplink][0] - a[uplink][0]) + (b[uplink][1] - a[uplink][1])
        except (KeyError, TypeError, IndexError):
            continue
        if dm < 0 or du < 0 or b["t"] - a["t"] > 3600:
            continue                                   # a counter reset (reboot, interface recreated) or a long gap
        p = per[day(b["t"])]; p[0] += dm; p[1] += du; p[2] += 1
    return per
out()
out("3. Uplink bytes per metered byte, per day, from the ten-minute counters, both directions")
out("   (each uplink also carries the node's own traffic: the tailnet's control plane, time, ssh; a day of purchases dwarfs it)")
out("   %-11s %-9s %13s %11s %8s %10s" % ("day", "site", "tailscale0 MB", "uplink MB", "up/met", "intervals"))
for site, rows, up in (("house", house, "eth0"), ("neighbor", neigh, "wlan0")):
    per = deltas(rows, "tailscale0", up)
    for dy in sorted(per):
        dm, du, n = per[dy]
        out("   %-11s %-9s %13.1f %11.1f %8s %10d" % (dy, site, dm / MB, du / MB, ("%.3f" % (du / dm)) if dm else "-", n))
    if not per:
        out("   %-11s %-9s no two lines yet" % ("", site))

# 4. the windows on the Mint
win_hop = {}
settle = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d)\s+(SETTLED|DISPUTED)\s+(\d+\.\.\d+)\s+payer ([\d.]+) MB\s+hop ([\d.]+) MB")
ack = re.compile(r"^\S+ \S+\s+ack\s+from (\S+)\s+(\d+\.\.\d+)")
rows = []
for line in text(d + "/ledger.txt"):
    m = ack.match(line)
    if m:
        win_hop[m.group(2)] = m.group(1)
        continue
    m = settle.match(line)
    if not m or win_hop.get(m.group(3)) != hop:
        continue
    try:
        t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M"))
    except ValueError:
        continue
    if t < since:
        continue
    payer, hopmb = float(m.group(4)), float(m.group(5))
    rows.append((m.group(1), m.group(2).lower(), payer, hopmb, (hopmb - payer) / payer * 100 if payer else 0))
out()
out("4. Windows on the Mint with %s as the hop: the payer's count against the hop's, the gap as a share of the payer's" % hop)
out("   %-16s %-9s %9s %9s %7s" % ("settled at", "status", "payer MB", "hop MB", "gap"))
for r in rows:
    out("   %-16s %-9s %9.2f %9.2f %+6.1f%%" % r)
if not rows:
    out("   no window with %s on the ledger in the period" % hop)
else:
    gaps = [r[4] for r in rows]
    out("   %d windows, %d disputed; gap mean %+.1f%%, min %+.1f%%, max %+.1f%%" % (len(rows), sum(1 for r in rows if r[1] == "disputed"), sum(gaps) / len(gaps), min(gaps), max(gaps)))
PY
