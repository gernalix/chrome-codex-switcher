#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
UUID="chrome-codex-switcher@gernalix.github.com"
DEST="$HOME/.local/share/gnome-shell/extensions/$UUID"
mkdir -p "$(dirname "$DEST")"
rm -rf "$DEST"
cp -a "$ROOT/gnome-extension@gernalix.github.com" "$DEST"
echo "Installed GNOME overlay source to: $DEST"
echo "GNOME Shell must load the new extension before it can be enabled. On Wayland, log out/in if gnome-extensions enable fails."
if command -v gnome-extensions >/dev/null; then
  gnome-extensions enable "$UUID" 2>/dev/null || true
fi
