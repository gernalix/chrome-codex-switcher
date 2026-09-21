#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
UUID="chrome-codex-switcher@gernalix.github.com"
DEST="$HOME/.local/share/gnome-shell/extensions/$UUID"
mkdir -p "$(dirname "$DEST")"
rm -rf "$DEST"
cp -a "$ROOT/gnome-extension@gernalix.github.com" "$DEST"
if [[ -d "$DEST/schemas" ]]; then
  glib-compile-schemas "$DEST/schemas"
fi
echo "Installed GNOME companion (overlay + clipboard bridge + global search shortcut) to: $DEST"

if command -v gnome-extensions >/dev/null; then
  gnome-extensions enable "$UUID" 2>/dev/null || true
  if gnome-extensions list --enabled 2>/dev/null | grep -Fxq "$UUID"; then
    echo "GNOME companion: enabled"
  else
    echo "GNOME companion: installed but not active yet."
    echo "On Wayland, log out/in once, then run: gnome-extensions enable $UUID"
  fi
else
  echo "WARNING: gnome-extensions command not found; enable $UUID after installation."
fi
