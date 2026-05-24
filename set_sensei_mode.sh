#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-}"

case "$MODE" in
  local|hybrid|cloud)
    ;;
  *)
    echo "ERROR: choose one mode: local | hybrid | cloud"
    echo "Usage: /madam local"
    echo "       /madam hybrid"
    echo "       /madam cloud"
    exit 2
    ;;
esac

CLAF_ENV="$HOME/.config/systemd/user/claf.env"
PROJECT_ENV="$HOME/projects/claf/.env"

mkdir -p "$(dirname "$CLAF_ENV")"

# Backup runtime env file before changing it
if [[ -f "$CLAF_ENV" ]]; then
  cp "$CLAF_ENV" "$CLAF_ENV.bak.$(date +%Y%m%d_%H%M%S)"
else
  touch "$CLAF_ENV"
fi

# Set the real runtime source used by systemd
if grep -q '^CLAF_MODE=' "$CLAF_ENV"; then
  sed -i "s/^CLAF_MODE=.*/CLAF_MODE=$MODE/" "$CLAF_ENV"
else
  echo "CLAF_MODE=$MODE" >> "$CLAF_ENV"
fi

# Mirror into project .env so the project file does not lie
if [[ -f "$PROJECT_ENV" ]]; then
  if grep -q '^CLAF_MODE=' "$PROJECT_ENV"; then
    sed -i "s/^CLAF_MODE=.*/CLAF_MODE=$MODE/" "$PROJECT_ENV"
  else
    echo "CLAF_MODE=$MODE" >> "$PROJECT_ENV"
  fi
fi

systemctl --user daemon-reload
systemctl --user restart claf.service
sleep 3

echo "=== MADAM / SENSEI MODE SWITCH ==="
echo "requested_mode=$MODE"
echo

curl -s http://127.0.0.1:8000/ | python3 -c '
import sys,json
d=json.load(sys.stdin)
print("active_mode:", d.get("mode"))
print("local:", [p.get("name") for p in d.get("providers",[]) if p.get("pool")=="local" and p.get("enabled")])
print("cloud:", [p.get("name") for p in d.get("providers",[]) if p.get("pool")=="cloud" and p.get("enabled")])
'
