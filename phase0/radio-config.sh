#!/bin/sh
# The T-Beams set up for the lab's LoRa channel, from the Mac over SSH (radio/PLAN.md, step 1). Run after
# `provision.sh radio` has put the Meshtastic tool on the nodes and named each board /dev/bwradio.
#
#   RADIO_NODES="bw-house bw-neighbor bw-cabin" SSH_SUFFIX=-ts phase0/radio-config.sh
#   (bw-forest, which is not on the tailnet, with FOREST_JUMP=bw-house-ts as in provision.sh)
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
# bw-forest is not on the tailnet: FOREST_JUMP=bw-house-ts reaches it through bw-house, as in provision.sh
ssh_to() { _h="$1"; shift; if [ "$_h" = bw-forest ] && [ -n "${FOREST_JUMP:-}" ]; then $SSH -J "$FOREST_JUMP" bw-forest "$@"; else $SSH "$_h$SSH_SUFFIX" "$@"; fi; }
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
  base="$(printf '%s' "$h" | sed 's/^bw-//')"
  case "$base" in *[0-9]) short="$(printf '%s' "$base" | cut -c1-3)$(printf '%s' "$base" | sed 's/.*\(.\)$/\1/')";; *) short="$(printf '%s' "$base" | cut -c1-4)";; esac
  short="$(printf '%s' "$short" | tr '[:lower:]' '[:upper:]')"   # four letters, unique: bw-mint MINT, bw-mint2 MIN2
  gps=DISABLED; case " $gps_nodes " in *" $h "*) gps=ENABLED;; esac
  say "$h: region US, owner $h ($short), role CLIENT, $preset, gps $gps, broadcasts quiet"
  ssh_to "$h" "$m --set lora.region US --set-owner '$h' --set-owner-short '$short' --set device.role CLIENT --set lora.hop_limit 3 --set lora.modem_preset $preset --set position.gps_mode $gps --set position.position_broadcast_smart_enabled false --set position.position_broadcast_secs 86400 --set telemetry.device_update_interval 86400 --set telemetry.environment_update_interval 86400 2>&1 | grep -vE '^Connected|^Writing|^Set |^INFO|^$' | tail -3"
  say "$h: the channel bwlab at index 0, its key from the Mac (the board reboots after each change; verified by key hash)"
  want="$(printf '%s' "$LORA_PSK" | shasum -a 256 | cut -c1-16)"
  have=""
  keyhash() { ssh_to "$h" "timeout 60 $m --info 2>/dev/null | grep -oE '\"psk\": \"[^\"]*\", \"name\": \"bwlab\"' | head -1 | sed -E 's/\"psk\": \"([^\"]*)\".*/\\1/' | tr -d '\\n' | sha256sum | cut -c1-16" 2>/dev/null || true; }
  # A board reboots after each change and answers again 20 to 90 s later; read it patiently before writing again,
  # or the write and the reboot chase each other (bw-mint, Oct 24, 2026).
  for try in 1 2 3; do
    for poll in 1 2 3 4 5 6; do
      sleep 15
      have="$(keyhash)"
      [ "$have" = "$want" ] && break
    done
    if [ "$have" = "$want" ]; then echo "$h: channel bwlab holds the Mac's key (write $((try - 1)), read $poll)"; break; fi
    printf '%s\n' "$LORA_PSK" | ssh_to "$h" "read k; timeout 90 $m --ch-index 0 --ch-set name bwlab --ch-set psk base64:\$k >/dev/null 2>&1" || true
    echo "$h: channel written (write $try); reading it back after the board's reboot"
  done
  [ "$have" = "$want" ] || { sleep 15; have="$(keyhash)"; }
  [ "$have" = "$want" ] || echo "$h: COULD NOT VERIFY the channel key after three writes"
  # --info prints the channel's key and a URL that encodes it; only the owner, the number, the region and the preset are shown
  ssh_to "$h" "timeout 60 $m --info 2>/dev/null | grep -E '^Owner|myNodeNum|\"region\"|modemPreset' | sed 's/^/$h: /' | head -5" || true
done

first="$(echo "$nodes" | awk '{print $1}')"
say "what $first's board has heard (fills in as the others announce themselves)"
ssh_to "$first" "$m --nodes 2>/dev/null | grep -E '^│ +[0-9]' | cut -c1-120" || true
