#!/usr/bin/env bash
set -euo pipefail
systemctl --user disable --now chrome-codex-switcher.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/chrome-codex-switcher.service"
systemctl --user daemon-reload
rm -f "$HOME/.local/bin/context-twin"
rm -rf "$HOME/.local/share/chrome-codex-switcher"
echo "Removed program files. Persistent state under ~/.local/state/chrome-codex-switcher was left intact."
