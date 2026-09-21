#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${HOME}/.local/share/chrome-codex-switcher"
BIN_DIR="${HOME}/.local/bin"
SYSTEMD_DIR="${HOME}/.config/systemd/user"

command -v python3 >/dev/null || { echo "ERROR: python3 is required" >&2; exit 1; }
command -v gio >/dev/null || { echo "ERROR: gio is required" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "ERROR: systemctl is required" >&2; exit 1; }

mkdir -p "$PREFIX" "$BIN_DIR" "$SYSTEMD_DIR"
rm -rf "$PREFIX/host" "$PREFIX/contrib"
cp -a "$ROOT/host" "$PREFIX/host"
cp -a "$ROOT/contrib" "$PREFIX/contrib"

# Keep the deployed extension directory itself stable. Flatpak Chrome may keep
# an unpacked-extension grant through the document portal (/run/flatpak/doc/*).
# Replacing this directory invalidates that grant and makes Reload fail with
# "File path cannot be resolved". Refresh only its contents instead.
mkdir -p "$PREFIX/extension"
find "$PREFIX/extension" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
cp -a "$ROOT/extension/." "$PREFIX/extension/"
install -m 0644 "$ROOT/systemd/chrome-codex-switcher.service" "$SYSTEMD_DIR/chrome-codex-switcher.service"

cat > "$BIN_DIR/context-twin" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="${HOME}/.local/share/chrome-codex-switcher/host${PYTHONPATH:+:${PYTHONPATH}}"
exec /usr/bin/python3 -m chrome_codex_switcher.cli "$@"
WRAPPER
chmod 0755 "$BIN_DIR/context-twin"

systemctl --user daemon-reload
systemctl --user enable chrome-codex-switcher.service
systemctl --user restart chrome-codex-switcher.service

bridge_ready=0
for _ in {1..20}; do
  if /usr/bin/python3 -c 'import json, urllib.request; p=json.load(urllib.request.urlopen("http://127.0.0.1:43817/api/health", timeout=0.5)); raise SystemExit(0 if p.get("ok") else 1)' >/dev/null 2>&1; then
    bridge_ready=1
    break
  fi
  sleep 0.1
done
if [[ "$bridge_ready" -ne 1 ]]; then
  echo "ERROR: chrome-codex-switcher daemon did not become healthy after deployment." >&2
  systemctl --user --no-pager --full status chrome-codex-switcher.service >&2 || true
  exit 1
fi

if [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* ]] && command -v gnome-extensions >/dev/null; then
  "$PREFIX/contrib/install-gnome-overlay.sh"
fi

if ! command -v wl-paste >/dev/null; then
  echo
  echo "INFO: wl-paste is missing. This is fine on GNOME when the GNOME companion is enabled."
  echo "      Other Wayland compositors can use: sudo dnf install wl-clipboard"
fi

echo
echo "Installed."
echo "Chrome extension path: $PREFIX/extension"
echo "Open chrome://extensions -> Developer mode -> Load unpacked -> select that directory."
echo "Then run: $BIN_DIR/context-twin status"
