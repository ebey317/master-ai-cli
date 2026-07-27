#!/bin/bash
# Apply S05 (i915 safety) → sync disks → reboot.
# Run with: sudo bash ~/scripts/fix_and_reboot.sh
# Idempotent. 5-second abort window before reboot.

set -e

if [ "$EUID" -ne 0 ]; then
    echo "This script needs root. Run: sudo bash $0"
    exit 1
fi

echo ""
echo "=== Master AI — Apply freeze fix + reboot ==="
echo ""

echo "[1/3] Applying S05 (i915 PSR/FBC safety parameters)..."
bash /home/user/scripts/apply_i915_safety.sh

echo ""
echo "[2/3] Flushing disk buffers..."
sync
echo "      done."

echo ""
echo "[3/3] Rebooting in 5 seconds. Ctrl-C to abort."
for i in 5 4 3 2 1; do
    echo "      $i..."
    sleep 1
done

echo ""
echo "Rebooting now."
reboot
