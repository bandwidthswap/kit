#!/bin/sh
# Install bwkit on one node. Run as root on the node, from a checkout of the kit:
#
#   sudo ./install.sh payer|hop|mint
#
# Puts bwkit.py at /usr/local/bin/bwkit (with steward_rules.py beside it), makes the bwkit system user and
# /var/lib/bwkit, writes a first /etc/bwkit/config.json for the role if none
# exists (the pilot's defaults: the hop's link terms, the Mint's price and the
# Steward's fee), installs the systemd units and generates the node's key. It does
# not start anything: see README.md for the peer exchange, the baseline
# sample and the enable step.
set -eu
role="${1:-}"
case "$role" in payer|hop|mint) ;; *) echo "usage: $0 payer|hop|mint" >&2; exit 2;; esac
here="$(cd "$(dirname "$0")" && pwd)"

apt-get -o DPkg::Lock::Timeout=600 install -y --no-install-recommends python3 python3-cryptography >/dev/null
id bwkit >/dev/null 2>&1 || useradd --system --home-dir /var/lib/bwkit --shell /usr/sbin/nologin bwkit
install -d -o bwkit -g bwkit -m 0750 /var/lib/bwkit
install -d -m 0755 /etc/bwkit
install -m 0755 "$here/bwkit.py" /usr/local/bin/bwkit
install -m 0644 "$here/steward_rules.py" /usr/local/bin/steward_rules.py   # imported by bwkit from its own directory
install -d -m 0755 /usr/local/share/bwkit/dashboard
install -m 0644 "$here"/dashboard/*.html "$here"/dashboard/*.css "$here"/dashboard/*.js /usr/local/share/bwkit/dashboard/   # the node's own page, served at / and /dash/
rm -f /usr/local/share/bwkit/dashboard.html   # the single-file page of 0.4.0
install -m 0644 "$here"/systemd/*.service "$here"/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload

if [ ! -f /etc/bwkit/config.json ]; then
  host="$(hostname)"
  case "$role" in
    payer) printf '{\n "role": "payer",\n "name": "%s",\n "iface": "wg0",\n "hop_name": "bw-forest",\n "hop_url": "http://10.77.0.2:8710",\n "mint": "http://bw-mint.local:8710"\n}\n' "$host" ;;
    hop)   printf '{\n "role": "hop",\n "name": "%s",\n "iface": "wg0",\n "mint": "http://bw-mint.local:8710",\n "link": {"price": 0.10, "margin": 0.01, "min_bytes": 65536, "patience_windows": 0, "medium": "wire", "range_class": "room"}\n}\n' "$host" ;;
    mint)  printf '{\n "role": "mint",\n "name": "%s",\n "price_usd_per_gb": 0.10,\n "steward_fee": 0.05\n}\n' "$host" ;;
  esac > /etc/bwkit/config.json
  echo "wrote /etc/bwkit/config.json for role $role"
fi
chmod 0644 /etc/bwkit/config.json

if [ ! -f /var/lib/bwkit/key.priv ]; then
  su -s /bin/sh bwkit -c 'bwkit keygen' >/dev/null
fi
echo "$(hostname) ($role) NYM: $(su -s /bin/sh bwkit -c 'bwkit nym')"
