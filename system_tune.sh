#!/bin/bash
# ============================================
# your-machine SYSTEM TUNE-UP
# Run: sudo bash ~/scripts/system_tune.sh
# ============================================

LOG="$HOME/scripts/master.log"
TIMESTAMP=$(date "+%Y-%m-%d %I:%M:%S %p")

log() { echo "[$TIMESTAMP] $1" | tee -a "$LOG"; }

if [ "$EUID" -ne 0 ]; then
    echo "⚠️  Please run with sudo: sudo bash ~/scripts/system_tune.sh"
    exit 1
fi

REAL_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
REAL_USER="$SUDO_USER"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║       your-machine SYSTEM TUNE-UP          ║"
echo "╚══════════════════════════════════════════╝"

# ── 1. APT CLEANUP ──────────────────────────────
echo ""
echo "▶ 1/7 Cleaning APT packages..."
apt autoremove -y -qq
apt autoclean -qq
apt clean -qq
echo "✅ APT cleaned."
log "APT cleanup done"

# ── 2. SSD TRIM ─────────────────────────────────
echo ""
echo "▶ 2/7 Running SSD TRIM (NVMe)..."
fstrim -v / 2>&1 | tee -a "$LOG"
echo "✅ TRIM complete."

# ── 3. ENABLE WEEKLY TRIM TIMER ─────────────────
echo ""
echo "▶ 3/7 Enabling weekly fstrim timer..."
systemctl enable fstrim.timer --quiet
systemctl start fstrim.timer --quiet
echo "✅ fstrim.timer active (runs weekly automatically)."
log "fstrim.timer enabled"

# ── 4. DISABLE DEAD SERVICES ────────────────────
echo ""
echo "▶ 4/7 Disabling failed/unused services..."
systemctl disable casper-md5check.service --quiet 2>/dev/null && echo "  ✅ Disabled casper-md5check (live ISO leftover)"
systemctl disable rustdesk-server.service --quiet 2>/dev/null && echo "  ✅ Disabled rustdesk-server (not needed — using client only)"
systemctl reset-failed 2>/dev/null
echo "✅ Failed service list cleared."
log "Dead services disabled"

# ── 5. CLEAR USER CACHES ────────────────────────
echo ""
echo "▶ 5/7 Clearing user caches..."
BEFORE=$(du -sh "$REAL_HOME/.cache" 2>/dev/null | cut -f1)
# Firefox cache (keeps profile/bookmarks/passwords intact)
rm -rf "$REAL_HOME/.cache/mozilla/firefox/"
# Thumbnail cache
rm -rf "$REAL_HOME/.cache/thumbnails/"
# App caches (pip, npm, etc.)
rm -rf "$REAL_HOME/.cache/pip/"
rm -rf "$REAL_HOME/.npm/_cacache/"
AFTER=$(du -sh "$REAL_HOME/.cache" 2>/dev/null | cut -f1)
echo "✅ Cache cleared: $BEFORE → $AFTER"
log "Cache cleared: $BEFORE → $AFTER"

# ── 6. FIX FIREFOX ──────────────────────────────
echo ""
echo "▶ 6/7 Fixing Firefox..."
# Remove lock file if Firefox crashed
rm -f "$REAL_HOME/.config/mozilla/firefox/5jdzwh4v.default-release/lock"
rm -f "$REAL_HOME/.config/mozilla/firefox/5jdzwh4v.default-release/.parentlock"
# Remove broken sqlite WAL journals (cause slow startup)
find "$REAL_HOME/.config/mozilla/firefox/" -name "*.sqlite-wal" -delete 2>/dev/null
find "$REAL_HOME/.config/mozilla/firefox/" -name "*.sqlite-shm" -delete 2>/dev/null
# Repair permissions
chown -R "$REAL_USER:$REAL_USER" "$REAL_HOME/.config/mozilla/" 2>/dev/null
echo "✅ Firefox lock files and broken journals cleared."
log "Firefox cleaned"

# ── 7. INSTALL GOOGLE CHROME ────────────────────
echo ""
echo "▶ 7/7 Installing Google Chrome..."
if command -v google-chrome-stable &>/dev/null || command -v google-chrome &>/dev/null; then
    echo "✅ Google Chrome already installed: $(google-chrome-stable --version 2>/dev/null || google-chrome --version 2>/dev/null)"
else
    curl -fsSLo /tmp/google-chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    apt install -y /tmp/google-chrome.deb
    rm -f /tmp/google-chrome.deb
    echo "✅ Google Chrome installed!"
    log "Google Chrome installed"
fi

# ── SUMMARY ─────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║            TUNE-UP COMPLETE              ║"
echo "╚══════════════════════════════════════════╝"
df -h / | tail -1 | awk '{print "  Disk: "$3" used / "$2" total ("$5" full)"}'
free -h | awk '/Mem:/{print "  RAM:  "$3" used / "$2" total"}'
echo ""
echo "  ✅ APT cleaned"
echo "  ✅ SSD TRIMmed (NVMe)"
echo "  ✅ Weekly TRIM scheduled"
echo "  ✅ Dead services disabled"
echo "  ✅ Caches cleared"
echo "  ✅ Firefox repaired"
echo "  ✅ Google Chrome installed"
echo ""
echo "  Open Chrome: google-chrome-stable &"
echo ""
log "=== SYSTEM TUNE-UP COMPLETE ==="
