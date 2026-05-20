#!/usr/bin/env bash
# fix_claf_stream.sh
# Scope: only files under MASTER_DIR that reference CLAF.
# Action: backup → patch stream:true variants to false → py_compile → restore on fail.
# Leaves other cloud lanes (Anthropic, OpenRouter, Fireworks, Groq, Gemini) untouched.

set -u

MASTER_DIR="${MASTER_DIR:-$HOME/master_ai}"
LOG="${LOG:-$MASTER_DIR/master.log}"
TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$HOME/master_ai_backups/claf_stream_$TS"

echo "=== fix_claf_stream.sh ==="
echo "MASTER_DIR = $MASTER_DIR"
echo "BACKUP_DIR = $BACKUP_DIR"
echo

if [ ! -d "$MASTER_DIR" ]; then
    echo "NOT STARTED — MASTER_DIR does not exist: $MASTER_DIR"
    echo "  rerun with: MASTER_DIR=/correct/path bash ~/scripts/fix_claf_stream.sh"
    exit 1
fi

mkdir -p "$BACKUP_DIR"

# --- step 1: confirm the lane name in the log ---
echo "--- last CLAF hits in master.log ---"
if [ -f "$LOG" ]; then
    grep -n -i 'claf' "$LOG" | tail -10
    [ "$(grep -ic 'claf' "$LOG")" = "0" ] && echo "(no CLAF lines in log)"
else
    echo "(log not found at $LOG — skipping)"
fi
echo

# --- step 2: find source files that reference CLAF ---
echo "--- files referencing CLAF under $MASTER_DIR ---"
CLAF_FILES="$(grep -rln -i 'claf' "$MASTER_DIR" \
    --include='*.py' --include='*.json' --include='*.sh' \
    --include='*.yaml' --include='*.yml' --include='*.toml' \
    2>/dev/null || true)"

if [ -z "$CLAF_FILES" ]; then
    echo "NOT STARTED — no source files reference CLAF under $MASTER_DIR"
    echo "  CLAF is named only in the server response, not in your code."
    echo "  tell me which lane file hit the error and I will re-target the patch."
    exit 1
fi
echo "$CLAF_FILES"
echo

# --- step 3: of those, find ones with stream truthy ---
TARGETS=()
for f in $CLAF_FILES; do
    if grep -qE '"stream"[[:space:]]*:[[:space:]]*true|stream[[:space:]]*=[[:space:]]*True|'\''stream'\''[[:space:]]*:[[:space:]]*True' "$f" 2>/dev/null; then
        TARGETS+=("$f")
    fi
done

if [ "${#TARGETS[@]}" = "0" ]; then
    echo "NOT STARTED — CLAF files exist but none contain stream:true / stream=True"
    echo "  the streaming flag may live in a wrapper / config file that does not mention CLAF."
    echo "  rerun with: grep -rn 'stream' \"$MASTER_DIR\" | grep -iE 'true|True'"
    exit 1
fi

echo "--- targets (CLAF files with stream truthy) ---"
printf '  %s\n' "${TARGETS[@]}"
echo

# --- step 4: backup, patch, verify, restore on fail ---
PATCHED=()
RESTORED=()
NOCHANGE=()

for f in "${TARGETS[@]}"; do
    BASENAME="$(basename "$f")"
    cp "$f" "$BACKUP_DIR/$BASENAME.bak"

    python3 - "$f" <<'PYEOF'
import re, sys
path = sys.argv[1]
with open(path, 'r') as fh:
    src = fh.read()
orig = src

# JSON / dict literal:  "stream": true   →   "stream": false
src = re.sub(r'("stream"\s*:\s*)true\b', r'\1false', src)

# Python dict literal:  'stream': True   →   'stream': False
src = re.sub(r"('stream'\s*:\s*)True\b", r'\1False', src)

# Python kwarg:  stream=True   →   stream=False
src = re.sub(r'\bstream\s*=\s*True\b', 'stream=False', src)

if src != orig:
    with open(path, 'w') as fh:
        fh.write(src)
    print("CHANGED")
else:
    print("NOCHANGE")
PYEOF

    RESULT=$?
    LAST_LINE="$(python3 - "$f" <<'PYEOF2'
import re, sys
src = open(sys.argv[1]).read()
hits = (
    len(re.findall(r'"stream"\s*:\s*false\b', src)) +
    len(re.findall(r"'stream'\s*:\s*False\b", src)) +
    len(re.findall(r'\bstream\s*=\s*False\b', src))
)
print(hits)
PYEOF2
)"

    if [ "$RESULT" != "0" ]; then
        cp "$BACKUP_DIR/$BASENAME.bak" "$f"
        RESTORED+=("$f (python edit failed, restored)")
        continue
    fi

    # py_compile only for .py files
    if [[ "$f" == *.py ]]; then
        if ! python3 -m py_compile "$f" 2>/dev/null; then
            cp "$BACKUP_DIR/$BASENAME.bak" "$f"
            RESTORED+=("$f (py_compile failed, restored)")
            continue
        fi
    fi

    # confirm change actually occurred
    if ! diff -q "$BACKUP_DIR/$BASENAME.bak" "$f" >/dev/null 2>&1; then
        PATCHED+=("$f")
    else
        NOCHANGE+=("$f")
    fi
done

# --- step 5: report ---
echo
echo "=== RESULT ==="
for f in "${PATCHED[@]}";  do echo "  PATCHED   $f"; done
for f in "${NOCHANGE[@]}"; do echo "  NO CHANGE $f"; done
for f in "${RESTORED[@]}"; do echo "  RESTORED  $f"; done
echo "  backups   $BACKUP_DIR"
echo

if [ "${#PATCHED[@]}" -gt "0" ]; then
    echo "DONE — CLAF lane now sends stream:false"
    echo "  next: retry the call that produced the 400."
else
    echo "NOT STARTED — no files were modified"
fi
