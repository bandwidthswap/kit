#!/bin/sh
# The T-Beams set up for the lab's LoRa channel, from the Mac over SSH (radio/PLAN.md, step 1). Run after
# `provision.sh radio` has put the Meshtastic tool on the nodes and named each board /dev/bwradio.
#
#   RADIO_NODES="bw-house bw-neighbor bw-cabin" SSH_SUFFIX=-ts phase0/radio-config.sh
#
# Per board: region US, the node's name as the owner (a four-letter short name from it), role CLIENT, hop limit 3,
# the modem preset LORA_PRESET (default LONG_FAST; MEDIUM_FAST once the cabin link is measured), GPS on only on
# GPS_NODES (default bw-cabin: the cabin's clock), position and telemetry broadcasts quiet, and one private channel
# "bwlab" at index 0 whose PSK lives in ~/.config/bwkit/radio.env on the Mac (LORA_PSK, mode 600; made there if
# absent, sent to each board on stdin, never printed, never in a repo). Each board reboots after its settings; the
# last step lists what bw-house's board hears, which fills in over the next minutes as the others beacon.
set -eu
nodes="${RADIO_NODES:-bw-house bw-neighbor bw-cabin}"
gps_nodes="${GPS_NODES:-bw-cabin}"
preset="${LORA_PRESET:-LONG_FAST}"
SSH="ssh -o ConnectTimeout=8 -o BatchMode=yes"
SSH_SUFFIX="${SSH_SUFFIX:-}"
env_file="$HOME/.config/bwkit/radio.env"
say() { printf '\n== %s\n' "$*"; }
m='sudo -u bwkit /opt/bwradio/bin/meshtastic --port /dev/bwradio'

if [ -f "$env_file" ]; then
  LORA_PSK=""; . "$env_file"
fi
if [ -z "${LORA_PSK:-}" ]; then
  mkdir -p "$(dirname "$env_file")"; chmod 700 "$(dirname "$env_file")"
  LORA_PSK="$(openssl rand -base64 32)"
  umask 077; printf '# the lab LoRa channel "bwlab": its key, made on %s by phase0/radio-config.sh; never in a repo\nLORA_PSK=%s\n' "$(date +%Y-%m-%d)" "$LORA_PSK" > "$env_file"
  echo "made the channel key in $env_file (mode 600)"
else
  echo "the channel key from $env_file"
fi

for h in $nodes; do
  short="$(printf '%s' "$h" | sed 's/^bw-//' | cut -c1-4 | tr '[:lower:]' '[:upper:]')"
  gps=DISABLED; case " $gps_nodes " in *" $h "*) gps=ENABLED;; esac
  say "$h: region US, owner $h ($short), role CLIENT, $preset, gps $gps, broadcasts quiet"
  $SSH "$h$SSH_SUFFIX" "$m --set lora.region US --set-owner '$h' --set-owner-short '$short' --set device.role CLIENT --set lora.hop_limit 3 --set lora.modem_preset $preset --set position.gps_mode $gps --set position.position_broadcast_smart_enabled false --set position.position_broadcast_secs 86400 --set telemetry.device_update_interval 86400 --set telemetry.environment_update_interval 86400 2>&1 | grep -vE '^Connected|^Writing|^Set |^INFO|^$' | tail -3"
  say "$h: the channel bwlab at index 0, its key from the Mac (the board reboots after its settings; up to three tries)"
  for try in 1 2 3; do
    sleep 25
    if printf '%s\n' "$LORA_PSK" | $SSH "$h$SSH_SUFFIX" "read k; $m --ch-index 0 --ch-set name bwlab --ch-set psk base64:\$k 2>&1 | grep -qiE 'reconnect|not fully booted|Timed out|No such file' && exit 1; exit 0"; then echo "$h: channel set (try $try)"; break; fi
    echo "$h: the board was not ready (try $try)"
  done
  sleep 20
  # --info prints the channel's key in full; only the owner, the number, the region, the preset and whether bwlab is there are shown
  $SSH "$h$SSH_SUFFIX" "$m --info 2>/dev/null | grep -E '^Owner|myNodeNum|\"region\"|modemPreset' | sed 's/^/$h: /' | head -5; $m --info 2>/dev/null | grep -q '\"name\": \"bwlab\"' && echo '$h: channel bwlab at index 0' || echo '$h: NO bwlab channel'" || true
done

say "what bw-house's board hears (fills in as the others beacon)"
first="$(echo "$nodes" | awk '{print $1}')"
$SSH "$first$SSH_SUFFIX" "$m --nodes 2>/dev/null | grep -E '^│ +[0-9]' | cut -c1-120" || true
