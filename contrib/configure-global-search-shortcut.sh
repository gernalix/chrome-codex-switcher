#!/usr/bin/env bash
set -euo pipefail

MEDIA_KEYS_SCHEMA='org.gnome.settings-daemon.plugins.media-keys'
SHORTCUT_PATH='/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/chrome-codex-switcher/'
SHORTCUT_SCHEMA="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:${SHORTCUT_PATH}"
DASHBOARD_URL='http://127.0.0.1:43817/ui/search'

remove_shortcut() {
  local existing updated
  existing="$(gsettings get "$MEDIA_KEYS_SCHEMA" custom-keybindings)"
  updated="$(python3 - "$existing" "$SHORTCUT_PATH" <<'PY'
import ast
import sys

paths = ast.literal_eval(sys.argv[1])
path = sys.argv[2]
print(repr([item for item in paths if item != path]))
PY
)"
  gsettings set "$MEDIA_KEYS_SCHEMA" custom-keybindings "$updated"
  gsettings reset "$SHORTCUT_SCHEMA" name || true
  gsettings reset "$SHORTCUT_SCHEMA" command || true
  gsettings reset "$SHORTCUT_SCHEMA" binding || true
}

if [[ "${1:-install}" == 'uninstall' ]]; then
  remove_shortcut
  exit 0
fi

command -v gsettings >/dev/null || { echo 'ERROR: gsettings is required' >&2; exit 1; }
command -v python3 >/dev/null || { echo 'ERROR: python3 is required' >&2; exit 1; }

RPM_CHROME_BIN=''
for candidate in /usr/bin/google-chrome-stable /usr/bin/google-chrome; do
  if [[ -x "$candidate" ]] && rpm -qf "$candidate" 2>/dev/null | grep -qx 'google-chrome-stable-[^[:space:]]*'; then
    RPM_CHROME_BIN="$candidate"
    break
  fi
done
[[ -n "$RPM_CHROME_BIN" ]] || { echo 'ERROR: Google Chrome RPM executable not found' >&2; exit 1; }

existing="$(gsettings get "$MEDIA_KEYS_SCHEMA" custom-keybindings)"
updated="$(python3 - "$existing" "$SHORTCUT_PATH" <<'PY'
import ast
import sys

paths = ast.literal_eval(sys.argv[1])
path = sys.argv[2]
if path not in paths:
    paths.append(path)
print(repr(paths))
PY
)"
gsettings set "$MEDIA_KEYS_SCHEMA" custom-keybindings "$updated"
gsettings set "$SHORTCUT_SCHEMA" name "'Context Search'"
gsettings set "$SHORTCUT_SCHEMA" command "'$RPM_CHROME_BIN --new-window $DASHBOARD_URL'"
gsettings set "$SHORTCUT_SCHEMA" binding "'<Alt><Shift>s'"

echo "GNOME global search shortcut: Alt+Shift+S -> $RPM_CHROME_BIN $DASHBOARD_URL"
