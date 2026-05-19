#!/bin/bash
# clone_anthropic_to_sensei.sh
#
# Take Anthropic's "Claude in Chrome" extension as a foundation, strip web-store
# verification, rebrand to Sensei, repoint network surface at the local bridge.
#
# Operator's directive (2026-05-19): "anthropic has the foundation already
# we just want to take that code and replace it with fonts and direction
# really the same direction we just want to replace it with font information
# same route different way."

set -euo pipefail

SRC="$HOME/.config/google-chrome/Default/Extensions/fcoeoabgfenejglbffodgkkbkcdhcgfn/1.0.70_0"
DEST="$HOME/scripts/sensei_in_chrome"

if [ ! -d "$SRC" ]; then
  echo "ERROR: Anthropic extension not found at $SRC" >&2
  exit 1
fi

echo "[1/7] Wiping any prior clone at $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"

echo "[2/7] Mirroring $SRC -> $DEST"
cp -a "$SRC/." "$DEST/"

echo "[3/7] Stripping web-store verification (_metadata/, computed_hashes, verified_contents)"
rm -rf "$DEST/_metadata"

echo "[4/7] Rewriting manifest.json — remove key, rebrand strings, relax CSP, repoint externally_connectable"
python3 - "$DEST/manifest.json" <<'PY'
import json, sys, re

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    m = json.load(f)

# (15) Remove the RSA key pin — Chrome will assign a fresh ID from install path
m.pop("key", None)

# (3) Display name
m["name"] = "Sensei"
# (4) Description
m["description"] = "Sensei in Chrome — local-first browser agent"
# (5) Toolbar tooltip
if "action" in m:
    m["action"]["default_title"] = "Open Sensei"
# (6) Command description
cmd = m.get("commands", {}).get("toggle-side-panel")
if cmd:
    cmd["description"] = "Toggle Sensei side panel"

# (12) CSP — keep Anthropic surface for now AND allow local bridge.
# (Operator can tighten after first smoke. The bundle still tries Anthropic
# domains at startup; we don't want CSP-blocked errors masking other issues.)
csp = m.get("content_security_policy", {}).get("extension_pages", "")
if csp and "127.0.0.1:8080" not in csp:
    csp = csp.replace(
        "connect-src 'self'",
        "connect-src 'self' http://127.0.0.1:8080 ws://127.0.0.1:8080",
        1,
    )
    m["content_security_policy"]["extension_pages"] = csp

# (13) externally_connectable — leave alone for now; without the original key
# the pairing flow can't auth back to claude.ai anyway, so this is informational.

with open(path, "w", encoding="utf-8") as f:
    json.dump(m, f, indent=2)
    f.write("\n")
print("manifest rewritten")
PY

echo "[5/7] Rebranding i18n strings (Claude -> Sensei) across all 12 locales"
for f in "$DEST"/i18n/*.json; do
  python3 -c "
import json, sys, re
p = sys.argv[1]
with open(p, 'r', encoding='utf-8') as f:
    data = json.load(f)
def swap(obj):
    if isinstance(obj, dict):
        return {k: swap(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [swap(x) for x in obj]
    if isinstance(obj, str):
        # Replace Claude→Sensei but keep claude.ai mentions intact
        s = obj
        s = re.sub(r'\bClaude\b(?!\.ai)', 'Sensei', s)
        return s
    return obj
data = swap(data)
with open(p, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
" "$f"
done
echo "    rebranded: $(ls "$DEST"/i18n/*.json | wc -l) locales"

echo "[6/7] Patching minified bundles — repoint api.anthropic.com -> 127.0.0.1:8080"
# Sed in place against the sidepanel + content scripts. Best-effort; if the
# bundle resists, Path B (polish sensei_extension) remains the working fallback.
for f in "$DEST"/assets/*.js "$DEST"/*.js; do
  [ -f "$f" ] || continue
  sed -i \
    -e 's#https://api\.anthropic\.com#http://127.0.0.1:8080#g' \
    -e 's#wss://api\.anthropic\.com#ws://127.0.0.1:8080#g' \
    "$f"
done
echo "    patched URLs in $(ls "$DEST"/assets/*.js "$DEST"/*.js 2>/dev/null | wc -l) JS files"

echo "[7/7] Final layout:"
ls -1 "$DEST" | head -20
echo
echo "DONE. Clone at: $DEST"
echo
echo "Next steps for operator:"
echo "  1. Chrome → chrome://extensions → Developer Mode ON → Load unpacked → select $DEST"
echo "  2. Chrome assigns a new extension ID (the RSA key was stripped)"
echo "  3. Re-run install_native_host.sh with that new ID:"
echo "       bash ~/scripts/sensei_extension/install_native_host.sh <new-ext-id>"
echo "  4. Start the bridge if not already running:"
echo "       python3 ~/scripts/sensei_bridge.py &"
echo "  5. Click the Sensei toolbar icon — side panel opens"
echo
echo "Known fragile: the 2.0 MB sidepanel bundle was built by Vite for Anthropic's API."
echo "URL rewrites are best-effort. If the side panel UI errors on load, the polished"
echo "sensei_extension at ~/scripts/sensei_extension/ is the working Path B fallback."
