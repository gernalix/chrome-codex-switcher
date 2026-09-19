# Chrome ↔ Codex Switcher

Fedora/Wayland workflow helper for pairing a specific Chrome tab with a specific Codex Desktop thread.

## What it does

- Adds a movable/resizable floating note to normal Chrome pages.
- Pairs one Chrome tab-context with one `codex://threads/...` Codex thread.
- Chrome → Codex: one extension shortcut opens the exact paired Codex thread with `gio open`.
- Codex → Chrome: press Codex Desktop's **Copy chat deep link** shortcut; the daemon detects the `codex://threads/...` clipboard value and focuses the exact paired Chrome tab.
- Persists notes and pairings in SQLite.
- Restores/focuses the right Chrome tab even with many windows/tabs; if the paired tab is closed, it reopens its URL.
- Provides a searchable Chrome side panel for all contexts.
- Ships an optional GNOME Shell overlay prototype for showing the current note over Codex Desktop.

The core intentionally avoids Wayland window automation. Chrome controls its own tabs; Codex is addressed through its registered `codex://` deep links.

## Architecture

```text
Codex Desktop --Copy deep link--> Wayland clipboard --wl-paste-->
                                                        Fedora daemon
Chrome extension <------- HTTP/event polling 127.0.0.1 -------^ |
       |                                                     | |
       +-- focus exact tab                                   | +-- gio open codex://threads/...
       +-- floating note / side panel                        +---- SQLite state
```

The loopback HTTP bridge is deliberate: Chrome Flatpak can make local network requests, while classic Native Messaging is fragile in sandboxed Chrome installs.

## Install on Fedora

Requirements:

```bash
sudo dnf install wl-clipboard
```

Then:

```bash
git clone https://github.com/gernalix/chrome-codex-switcher.git
cd chrome-codex-switcher
./install.sh
```

`install.sh` installs the Python helper under `~/.local/share/chrome-codex-switcher`, the CLI as `~/.local/bin/context-twin`, and enables the user systemd service.

Load the unpacked Chrome extension:

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Choose **Load unpacked**.
4. Select the path printed by `./install.sh` (normally `~/.local/share/chrome-codex-switcher/extension`).

The extension has a fixed development key, so its ID remains stable across reinstalls.

Run:

```bash
context-twin status
```

Expected core checks:

- daemon reachable;
- `codex://` handler registered;
- `wl-paste` available.

## Workflow

### Pair Chrome Y ↔ Codex X

1. In Chrome Y, press **Alt+Shift+L** or click **Link Codex** in the floating note.
2. Switch to Codex X.
3. In Codex Desktop press **Copy chat deep link** (`Ctrl+Alt+L` on the current Linux build).
4. The daemon stores the pair.

### Chrome → Codex

While Chrome Y is active, press **Alt+Shift+T** or click **↔ Codex**.

### Codex → Chrome

While Codex X is active, press Codex's **Copy chat deep link** shortcut. When auto-switch is enabled (default), the clipboard watcher resolves X and asks the extension to focus Chrome Y.

Copying a Codex deep link intentionally acts as “switch to twin” outside pairing mode. This can be disabled from the API/CLI later if desired.

### Floating note

The Chrome note is:

- draggable;
- resizable;
- collapsible;
- hideable;
- persisted per tab-context;
- searchable from the side panel.

Use **Alt+Shift+N** to show/hide the note in the active Chrome tab.

## CLI

```bash
context-twin status
context-twin list
context-twin capture-clipboard
context-twin overlay-state
```

## Data

SQLite:

```text
~/.local/state/chrome-codex-switcher/state.sqlite3
```

Overlay cache:

```text
~/.cache/chrome-codex-switcher/overlay.json
```

The HTTP server binds only to `127.0.0.1:43817`. Mutating browser requests are accepted only from the fixed extension origin or from local non-browser helper processes.

## Optional Codex floating overlay

`contrib/gnome-extension@gernalix.github.com/` contains a GNOME Shell 49/50 prototype that reads the daemon's overlay cache and displays the paired note when a Codex/ChatGPT desktop window is focused.

It is not required for switching. It is intentionally isolated from the core so a GNOME update cannot break pairing or navigation.

## Tests

```bash
PYTHONPATH=host python3 -m unittest discover -s tests -v
python3 -m py_compile host/chrome_codex_switcher/*.py
```
