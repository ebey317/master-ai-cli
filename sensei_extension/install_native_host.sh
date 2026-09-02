#!/bin/bash
set -euo pipefail

ext_id="leffhbjachgdjgniilffijjlhfekjghb"
host_dir="$HOME/.config/google-chrome/NativeMessagingHosts"
host_path="$host_dir/com.master_ai.sensei_extension.json"
src="$(cd "$(dirname "$0")" && pwd)/native_messaging/com.master_ai.sensei_extension.json"

mkdir -p "$host_dir"
cp "$src" "$host_path"
chmod +x "$HOME/scripts/sensei_native_host.py"
echo "installed $host_path for extension $ext_id"
