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
  SCHEMA_ID="org.gnome.shell.extensions.chrome-codex-switcher"
  SCHEMA_KEY="open-search-dashboard"
  if GSETTINGS_SCHEMA_DIR="$DEST/schemas" gsettings writable "$SCHEMA_ID" "$SCHEMA_KEY" >/dev/null 2>&1; then
    SHORTCUT="$(GSETTINGS_SCHEMA_DIR="$DEST/schemas" gsettings get "$SCHEMA_ID" "$SCHEMA_KEY")"
    echo "GNOME global search shortcut schema: OK ($SHORTCUT)"
  else
    echo "ERROR: GNOME shortcut schema could not be loaded from $DEST/schemas" >&2
    exit 1
  fi
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
