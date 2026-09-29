#!/bin/bash
source ~/scripts/brand.sh

LOG_FILE="$HOME/scripts/master.log"
SESSION_DIR="$HOME/scripts/sessions"
OLLAMA_URL="http://localhost:11434"
mkdir -p "$SESSION_DIR" "$HOME/scripts"
touch "$LOG_FILE"

log() {
    TIMESTAMP=$(date "+%Y-%m-%d %I:%M:%S %p")
    echo "[$TIMESTAMP] $1" | tee -a "$LOG_FILE"
}

# open_once <url> <title-keyword>
# Launches URL in Firefox only if no existing window title contains <keyword>.
# Avoids duplicate tabs — reuses/focuses the existing window instead.
open_once() {
    local url="$1" key="$2"
    if command -v wmctrl >/dev/null 2>&1 && wmctrl -l 2>/dev/null | grep -qi "$key"; then
        wmctrl -a "$key" 2>/dev/null || true
        echo "  ↺ already open: $key (focused existing window)"
    else
        firefox "$url" >/dev/null 2>&1 &
        echo "  ↗ opened: $url"
    fi
}

check_ollama() {
    log "--- Ollama Health Check ---"
    if curl -s --max-time 5 "$OLLAMA_URL" > /dev/null 2>&1; then
        log "OK — Ollama responding"
        echo "✅ Ollama is running."
    else
        log "ALERT — Ollama NOT responding"
        echo "⚠️  Ollama is DOWN."
    fi
}

check_rustdesk() {
    log "--- RustDesk Health Check ---"
    if systemctl is-active --quiet rustdesk; then
        RDID=$(rustdesk --get-id 2>/dev/null || echo "unknown")
        echo "✅ RustDesk running — ID: $RDID"
        log "OK — RustDesk active, ID: $RDID"
    else
        echo "⚠️  RustDesk is NOT running."
        log "ALERT — RustDesk NOT running"
    fi
}

launch_master_ai_terminal() {
    log "--- Launching Sensei directly ---"
    if ! pgrep -f "stt_server.py" > /dev/null; then
        bash "$HOME/scripts/serve_ui.sh" > /tmp/ui_server.log 2>&1 &
        sleep 1
    fi
    # Menu 4 is Sensei-only by design. Pupil has its own menu entry (5)
    # and menu 1 (Full startup) opens both doors. Don't bundle Pupil
    # here — respects the menu contract and doesn't steal focus from
    # users who deliberately pick one door.
    cd "$HOME/scripts"
    # Sensei opens immediately. Dojo remains available from Projects when
    # the user wants to pin a project/task, but it no longer blocks entry.
    bash "$HOME/scripts/launch_master_ai.sh"
}

open_firefox_tabs() {
    log "--- Opening Firefox tabs ---"
    open_once "http://localhost:8080/pupil.html" "Pupil"
    open_once "http://localhost:4040" "ngrok"
    echo "✅ Firefox tabs ready."
}

save_session() {
    local FILENAME="${1:-session}"
    local SAVE_PATH="$SESSION_DIR/${FILENAME}_$(date +%Y%m%d_%H%M%S).log"
    cp "$LOG_FILE" "$SAVE_PATH"
    log "SESSION SAVED: $SAVE_PATH"
    echo "✅ Session saved: $SAVE_PATH"
}

load_session() {
    local FILENAME="$1"
    local MATCH=$(ls "$SESSION_DIR" 2>/dev/null | grep "^$FILENAME" | tail -1)
    if [ -n "$MATCH" ]; then
        log "SESSION LOADED: $MATCH"
        echo "--- SESSION: $MATCH ---"
        cat "$SESSION_DIR/$MATCH"
        echo "--- END SESSION ---"
    else
        echo "⚠️  No session found for: $FILENAME"
    fi
}

PROJECTS_FILE="$HOME/scripts/PROJECTS.md"

prompt_idea() {
    local TITLE DESC STATUS TODAY
    TODAY=$(date "+%Y-%m-%d")

    echo -ne "${BC}Title:${X} "
    read -r TITLE
    [ -z "$TITLE" ] && { echo "  (cancelled — empty title)"; return 1; }

    echo -ne "${BC}Description:${X} "
    read -r DESC
    [ -z "$DESC" ] && { echo "  (cancelled — empty description)"; return 1; }

    echo -ne "${BC}Status${X} [idea/built/live] (default: idea): "
    read -r STATUS
    STATUS="${STATUS:-idea}"
    case "$STATUS" in idea|built|live) ;; *) STATUS="idea" ;; esac

    # Show alert/preview first — user decides whether to persist
    echo
    echo -e "${Y}╔══════════════════════════════════════════╗${X}"
    echo -e "${Y}║  🚨 IDEA ALERT — review before saving    ║${X}"
    echo -e "${Y}║${X}  [$TODAY] ${BC}$TITLE${X} (_${STATUS}_)"
    echo -e "${Y}║${X}  $DESC"
    echo -e "${Y}╚══════════════════════════════════════════╝${X}"
    echo
    echo -ne "${BC}Add to PROJECTS.md?${X} [y/N]: "
    read -r CONFIRM
    case "$CONFIRM" in
        y|Y|yes|YES) ;;
        *)
            log "IDEA DISCARDED: $TITLE"
            echo -e "${D}  ✗ not added. Idea discarded.${X}"
            return 0
            ;;
    esac

    # Ensure PROJECTS.md exists with the Ideas/POCs section
    if [ ! -f "$PROJECTS_FILE" ]; then
        log "WARN — PROJECTS.md missing, creating stub"
        {
            echo "# Elijah's Projects"
            echo
            echo "## Ideas / POCs"
            echo "<!-- auto-appended by master.sh option 9 -->"
        } > "$PROJECTS_FILE"
    fi
    grep -q "^## Ideas / POCs" "$PROJECTS_FILE" || {
        printf '\n## Ideas / POCs\n<!-- auto-appended by master.sh option 9 -->\n' >> "$PROJECTS_FILE"
    }

    printf -- '- [%s] **%s** (_%s_) — %s\n' "$TODAY" "$TITLE" "$STATUS" "$DESC" >> "$PROJECTS_FILE"

    log "NEW IDEA: [$STATUS] $TITLE — $DESC"
    echo -e "${BG}  ✅ added to ~/scripts/PROJECTS.md${X}"
}

startup() {
    clear
    banner_master_ai
    log "=== FULL STARTUP BEGIN ==="

    echo -e "${C}  1/3 Starting TTS server...${X}"
    if ! pgrep -f "tts_server.py" > /dev/null; then
        python3 "$HOME/scripts/tts_server.py" > /tmp/tts_server.log 2>&1 &
        sleep 2
        echo -e "${G}  ✅ TTS server started (port 5050).${X}"
    else
        echo -e "${G}  ✅ TTS server already running.${X}"
    fi

    echo -e "${C}  2/2 Starting Remote UI (Vite dev server on :5173)...${X}"
    if ! pgrep -f "npm run dev" > /dev/null; then
        (cd "$HOME/Downloads/sunkissed-soul" && nohup npm run dev > /tmp/remote_ui.log 2>&1 &)
        sleep 2
        echo -e "${G}  ✅ Remote UI started.${X}"
    else
        echo -e "${G}  ✅ Remote UI already running.${X}"
    fi

    echo -e "${G}  ✅ All services launched!${X}"
    echo -e "${D}  (Skipping :8080 stt_server — Pupil replaces it. Run serve_ui.sh manually if you want Web Chat.)${X}"
    log "=== FULL STARTUP COMPLETE ==="
}

launch_master_ai() {
    clear
    banner_master_ai
    log "=== MASTER AI LAUNCH BEGIN ==="

    echo -e "${C}  1/2 Starting TTS server...${X}"
    if ! pgrep -f "tts_server.py" > /dev/null; then
        python3 "$HOME/scripts/tts_server.py" > /tmp/tts_server.log 2>&1 &
        sleep 2
        echo -e "${G}  ✅ TTS server started.${X}"
    else
        echo -e "${G}  ✅ TTS server already running.${X}"
    fi

    echo -e "${C}  2/2 Starting UI server...${X}"
    bash "$HOME/scripts/serve_ui.sh"
    sleep 1
    open_once "http://localhost:8080/pupil.html" "Pupil"
    log "=== MASTER AI LAUNCH COMPLETE ==="
}

view_projects() {
    # Option 16 — browse the project boards from the terminal.
    # Read-only by default: see goals, tasks, model, last pickup point.
    # From here the user can:
    #   - open the detail view of one project
    #   - pin it as Sensei context and launch Sensei directly
    local pfile="$HOME/scripts/PROJECTS.md"
    [ ! -f "$pfile" ] && { echo -e "${R}  ❌ PROJECTS.md not found at $pfile${X}"; return 1; }

    while true; do
        clear
        banner_master_ai
        echo ""
        echo -e "  ${BC}╔════════════════════════════════════════════╗${X}"
        echo -e "  ${BC}║${X}  ${BW}📁  PROJECTS${X}                               ${BC}║${X}"
        echo -e "  ${BC}║${X}  ${D}terminal view — pick one to see details${X}     ${BC}║${X}"
        echo -e "  ${BC}╚════════════════════════════════════════════╝${X}"
        echo ""
        echo -e "  ${BY}⏳ Heads up:${X} ${D}when you open a project, the AI reads your past chats, memory,${X}"
        echo -e "     ${D}and project notes before it answers. First time feels slow — like${X}"
        echo -e "     ${D}a cold car engine. Once it's warmed up, it's quick.${X}"
        echo ""

        local -a names
        mapfile -t names < <(awk '
            /^## Project Boards/ { in_b = 1; next }
            /^## / && in_b  { in_b = 0 }
            /^### / && in_b { sub(/^### /, ""); print }
        ' "$pfile")

        if [ ${#names[@]} -eq 0 ]; then
            echo -e "  ${Y}no project boards found in PROJECTS.md${X}"
            echo ""
            read -rp "  press Enter to return... " _
            return 0
        fi

        echo -e "  ${BW}Pick a project to view:${X}"
        local i=1
        for name in "${names[@]}"; do
            local ptype open total model last
            ptype=$(awk -v p="$name" 'BEGIN{found=0} $0=="### " p{in_p=1;next} /^### /{in_p=0} in_p && /^- \*\*Type:\*\*/{sub(/^- \*\*Type:\*\* */, "");sub(/[[:space:]]+←.*$/,"");sub(/[[:space:]]+$/,"");print;found=1;exit} END{if(!found)print ""}' "$pfile")
            model=$(awk -v p="$name" 'BEGIN{found=0} $0=="### " p{in_p=1;next} /^### /{in_p=0} in_p && /^- \*\*Model:\*\*/{sub(/^- \*\*Model:\*\* */, "");sub(/[[:space:]]+←.*$/,"");sub(/[[:space:]]+$/,"");print;found=1;exit} END{if(!found)print ""}' "$pfile")
            open=$(awk -v p="$name" '$0=="### " p{in_p=1;next} /^### /{in_p=0} in_p && /^[[:space:]]*- \[ \]/{n++} END{print n+0}' "$pfile")
            total=$(awk -v p="$name" '$0=="### " p{in_p=1;next} /^### /{in_p=0} in_p && /^[[:space:]]*- \[[x ]\]/{n++} END{print n+0}' "$pfile")

            local type_tag=""
            [ "$ptype" = "training" ] && type_tag="  ${BY}[training]${X}"
            local model_tag=""
            [ -n "$model" ] && [ "$model" != "auto" ] && model_tag="  ${D}model:${model}${X}"

            printf "  ${BC}%2d)${X} ${BW}%-34s${X}  ${G}%d${X}/${D}%d${X} open%s%s\n" \
                "$i" "$name" "$open" "$total" "$type_tag" "$model_tag"
            i=$((i + 1))
        done
        echo ""
        echo -e "  ${BC}g)${X} open Dojo project picker"
        echo -e "  ${BC}x)${X} back to menu"
        echo ""
        read -rp "  pick: " choice

        case "$choice" in
            x|X|'') return 0 ;;
            g|G) bash "$HOME/scripts/dojo_gate.sh"; return 0 ;;
            ''|*[!0-9]*) echo -e "  ${R}enter a number, g, or x${X}"; sleep 1 ;;
            *)
                if [ "$choice" -ge 1 ] && [ "$choice" -le "${#names[@]}" ]; then
                    _view_project_detail "${names[$((choice - 1))]}"
                else
                    echo -e "  ${R}out of range${X}"; sleep 1
                fi
                ;;
        esac
    done
}

_view_project_detail() {
    local name="$1"
    local pfile="$HOME/scripts/PROJECTS.md"
    while true; do
        clear
        banner_master_ai
        echo ""
        echo -e "  ${BC}╔════════════════════════════════════════════╗${X}"
        echo -e "  ${BC}║${X}  ${BW}📁  $name${X}"
        echo -e "  ${BC}╚════════════════════════════════════════════╝${X}"
        echo ""

        # Dump the project block to screen (H3 through next H3/H2 boundary)
        awk -v p="$name" '
            $0 == "### " p { in_p = 1; next }
            in_p && (/^### / || /^## /) { exit }
            in_p { print "  " $0 }
        ' "$pfile"

        echo ""
        echo -e "  ${BC}s)${X} pin this project and open Sensei"
        echo -e "  ${BC}e)${X} edit PROJECTS.md in \$EDITOR"
        echo -e "  ${BC}x)${X} back to project list"
        echo ""
        read -rp "  pick: " c
        case "$c" in
            x|X|'') return ;;
            s|S)
                # Pin this project as Sensei context, then launch directly.
                echo "$name" > "$HOME/.master_ai_active_project"
                : > "$HOME/.master_ai_active_task"
                echo -e "  ${G}✅ $name pinned — opening Sensei${X}"
                sleep 1
                bash "$HOME/scripts/launch_master_ai.sh"
                return
                ;;
            e|E)
                ${EDITOR:-nano} "$pfile"
                ;;
        esac
    done
}

add_user() {
    # Option 15 — Add a Master AI user profile.
    # TESTING SCAFFOLD: creates a profile directory under ~/.master_ai_profiles/<name>/
    # with per-user memory / sessions / chats slots. Does NOT add a Linux user.
    # Future work (pinned to Master AI tasks): wire master_ai.py + pupil.html to
    # actually read from the active profile instead of the global dotfiles.
    log "=== ADD USER ==="
    local profiles_dir="$HOME/.master_ai_profiles"
    mkdir -p "$profiles_dir"

    echo ""
    echo -e "${BC}  ── ADD USER (TESTING — not wired to multi-tenant yet) ──${X}"
    echo ""
    echo -e "${D}  A profile gets its own memory / sessions / chat history.${X}"
    echo -e "${D}  Max 4 per node (per the Multi-User Node plan). All share Ollama.${X}"
    echo ""

    # List existing profiles
    local existing
    existing=$(ls -1 "$profiles_dir" 2>/dev/null | sort)
    if [ -n "$existing" ]; then
        echo -e "${W}  Existing profiles:${X}"
        echo "$existing" | sed "s|^|    ${C}·${X} |"
        echo ""
    fi

    local count
    count=$(ls -1 "$profiles_dir" 2>/dev/null | wc -l)
    if [ "$count" -ge 4 ]; then
        echo -e "${R}  ❌ Already 4 profiles — node limit reached.${X}"
        return 1
    fi

    echo -ne "  ${C}New profile name (letters/digits/_ only, or 'x' to cancel): ${X}"
    read -r pname
    [[ -z "$pname" || "$pname" =~ ^[xX]$ ]] && { echo "  cancelled."; return 0; }

    if [[ ! "$pname" =~ ^[A-Za-z0-9_]+$ ]]; then
        echo -e "  ${R}❌ invalid name — use only letters, digits, or underscore${X}"
        return 1
    fi

    local p_root="$profiles_dir/$pname"
    if [ -d "$p_root" ]; then
        echo -e "  ${Y}⚠ profile '$pname' already exists at $p_root${X}"
    else
        mkdir -p "$p_root/sessions" "$p_root/chats"
        : > "$p_root/memory"
        : > "$p_root/tasks"
        cat > "$p_root/profile.json" <<EOF
{
  "name": "$pname",
  "created": "$(date -Iseconds)",
  "shared": {
    "ollama": true,
    "tts": true,
    "keys": true
  }
}
EOF
        chmod 700 "$p_root"
        echo -e "  ${G}✅ profile created:${X} ${W}$p_root${X}"
    fi

    echo ""
    echo -e "  ${BW}Set this as the active profile now? [y/N]${X}"
    read -r act
    if [[ "$act" =~ ^[yY] ]]; then
        echo "$pname" > "$HOME/.master_ai_active_profile"
        echo -e "  ${G}✅ active profile:${X} ${W}$pname${X}"
        echo -e "  ${D}   (Sensei + Pupil will read this on next launch — wiring still in testing)${X}"
    else
        echo -e "  ${D}   (not activated — you stay on the default profile)${X}"
    fi
}

switch_user() {
    # Option 17 — Switch active Master AI profile.
    # Writes ~/.master_ai_active_profile; master_ai.py + stt_server.py + pupil.html
    # all read it and rebase their state accordingly. No Linux user switch.
    log "=== SWITCH USER ==="
    local profiles_dir="$HOME/.master_ai_profiles"
    local active_file="$HOME/.master_ai_active_profile"

    echo ""
    echo -e "${BC}  ── SWITCH USER ──${X}"
    echo ""

    local current=""
    [ -f "$active_file" ] && current=$(cat "$active_file" 2>/dev/null | tr -d '[:space:]')
    if [ -n "$current" ]; then
        echo -e "  ${D}current:${X} ${BW}$current${X}"
    else
        echo -e "  ${D}current:${X} ${BW}(default)${X}"
    fi
    echo ""

    if [ ! -d "$profiles_dir" ] || [ -z "$(ls -1 "$profiles_dir" 2>/dev/null)" ]; then
        echo -e "  ${Y}⚠ no extra profiles yet — use menu 15 to add one.${X}"
        return 0
    fi

    local -a profs
    mapfile -t profs < <(ls -1 "$profiles_dir" 2>/dev/null | sort)

    echo -e "  ${W}Pick a profile:${X}"
    echo -e "  ${BC}  0)${X} ${BW}(default)${X}  ${D}— legacy dotfiles in \$HOME${X}"
    local i=1
    for p in "${profs[@]}"; do
        local marker=""
        [ "$p" = "$current" ] && marker="  ${G}← active${X}"
        printf "  ${BC}%3d)${X} ${BW}%s${X}%s\n" "$i" "$p" "$marker"
        i=$((i + 1))
    done
    echo -e "  ${BC}  x)${X} cancel"
    echo ""
    read -rp "  pick: " choice

    case "$choice" in
        x|X|'') echo "  cancelled."; return 0 ;;
        0) rm -f "$active_file"; echo -e "  ${G}✅ switched to default profile${X}" ;;
        *[!0-9]*) echo -e "  ${R}enter a number${X}"; return 1 ;;
        *)
            if [ "$choice" -ge 1 ] && [ "$choice" -le "${#profs[@]}" ]; then
                local pick="${profs[$((choice - 1))]}"
                echo "$pick" > "$active_file"
                echo -e "  ${G}✅ active profile:${X} ${BW}$pick${X}"
                echo -e "  ${D}   Sensei + Pupil + stt_server will rebase on next launch.${X}"
                echo -e "  ${D}   Running processes keep their old profile until restart.${X}"
            else
                echo -e "  ${R}out of range${X}"
            fi
            ;;
    esac
}

launch_pupil() {
    log "=== PUPIL LAUNCH ==="
    local html="$HOME/scripts/pupil.html"
    if [ ! -f "$html" ]; then
        echo -e "${R}  ❌ pupil.html not found at $html${X}"
        return 1
    fi
    # Pupil prefers Ollama (free, local) but does not require it; TTS is
    # optional and only affects voice. Both are preferences, not gates.
    if ! curl -s -m 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
        echo -e "${Y}  ⚠  Ollama not responding — attempting start...${X}"
        systemctl start ollama 2>/dev/null || true
        sleep 2
    fi
    # 2026-09-27: was a hard refusal (return 1) -- "Pupil requires local
    # Ollama + at least one model." Elijah: "make it to where it's not so
    # dependent upon the local model. make the local model an option." Local
    # going down (or never being set up) is no longer a reason to block
    # Pupil -- it launches on cloud providers instead. Mirrors the same
    # warn-and-continue block in ~/scripts/pupil.sh; keep the two in sync so
    # a re-sync from this repo can't reintroduce the block.
    if ! curl -s -m 2 http://localhost:11434/api/tags >/dev/null 2>&1 \
       || ! ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -q .; then
        echo ""
        echo -e "${Y}  ⚠  Local Ollama not available — continuing on cloud providers only.${X}"
        echo -e "     Either Ollama isn't running, or no models are pulled."
        echo ""
        echo -e "  ${BW}To add local as an option:${X}"
        echo -e "    ${BG}·${X} ${BW}ollama pull qwen2.5:7b${X}  (the daily-driver brain)"
        echo -e "    ${BG}·${X} Or run option 19 (self-scan) for what your box can handle"
        echo ""
    fi
    if ! pgrep -f "tts_server.py" > /dev/null; then
        echo -e "${C}  Starting TTS server (for Pupil voice)...${X}"
        python3 "$HOME/scripts/tts_server.py" > /tmp/tts_server.log 2>&1 &
        sleep 1
    fi
    # Serve via stt_server on :8080 so /keys auto-syncs with menu 11's
    # ~/.master_ai_keys. Falls back to file:// if the server isn't up.
    local url="file://$html"
    if systemctl --user is-active --quiet master-ai-ui.service 2>/dev/null \
         || pgrep -f "stt_server.py" >/dev/null 2>&1; then
        # Confirm pupil.html is reachable through the server before switching
        if curl -sf -o /dev/null -m 2 "http://localhost:8080/pupil.html"; then
            url="http://localhost:8080/pupil.html"
            echo -e "${G}  ✅ Opening Pupil — keys auto-synced from menu 11 (${url})${X}"
        else
            echo -e "${Y}  ⚠  server on :8080 up but /pupil.html not reachable — falling back to file://${X}"
        fi
    else
        echo -e "${Y}  ⚠  stt_server not running — keys won't auto-sync; opening via file://${X}"
    fi
    [ "$url" = "file://$html" ] && echo -e "${G}  ✅ Opening Pupil in Firefox...${X}"
    open_once "$url" "Pupil"
}

launch_remote() {
    # Option 6 — "Remote": the peer-node bridge UI.
    # Same provider list as Pupil, but Ollama host is configurable so it
    # can point at another Master AI node's IP for true remote chat.
    # Shown to buyers as "Remote," not as the internal "SKS" codename.
    log "=== REMOTE LAUNCH ==="

    if ! pgrep -f "npm run dev" > /dev/null; then
        cd "$HOME/Downloads/sunkissed-soul"
        npm run dev > "$LOG_FILE.remote" 2>&1 &
        sleep 5
        echo -e "${G}  ✅ Remote UI started on :5173${X}"
    else
        echo -e "${G}  ✅ Remote UI already running.${X}"
    fi
    sleep 2

    # ── Hookup info the buyer needs ────────────────────────────
    # Everything required to point a different device at THIS node.
    local host_ip tailscale_ip hostname_val
    hostname_val=$(hostname 2>/dev/null)
    host_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    tailscale_ip=$(command -v tailscale >/dev/null 2>&1 && tailscale ip -4 2>/dev/null | head -1)

    echo ""
    echo -e "${BC}  ── REMOTE HOOKUP INFO ─────────────────────────────${X}"
    echo -e "  ${BW}Host machine:${X}      ${hostname_val:-unknown}"
    echo -e "  ${BW}Local IP (LAN):${X}    ${host_ip:-unknown}"
    [ -n "$tailscale_ip" ] && echo -e "  ${BW}Tailscale IP:${X}      $tailscale_ip" \
                         || echo -e "  ${BW}Tailscale:${X}         ${D}not installed (install for secure remote access beyond LAN)${X}"
    echo ""
    echo -e "  ${BW}Ports served by this node:${X}"
    echo -e "    ${C}:8080${X}   Pupil + /keys + /sessions          ${D}(HTTP)${X}"
    echo -e "    ${C}:5173${X}   Remote UI (this screen)            ${D}(HTTP)${X}"
    echo -e "    ${C}:11434${X}  Ollama (model runtime)             ${D}(HTTP)${X}"
    echo -e "    ${C}:5050${X}   TTS (voice synth)                  ${D}(HTTP)${X}"
    echo ""
    echo -e "  ${BW}From another device, open:${X}"
    [ -n "$host_ip" ] && echo -e "    ${G}http://${host_ip}:8080/pupil.html${X}      ${D}(LAN)${X}"
    [ -n "$tailscale_ip" ] && echo -e "    ${G}http://${tailscale_ip}:8080/pupil.html${X}  ${D}(Tailscale — any network)${X}"
    echo ""
    echo -e "  ${D}Tip: for phone access, Tailscale is the easiest path.${X}"
    echo -e "  ${D}  1) install Tailscale on both this machine and your phone${X}"
    echo -e "  ${D}  2) both devices log into the same Tailscale account${X}"
    echo -e "  ${D}  3) open the Tailscale IP above from your phone browser${X}"
    echo ""

    # Open directly on the /Assistant route — skip the root landing page.
    open_once "http://localhost:5173/Assistant" "Remote"
}

# Backward-compat alias so old scripts / muscle memory keep working.
launch_sunkissed() { launch_remote "$@"; }

view_sessions() {
    local CHATS="$HOME/.master_ai_chats"
    local PC_SESSIONS="$HOME/scripts/sessions"
    clear
    banner_master_ai
    echo -e "\n  ${BC}◈ Saved Chat Sessions${X}  ${W}(~/.master_ai_chats/)${X}"
    echo -e "  ${C}─────────────────────────────────────────${X}"
    local files=( $(ls -t "$CHATS"/*.json.gz "$CHATS"/*.json 2>/dev/null) )
    if [ ${#files[@]} -eq 0 ]; then
        echo -e "  ${W}No saved sessions yet.${X}"
    else
        local i=1
        for f in "${files[@]}"; do
            local base=$(basename "$f")
            local size=$(du -h "$f" | cut -f1)
            local date=$(date -r "$f" "+%Y-%m-%d %I:%M %p" 2>/dev/null)
            echo -e "  ${Y}$i)${W} $base  ${C}[$size]${W}  $date${X}"
            ((i++))
        done
        echo ""
        echo -ne "  ${BC}[MAI]${X} ${Y}▸ ${X}Enter number to view, or x to go back: "
        read -r SEL
        if [[ "$SEL" =~ ^[0-9]+$ ]] && [ "$SEL" -ge 1 ] && [ "$SEL" -le ${#files[@]} ]; then
            local chosen="${files[$((SEL-1))]}"
            echo ""
            if [[ "$chosen" == *.gz ]]; then
                python3 -c "import gzip,json,sys; d=json.load(gzip.open('$chosen','rt')); [print(m.get('role','?').upper()+': '+m.get('content','')) for m in d.get('messages',[])]" 2>/dev/null | less
            else
                python3 -c "import json; d=json.load(open('$chosen')); [print(m.get('role','?').upper()+': '+m.get('content','')) for m in d.get('messages',[])]" 2>/dev/null | less
            fi
        fi
    fi
    echo ""
    echo -e "  ${BC}◈ PC Control Sessions${X}  ${W}(~/scripts/sessions/)${X}"
    echo -e "  ${C}─────────────────────────────────────────${X}"
    local pcfiles=( $(ls -t "$PC_SESSIONS"/*.log 2>/dev/null) )
    if [ ${#pcfiles[@]} -eq 0 ]; then
        echo -e "  ${W}No PC control sessions yet.${X}"
    else
        for f in "${pcfiles[@]}"; do
            local size=$(du -h "$f" | cut -f1)
            echo -e "  ${Y}▸${W} $(basename "$f")  ${C}[$size]${X}"
        done
        echo ""
        echo -ne "  ${BC}[MAI]${X} ${Y}▸ ${X}Enter filename to view (or x): "
        read -r PSEL
        [[ "$PSEL" != "x" && "$PSEL" != "X" && -f "$PC_SESSIONS/$PSEL" ]] && less "$PC_SESSIONS/$PSEL"
    fi
}

main_menu() {
    clear
    banner_master_ai
    echo ""

    # Active profile indicator — blank when default, named when switched
    local active_profile=""
    [ -f "$HOME/.master_ai_active_profile" ] && active_profile=$(cat "$HOME/.master_ai_active_profile" 2>/dev/null | tr -d '[:space:]')
    if [ -n "$active_profile" ] && [ -d "$HOME/.master_ai_profiles/$active_profile" ]; then
        echo -e "  ${BC}👤 active profile:${X} ${BW}$active_profile${X}  ${D}(memory / chats / tasks are per-profile)${X}"
        echo ""
    fi

    # 2-column row helper — if right col empty, print left col full-width
    row() {
        if [ -z "$3" ]; then
            printf "  ${Y}%3s)${W} %s${X}\n" "$1" "$2"
        else
            printf "  ${Y}%3s)${W} %-32s${Y}%4s)${W} %s${X}\n" "$1" "$2" "$3" "$4"
        fi
    }
    section() { echo -e "\n  ${BC}── $1 ──${X}"; }

    section "START"
    row  "1" "Start everything"               "4" "Open Sensei terminal"
    row  "5" "Open Pupil browser"             "6" "Connect remote node"

    section "HEALTH"
    row  "2" "Ollama health check"            "3" "RustDesk health check"

    section "RECOVERY"
    row  "7" "Restart Sensei cleanly"         ""   ""

    section "WORK"
    row  "8" "Review chat sessions"           "9" "Capture idea or POC"

    section "SETUP"
    row "10" "Working agreement"             "11" "API keys"
    row "12" "Clean and tune this PC"        "13" "Learn Python and AI"
    row "14" "Uninstall Master AI"           "15" "Add user profile"
    row "16" "Projects and pinned tasks"     "17" "Switch user profile"
    row "18" "Mesh and peer routing"         "19" "Self-scan this machine"
    row "20" "Download links"                "21" "Benchmark local vs cloud"

    echo ""
    echo -e "  ${Y}x)${W} Exit${X}"
    echo ""
    echo -ne "${C}  Choose: ${X}"
    # Single-key dispatch — no Enter required for common options.
    # 2026-04-22: matches Sensei's 5-button confirm UX — "press the
    # number, it automatically enters." On phone via RustDesk, Enter
    # is a two-tap motion; this removes it for options 3-9 and x.
    # Options 1 and 2 wait 0.5s for a possible second digit (so 10-19
    # and 20-21 still work via two fast keypresses).
    read -n 1 -r CHOICE
    case "$CHOICE" in
        1|2)
            # Could be 1/2 alone or the first digit of 10-19/20-21.
            # 2.5-second grace window after the first digit to collect
            # the second — gives comfortable time to double-tap on
            # phone/voice-to-text. If nothing arrives, we fire on the
            # first digit alone.
            if read -n 1 -t 2.5 -r SECOND 2>/dev/null; then
                if [[ "$SECOND" =~ [0-9] ]]; then
                    CHOICE="${CHOICE}${SECOND}"
                fi
            fi
            ;;
    esac
    echo  # newline after single-keypress for clean formatting

    # Short pause after read-only check commands so output is readable
    # before the menu redraws. Enter to continue, or auto-skip after 8 sec.
    pause_read() {
        echo ""
        read -t 8 -rp "  [press Enter to return — or wait 8s] " _ || true
    }

    case "$CHOICE" in
        1)  startup; pause_read ;;
        2)  check_ollama; pause_read ;;
        3)  check_rustdesk; pause_read ;;
        4)  launch_master_ai_terminal ;;
        5)  launch_pupil ;;
        6)  launch_remote; pause_read ;;
        7)  echo -e "  ${Y}🔄 Force-rebuilding Sensei (kills tmux session, fresh start)...${X}"
            bash ~/scripts/master_ai_kick.sh
            pause_read ;;
        8)  view_sessions; pause_read ;;
        9)  prompt_idea; pause_read ;;
        10) less ~/scripts/howwework.txt ;;
        11) bash ~/scripts/update_keys.sh; pause_read ;;
        12) sudo bash ~/scripts/system_tune.sh; pause_read ;;
        13) bash ~/scripts/learn.sh; pause_read ;;
        14) bash ~/scripts/uninstall.sh; pause_read ;;
        15) add_user; pause_read ;;
        16) view_projects; pause_read ;;
        17) switch_user; pause_read ;;
        18) bash ~/scripts/mesh.sh menu ;;
        19) bash ~/scripts/selfscan.sh; pause_read ;;
        20) less ~/scripts/LINKS.md ;;
        21) bash ~/scripts/benchmark_sensei.sh; pause_read ;;
        x|X) log "--- Script Exited ---"; echo -e "${G}Goodbye.${X}"; exit 0 ;;
        *) echo "Invalid option."; pause_read ;;
    esac

    main_menu
}

# ── First-launch walkthrough — runs ONCE after install ────────
# Sentinel: ~/.master_ai_first_run_done (touched at end).
# install.sh hands off to master.sh; on the very first run this fires
# the API-key + local-AI walkthrough. Every subsequent `master` skips it.
first_launch_walkthrough() {
    local sentinel="$HOME/.master_ai_first_run_done"
    [ -f "$sentinel" ] && return 0

    clear
    echo ""
    echo -e "  ${BC}╔══════════════════════════════════════════════════════╗${X}"
    echo -e "  ${BC}║${X}  ${BW}🥷  WELCOME TO MASTER AI${X}                          ${BC}║${X}"
    echo -e "  ${BC}╚══════════════════════════════════════════════════════╝${X}"
    echo ""
    echo -e "  ${BW}Master AI is a local-first AI workspace.${X}"
    echo -e "  It runs on your machine. No hosted login, no account."
    echo ""
    echo -e "  Add free cloud API keys for faster answers and stronger"
    echo -e "  reasoning, or skip and run fully local."
    echo ""
    read -rp "  [press Enter to continue] " _

    clear
    echo ""
    echo -e "  ${BC}━━━ API keys (recommended, not required) ━━━${X}"
    echo ""
    echo -e "  ${BW}Best starter key — OpenRouter${X}"
    echo -e "    one signup, multiple free cloud models"
    echo -e "    ${C}https://openrouter.ai/keys${X}"
    echo ""
    echo -e "  ${D}Any key works — we auto-detect the provider${X}"
    echo -e "  ${D}(Groq, Gemini, OpenRouter, OpenAI, HuggingFace, …)${X}"
    echo ""
    read -rp "  Paste a key now (or Enter to skip): " key
    while [ -n "$key" ]; do
        python3 - "$key" <<'PY'
import json, os, sys
key = sys.argv[1]
field = None
if   key.startswith("sk-or-v1-"): field = "openrouter"
elif key.startswith("gsk_"):      field = "groq"
elif key.startswith("AIzaSy"):    field = "gemini"
elif key.startswith("sk-proj-"):  field = "openai"
elif key.startswith("sk-ant-"):   field = "anthropic"
elif key.startswith("sk-"):       field = "deepseek"
elif key.startswith("hf_"):       field = "huggingface"
elif key.startswith("xai-"):      field = "xai"
if not field:
    print("  ? couldn't identify key prefix — skipped"); sys.exit(0)
path = os.path.expanduser("~/.master_ai_keys")
try: d = json.load(open(path))
except Exception: d = {}
if field in d and d[field] and d[field] != key:
    d[field + "_2"] = key; print(f"  ✅ {field}: saved as SECONDARY")
else:
    d[field] = key; print(f"  ✅ {field}: saved as PRIMARY")
with open(path, "w") as f: json.dump(d, f, indent=2)
os.chmod(path, 0o600)
PY
        echo ""
        read -rp "  Paste another, or Enter to finish: " key
    done

    echo ""
    echo -e "  ${BC}━━━ Local AI setup ━━━${X}"
    echo ""
    echo -e "  ${BW}Pupil${X} (the browser UI) is for local-first runs."
    echo -e "  Without local, ChatGPT or Gemini will serve you better."
    echo ""
    echo -e "  Type ${BG}go${X} and we'll pull the local AI models, start"
    echo -e "  the services, and walk you through Tailscale."
    echo ""
    read -rp "  Type 'go' to set up local now (or Enter for menu): " choice
    case "$choice" in
        go|GO|Go) walkthrough_go_setup ;;
    esac

    touch "$sentinel"
    echo ""
    echo -e "  ${BG}✓ Walkthrough done.${X} Add keys later from menu 11."
    echo -e "  Type ${BW}master${X} for this menu, ${BW}sensei${X} for the terminal agent."
    echo ""
    read -rp "  [press Enter to continue to menu] " _
}

# Run by `go` inside the walkthrough — no sudo from here, ever.
walkthrough_go_setup() {
    echo ""
    echo -e "  ${BC}━━━ go: setting up local AI ━━━${X}"
    echo ""

    if ! curl -s --max-time 3 "$OLLAMA_URL" >/dev/null 2>&1; then
        echo -e "  ${Y}⚠ Ollama daemon not responding — starting it...${X}"
        systemctl start ollama 2>/dev/null || (nohup ollama serve >/tmp/ollama.log 2>&1 &)
        sleep 2
    fi
    if curl -s --max-time 3 "$OLLAMA_URL" >/dev/null 2>&1; then
        echo -e "  ${G}✓ Ollama running${X}"
    else
        echo -e "  ${R}❌ Ollama not reachable — check 'systemctl status ollama'${X}"
        return 1
    fi

    if ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -q '^qwen2.5:7b$'; then
        echo -e "  ${G}✓ qwen2.5:7b already pulled${X}"
    else
        echo -e "  ${C}  pulling qwen2.5:7b (the daily-driver brain, ~4.7 GB)...${X}"
        ollama pull qwen2.5:7b
    fi

    if pgrep -f stt_server.py >/dev/null 2>&1; then
        echo -e "  ${G}✓ stt_server already running${X}"
    elif [ -f "$HOME/scripts/serve_ui.sh" ]; then
        bash "$HOME/scripts/serve_ui.sh" > /tmp/ui_server.log 2>&1 &
        sleep 1
        echo -e "  ${G}✓ stt_server started${X}"
    else
        echo -e "  ${Y}⚠ serve_ui.sh missing — Pupil may not be reachable on :8080${X}"
    fi

    if command -v tailscale >/dev/null 2>&1; then
        local tsip
        tsip=$(tailscale ip -4 2>/dev/null | head -1)
        if [ -n "$tsip" ]; then
            echo -e "  ${G}✓ Tailscale active — IP: $tsip${X}"
        else
            echo -e "  ${Y}⚠ Tailscale installed but not logged in. Run in another terminal:${X}"
            echo -e "    ${BW}sudo tailscale up${X}"
        fi
    else
        echo -e "  ${Y}⚠ Tailscale not installed. To enable Pupil from any network,${X}"
        echo -e "  ${Y}  run this in another terminal (it needs sudo):${X}"
        echo -e "    ${BW}curl -fsSL https://tailscale.com/install.sh | sh${X}"
        echo -e "  ${D}  Then: sudo tailscale up${X}"
    fi

    echo ""
    echo -e "  ${G}✓ go: done.${X}"
}

first_launch_walkthrough
main_menu
