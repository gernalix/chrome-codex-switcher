#!/usr/bin/env bash
set -euo pipefail
for uuid in chrome-codex-switcher-v2@gernalix.github.com chrome-codex-switcher@gernalix.github.com; do
  extension_dir="$HOME/.local/share/gnome-shell/extensions/$uuid"
  if command -v gnome-extensions >/dev/null &&
      enabled=$(gnome-extensions list --enabled 2>/dev/null) &&
      active=$(gnome-extensions list --active 2>/dev/null); then
    if grep -Fxq "$uuid" <<< "$enabled"$'\n'"$active"; then
      gnome-extensions disable "$uuid"
      enabled=$(gnome-extensions list --enabled)
      active=$(gnome-extensions list --active)
      if grep -Fxq "$uuid" <<< "$enabled"$'\n'"$active"; then
        printf 'GNOME companion %s is still enabled or active. Files retained.\n' "$uuid" >&2
        exit 1
      fi
    fi
  elif [[ -d "$extension_dir" ]]; then
    printf 'GNOME session unavailable. Sign out to unload any running companion after removal.\n' >&2
  fi
  rm -rf -- "$extension_dir"
done
if command -v gsettings >/dev/null && command -v python3 >/dev/null; then
  "$HOME/.local/share/chrome-codex-switcher/contrib/configure-global-search-shortcut.sh" uninstall || true
fi
systemctl --user disable --now chrome-codex-switcher.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/chrome-codex-switcher.service"
systemctl --user daemon-reload
rm -f "$HOME/.local/bin/context-twin"
rm -rf "$HOME/.local/share/chrome-codex-switcher"
echo "Removed program files. Persistent state under ~/.local/state/chrome-codex-switcher was left intact."
