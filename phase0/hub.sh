#!/bin/sh
# The rendezvous host: a small public machine running WireGuard, which bw-house and bw-neighbor each connect out to,
# because neither house can open a port. It forwards between its peers and does nothing else. Debian or Ubuntu.
#
#   HUB=user@host phase0/hub.sh prep                       install WireGuard, make the hub's key, wg0 at 10.77.2.1, forwarding on
#   HUB=user@host phase0/hub.sh peer <name> <pubkey> <allowed>   add or replace a peer (its tunnel address, and any network routed through it)
#   HUB=user@host phase0/hub.sh show                       wg show
#
# The hub's private key is made on the hub and stays there. The user in HUB must be root or have passwordless sudo.
set -eu
[ -n "${HUB:-}" ] || { echo "HUB=user@host is required"; exit 2; }
SSH="ssh -o ConnectTimeout=8 -o BatchMode=yes"
PORT="${HUB_PORT:-51820}"
hub_sh() { $SSH "$HUB" sudo sh -s; }

case "${1:-}" in
  prep)
    hub_sh <<EOS
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null && apt-get install -y -q wireguard-tools iptables >/dev/null
install -d -m 0700 /etc/wireguard
if [ ! -s /etc/wireguard/wg0.key ]; then (umask 077; wg genkey > /etc/wireguard/wg0.key); fi
wg pubkey < /etc/wireguard/wg0.key > /etc/wireguard/wg0.pub
if [ ! -s /etc/wireguard/wg0.conf ]; then
umask 077
cat > /etc/wireguard/wg0.conf <<EOC
# Bandwidth Swap rendezvous host. Peers are appended by phase0/hub.sh peer. Private key in /etc/wireguard/wg0.key.
[Interface]
Address = 10.77.2.1/24
ListenPort = $PORT
PostUp = wg set %i private-key /etc/wireguard/wg0.key
PostUp = sysctl -q -w net.ipv4.ip_forward=1
PostUp = iptables -A FORWARD -i %i -o %i -j ACCEPT
PostDown = iptables -D FORWARD -i %i -o %i -j ACCEPT
EOC
fi
systemctl enable -q wg-quick@wg0
systemctl restart wg-quick@wg0
echo "hub: wg0 10.77.2.1 listening on udp/$PORT; public key \$(cat /etc/wireguard/wg0.pub)"
echo "open udp/$PORT in the host's firewall if it has one (ufw allow $PORT/udp)"
EOS
    ;;
  peer)
    name="$2"; pub="$3"; allowed="$4"
    hub_sh <<EOS
set -e
umask 077
# replace any earlier block for this name
awk -v n="# peer $name" 'BEGIN{skip=0} \$0==n{skip=1; next} skip && /^\[Peer\]/{next} skip && /^(PublicKey|AllowedIPs|PersistentKeepalive)/{next} skip && /^\$/{skip=0; next} {print}' /etc/wireguard/wg0.conf > /etc/wireguard/wg0.conf.new
printf '\n# peer %s\n[Peer]\nPublicKey = %s\nAllowedIPs = %s\n' "$name" "$pub" "$allowed" >> /etc/wireguard/wg0.conf.new
mv /etc/wireguard/wg0.conf.new /etc/wireguard/wg0.conf
systemctl restart wg-quick@wg0
echo "hub: peer $name -> $allowed"
EOS
    ;;
  show) $SSH "$HUB" 'sudo wg show wg0' ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
