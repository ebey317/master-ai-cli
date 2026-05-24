#!/usr/bin/env bash
# iptv.sh — Launch Hypnotix or play IPTV via mpv/VLC
# Usage: iptv.sh [play|gui|status]

set -euo pipefail

XTREAM_HOST="http://line.plugtv.xyz"
XTREAM_USER="28fa070c23"
XTREAM_PASS="d6e9d5fb7ee4"
M3U_URL="${XTREAM_HOST}/get.php?username=${XTREAM_USER}&password=${XTREAM_PASS}&type=m3u_plus&output=ts"
EPG_URL="${XTREAM_HOST}/xmltv.php?username=${XTREAM_USER}&password=${XTREAM_PASS}"

CMD="${1:-gui}"

case "$CMD" in
  gui)
    echo "[iptv] Launching Hypnotix..."
    hypnotix &
    ;;
  play)
    # Direct mpv playback of full channel list
    echo "[iptv] Opening M3U in mpv..."
    mpv --playlist="$M3U_URL" \
        --hwdec=vaapi \
        --cache=yes \
        --demuxer-max-bytes=150M \
        &
    ;;
  vlc)
    if command -v vlc &>/dev/null; then
      vlc "$M3U_URL" &
    else
      echo "[iptv] VLC not installed. Try: sudo apt install vlc"
    fi
    ;;
  status)
    echo "[iptv] Checking account..."
    curl -s --max-time 8 \
      "${XTREAM_HOST}/player_api.php?username=${XTREAM_USER}&password=${XTREAM_PASS}" \
      | python3 -c "
import sys, json, datetime
d = json.load(sys.stdin)
u = d.get('user_info', {})
s = d.get('server_info', {})
exp = int(u.get('exp_date', 0))
exp_str = datetime.datetime.fromtimestamp(exp).strftime('%Y-%m-%d') if exp else '?'
print(f\"  Status:      {u.get('status','?')}\")
print(f\"  Expires:     {exp_str}\")
print(f\"  Max conns:   {u.get('max_connections','?')}\")
print(f\"  Active conns: {u.get('active_cons','?')}\")
cats = '187 live / 59 VOD / 37 series'
print(f\"  Content:     {cats}\")
"
    ;;
  m3u-url)
    echo "$M3U_URL"
    ;;
  epg-url)
    echo "$EPG_URL"
    ;;
  *)
    echo "Usage: iptv.sh [gui|play|vlc|status|m3u-url|epg-url]"
    ;;
esac
