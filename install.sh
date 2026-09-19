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
rm -rf "$PREFIX/host" "$PREFIX/extension" "$PREFIX/contrib"
cp -a "$ROOT/host" "$PREFIX/host"
cp -a "$ROOT/extension" "$PREFIX/extension"
cp -a "$ROOT/contrib" "$PREFIX/contrib"
install -m 0644 "$ROOT/systemd/chrome-codex-switcher.service" "$SYSTEMD_DIR/chrome-codex-switcher.service"

cat > "$BIN_DIR/context-twin" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="${HOME}/.local/share/chrome-codex-switcher/host${PYTHONPATH:+:${PYTHONPATH}}"
exec /usr/bin/python3 -m chrome_codex_switcher.cli "$@"
WRAPPER
chmod 0755 "$BIN_DIR/context-twin"

systemctl --user daemon-reload
systemctl --user enable --now chrome-codex-switcher.service

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
