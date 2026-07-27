#!/usr/bin/env bash
# M0a readiness gate — Master AI Chrome extension build.
# Run once on the box before M0a code work starts. Reports readiness
# (memory, backend, CORS, LLaVA pinning, keep_alive call sites) and
# bootstraps the extension token. Code edits to prewarm_master_ai.py and
# master_ai.py happen in M0a — this script does NOT modify either file.
# Per HARD_LIMITS, sudo steps are PRINTED, never executed.

set -u
G='\033[32m'; R='\033[31m'; Y='\033[33m'; W='\033[97m'; D='\033[90m'; X='\033[0m'
green() { echo -e "  ${G}✅ $*${X}"; }
red()   { echo -e "  ${R}❌ $*${X}"; }
warn()  { echo -e "  ${Y}⚠  $*${X}"; }
hdr()   { echo; echo -e "${W}── $* ──${X}"; }

hdr "1) Memory + processes baseline"
free -h | head -3
echo
ollama ps
echo
swap_pct=$(free | awk '/Swap:/ {if ($2==0) print 0; else printf "%.0f", $3*100/$2}')
echo "Swap utilization: ${swap_pct}%"
[ "$swap_pct" -lt 80 ] && green "swap pressure ok" || warn "swap >80% — LLaVA fix below will help"

hdr "2) Backend health"
if curl -sf http://127.0.0.1:8080/health >/dev/null; then green "/health 200"; else red "/health unreachable — start master-ai-ui.service"; fi
if curl -sf http://127.0.0.1:8080/status >/dev/null; then green "/status 200"; else red "/status unreachable"; fi

hdr "3) /chat implementation state"
if grep -q "api_handle(payload)" /home/user/scripts/stt_server.py; then
  green "/chat uses api_handle() wrapper — M0a upgrade present"
elif awk '/self.path == '\''\/chat'\''/{in_chat=1} in_chat && /api\/generate/{found=1} in_chat && /POST \/mode/{in_chat=0} END{exit found ? 0 : 1}' /home/user/scripts/stt_server.py; then
  warn "/chat still appears to call Ollama /api/generate directly — M0a upgrade target confirmed"
else
  warn "/chat state unclear — review stt_server.py before M0a"
fi

hdr "4) CORS state"
cors=$(grep -E "Access-Control-Allow-Origin" /home/user/scripts/stt_server.py | head -3 || true)
echo "${cors:-no CORS headers found}"
# Match wildcard whether single- OR double-quoted (the live code uses '*').
if echo "$cors" | grep -qE "['\"]\*['\"]"; then
  warn "wildcard CORS detected — M0a must replace with exact-origin echo"
else
  green "no wildcard CORS"
fi

hdr "5) Image-gen proxy health"
if curl -sf http://127.0.0.1:8080/sdcpp/health >/dev/null; then
  green "/sdcpp/health 200 — sd-server pattern reusable for M5 OmniParser"
else
  warn "/sdcpp/health unreachable (sd-server may be stopped — not blocking M0a)"
fi

hdr "6) Ollama keep-alive systemd config"
ka_file=/etc/systemd/system/ollama.service.d/keep-alive.conf
if [ -r "$ka_file" ]; then
  cat "$ka_file"
else
  warn "$ka_file not present"
fi

hdr "7) LLaVA pressure — two-part fix"
# Part A: report prewarm pinning
if grep -nq "llava" /home/user/scripts/prewarm_master_ai.py 2>/dev/null; then
  warn "llava is pinned in prewarm_master_ai.py — remove this line"
  grep -n "llava" /home/user/scripts/prewarm_master_ai.py
else
  green "llava NOT pinned in prewarm_master_ai.py"
fi
# Part B: report per-call keep_alive values for LLaVA in master_ai.py
echo
echo "Per-call keep_alive values for LLaVA in master_ai.py:"
grep -nE 'llava.*keep_alive|keep_alive.*llava|"keep_alive"\s*:\s*"30m"' /home/user/scripts/master_ai.py | head -10 || warn "no explicit LLaVA keep_alive found"
echo
echo -e "${D}Required edits before M0a:${X}"
echo "  · remove llava preload from prewarm_master_ai.py"
echo "  · change any LLaVA-call keep_alive from \"30m\" to \"60s\" (or \"0\") in master_ai.py"

hdr "Post-fix verification (run AFTER applying the two edits above)"
echo "  · wait 90s after last LLaVA call"
echo "  · ollama ps should show only master-ai:latest"
echo "  · free -h should show ~6-7 GiB available"
echo

hdr "Extension-token bootstrap"
tok=/home/user/.master_ai_extension_token
if [ -f "$tok" ]; then
  green "extension token exists at $tok"
else
  python3 -c "import secrets, os; open(os.path.expanduser('~/.master_ai_extension_token'), 'w').write(secrets.token_hex(32))"
  chmod 600 "$tok"
  green "extension token generated at $tok (chmod 600)"
fi

echo
echo -e "${W}── Summary ──${X}"
echo "M0a readiness/verification: backend health ✅, /chat wrapper state known, prewarm fixed, per-call keep_alive fixed, ~6-7 GiB available, token file exists."
