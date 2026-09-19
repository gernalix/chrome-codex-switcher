# Chrome ↔ Codex Switcher

Fedora/Wayland workflow helper for pairing a specific Chrome tab with a specific Codex Desktop thread.

## What it does

- Adds a movable/resizable floating note to normal Chrome pages.
- Pairs one Chrome tab-context with one `codex://threads/...` Codex thread.
- Optionally binds that pair to an explicit six-digit roadmap `PROMPT_ID`; the ID is never inferred from URLs, tab titles or Codex deep links.
- Chrome → Codex: one extension shortcut opens the exact paired Codex thread with `gio open`.
- Codex → Chrome: press Codex Desktop's **Copy chat deep link** shortcut; on GNOME Wayland the GNOME companion observes the clipboard change inside the compositor and forwards the `codex://threads/...` value to the daemon, which focuses the exact paired Chrome tab.
- Persists notes and pairings in SQLite.
- Shows the note over the active Codex conversation and hides it whenever the active thread cannot be resolved safely, so a stale note is never shown over a different chat.
- Lets the note be edited from either Chrome or Codex. By default both surfaces share one note; enable **Separate Chrome/Codex notes** to keep two independent values for the same pair.
- Restores/focuses the right Chrome tab even with many windows/tabs; if the paired tab is closed, it reopens its URL.
- Provides a searchable Chrome side panel for all contexts.
- Ships a GNOME Shell companion that provides both the Codex floating-note overlay and a native GNOME Wayland clipboard bridge.

The core intentionally avoids Wayland window automation. Chrome controls its own tabs; Codex is addressed through its registered `codex://` deep links.

## Architecture

```text
Codex Desktop --Copy deep link--> Wayland clipboard
                                  |-- GNOME Shell owner-changed --> GNOME companion --+
                                  |-- wl-paste --watch (other compositors) ------------+--> Fedora daemon
Chrome extension <---------------- HTTP/event polling 127.0.0.1 ----------------------^ |
       |                                                                                |
       +-- focus exact tab                                                              +-- gio open codex://threads/...
       +-- floating note / side panel                                                   +-- SQLite state
```

The loopback HTTP bridge is deliberate: Chrome Flatpak can make local network requests, while classic Native Messaging is fragile in sandboxed Chrome installs.

## Install on Fedora

On GNOME Wayland no compositor change and no data-control protocol are required. The installer deploys the GNOME companion automatically when GNOME is detected. On other Wayland compositors, `wl-clipboard` remains the fallback:

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
- an active clipboard backend: `gnome-shell` on GNOME Wayland, otherwise a working `wl-paste --watch`.

If the GNOME companion was installed for the first time but is not active yet, log out/in once and run:

```bash
gnome-extensions enable chrome-codex-switcher@gernalix.github.com
context-twin status
```

## Workflow

### Pair Chrome Y ↔ Codex X

1. In Chrome Y, press **Alt+Shift+K** or click **Link Codex** in the floating note.
2. Switch to Codex X.
3. In Codex Desktop press **Copy chat deep link** (`Ctrl+Alt+L` on the current Linux build).
4. The daemon stores the pair.

### Chrome → Codex

While Chrome Y is active, press **Alt+Shift+G** or click **↔ Codex**.

### Codex → Chrome

While Codex X is active, press Codex's **Copy chat deep link** shortcut. When auto-switch is enabled (default), the active clipboard backend resolves X and asks the extension to focus Chrome Y.

On GNOME Wayland this does **not** depend on `wl-paste --watch`: the GNOME Shell companion listens to the compositor's clipboard ownership change signal and reads the clipboard through GNOME Shell itself.

Copying a Codex deep link intentionally acts as “switch to twin” outside pairing mode. This can be disabled from the API/CLI later if desired.

### Floating note

The note can be edited from both paired surfaces:

- Chrome: editable textarea, draggable/resizable/collapsible/hideable;
- Codex: editable GNOME overlay bound to the currently resolved Codex thread;
- default mode: one shared note, so an edit on either side appears on the other;
- optional **Separate Chrome/Codex notes** checkbox: Chrome and Codex keep independent note text for the same pair;
- persisted per tab-context and searchable from the Chrome side panel.

When Codex changes to a conversation that cannot be resolved unambiguously, the overlay is hidden instead of reusing the previous chat's note.

Use **Alt+Shift+V** to show/hide the note in the active Chrome tab.

## CLI

```bash
context-twin status
context-twin list
context-twin capture-clipboard
context-twin overlay-state
context-twin verify-prompt 947306
context-twin verify-prompt 947306 --full
context-twin verify-binding 947306
context-twin verify-note 947306
context-twin verify-overlay
context-twin verify-workflowy 947306
context-twin self-test
```

`verify-prompt` is the programmatic control plane for Codex/automation. The
read-only form validates the explicit PROMPT_ID binding, real Chrome tab/content
script and the context/thread twin. `--full` also opens/focuses the bound Codex
thread, waits for the GNOME companion heartbeat, exercises shared-note
propagation in both directions with automatic snapshot/restore, checks overlay
invalidation/recovery, and verifies Chrome focus acknowledgement.

If the Chrome context is missing, the verifier asks the extension to create a
fresh canonical ChatGPT context rather than guessing among existing tabs. If the
Codex thread is missing, it may pair the one unique recent native Codex session
that explicitly contains the same six-digit PROMPT_ID; ambiguous matches fail
closed.

`GET /api/verify/prompt/947306` runs the full verifier and returns JSON
`result: PASS|BLOCKED`, granular `gates`, and a concrete `blocker` on failure.
The focused CLI commands reuse the same gates (`verify-note` exercises the
bidirectional note snapshot/restore; `verify-overlay` reads the currently
focused GNOME overlay). Workflowy projects a `🔎 Verify` action for each
prompt; clicking it displays `✅ Runtime verified` or `❌ Runtime failed` with
the blocker. A full PASS additionally requires that action to be rendered in
an open Workflowy tab. No browser screenshots or title-based thread guessing
are involved.

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

## GNOME companion

`contrib/gnome-extension@gernalix.github.com/` supports GNOME Shell 49/50. It has two jobs:

- use AT-SPI to resolve the selected Codex/ChatGPT conversation and display only the matching paired note;
- provide an editable Codex note plus the **Separate Chrome/Codex notes** toggle;
- bridge copied Codex deep links to the daemon using GNOME Shell's native clipboard APIs.

The clipboard access is deliberate and limited to values matching `codex://threads/...`; unrelated clipboard text is ignored. On GNOME Wayland this companion is the preferred Codex → Chrome backend because GNOME does not expose the wlroots data-control protocol required by `wl-paste --watch`.

## Tests

```bash
PYTHONPATH=host python3 -m unittest discover -s tests -v
python3 -m py_compile host/chrome_codex_switcher/*.py
```


## Workflowy roadmap cockpit

The localhost API supports explicit roadmap bindings:

- `GET /api/prompt?prompt_id=514458` returns the current Chrome/Codex binding.
- `POST /api/prompt/bind` binds a known Chrome context to that exact PROMPT_ID.
- `POST /api/prompt/arm` marks that exact prompt as the next Codex deep-link association.
- `POST /api/prompt/open-codex` opens the exact bound Codex thread.

The Chrome extension recognizes Workflowy action links under
`http://127.0.0.1:43817/ui/prompt/<PROMPT_ID>/...`. From Workflowy it can copy
the canonical prompt through the local Workflowy bridge, open/focus the prompt's
ChatGPT tab in the same Chrome window, and open the exact Codex thread.

If a prompt has no Chrome context yet, the launch action creates one and stores
the PROMPT_ID explicitly before Codex pairing. The PROMPT_ID is never guessed
from the browser URL or the Codex deep link.
