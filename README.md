# Chrome ↔ Codex Switcher

Fedora/Wayland workflow helper for pairing a specific Chrome tab with a specific Codex Desktop thread.

## What it does

- Adds a movable/resizable floating note to normal Chrome pages.
- Pairs one Chrome tab-context with one `codex://threads/...` Codex thread.
- Optionally binds that pair to an explicit six-digit roadmap `PROMPT_ID`; this canonical binding is never inferred from URLs, tab titles or Codex deep links. Separately, CSS indexes standalone six-digit IDs visible in each page as searchable context metadata.
- Chrome → Codex: one extension shortcut opens the exact paired Codex thread with `gio open`.
- Codex → Chrome: press Codex Desktop's **Copy chat deep link** shortcut; on GNOME Wayland the GNOME companion observes the clipboard change inside the compositor and forwards the `codex://threads/...` value to the daemon, which focuses the exact paired Chrome tab.
- Persists the full floating-note state in SQLite: text, position, size, collapsed/hidden state, and pairings. Canonical page URL is used as a fallback, so refreshing or reopening the same Chrome page restores the existing note and layout instead of creating an empty/default one.
- Shows the note over the active Codex conversation and hides it whenever the active thread cannot be resolved safely, so a stale note is never shown over a different chat.
- Lets the note be edited from either Chrome or Codex. By default both surfaces share one note; enable **Separate Chrome/Codex notes** to keep two independent values for the same pair.
- Restores/focuses the right Chrome tab even with many windows/tabs; if the paired tab is closed, it reopens its URL.
- Provides a searchable Chrome side panel for all contexts; **Alt+Shift+S** opens it with the search field focused. It searches canonical and page-detected `PROMPT_ID` values, Chrome titles, Codex chat titles, and note text. Indexed IDs can also be added or removed manually per context.
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
gnome-extensions enable chrome-codex-switcher-v2@gernalix.github.com
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

### Search dashboard

Press **Alt+Shift+S** globally from GNOME to open the context dashboard, regardless of which application currently has focus. The GNOME companion owns this system-wide shortcut and opens the localhost dashboard in Google Chrome when installed (falling back to the default browser only if Chrome cannot be resolved). The Chrome extension keeps the same shortcut as an in-Chrome fallback.

The extension bundles its GSettings schema inside its own `schemas/` directory, so a plain system-level `gsettings get org.gnome.shell.extensions.chrome-codex-switcher open-search-dashboard` may report `No such schema`. Verify the bundled schema with:

```bash
EXT="$HOME/.local/share/gnome-shell/extensions/chrome-codex-switcher-v2@gernalix.github.com"
GSETTINGS_SCHEMA_DIR="$EXT/schemas" gsettings get org.gnome.shell.extensions.chrome-codex-switcher open-search-dashboard
``` Start typing immediately to filter by note text, canonical/page-detected `PROMPT_ID`, Chrome tab title, or Codex chat title. CSS performs one full-page scan and then incrementally inspects changed DOM subtrees for standalone six-digit IDs; discovered IDs remain searchable history for that context. Use **+ ID** to add an association manually or **×** on a detected/manual chip to remove it. Removing an automatically detected ID creates a persistent exclusion, so later scans do not immediately restore it. Use **↑/↓** to select a result, **Enter** to switch to its Chrome tab, or **Shift+Enter** to open its Codex twin. Mouse users can click a row for Chrome or the explicit **Chrome**/**Codex** buttons. Notes are rendered first and with stronger spacing, contrast and typography because they are the primary search/recognition field.

### Floating note

The note can be edited from both paired surfaces:

- Chrome: editable textarea, draggable/resizable/collapsible/hideable;
- Codex: editable GNOME overlay bound to the currently resolved Codex thread;
- default mode: one shared note, so an edit on either side appears on the other;
- optional **Separate Chrome/Codex notes** checkbox: Chrome and Codex keep independent note text for the same pair;
- persisted per tab-context and searchable from the Chrome side panel.

When Codex changes conversation, the previous active-thread cache is invalidated immediately. The overlay follows an exact AT-SPI thread ID when available; if only a title is available it is accepted only when it maps to one unique learned thread. Otherwise the overlay stays hidden instead of reusing the previous chat's note.

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

- use AT-SPI to resolve the exact selected Codex/ChatGPT thread ID when the UI exposes a thread route/attribute, with a fail-closed unique-title fallback for older builds, and display only the matching paired note;
- provide an editable Codex note plus the **Separate Chrome/Codex notes** toggle;
- bridge copied Codex deep links to the daemon using GNOME Shell's native clipboard APIs.

The clipboard access is deliberate and limited to values matching `codex://threads/...`; unrelated clipboard text is ignored. On GNOME Wayland this companion is the preferred Codex → Chrome backend because GNOME does not expose the wlroots data-control protocol required by `wl-paste --watch`.

## Tests

```bash
PYTHONPATH=host python3 -m unittest discover -s tests -v
python3 -m py_compile host/chrome_codex_switcher/*.py
```

CI also runs real-browser end-to-end persistence tests with Chrome for Testing plus the unpacked extension. They verify the complete floating-note state after both a page refresh and closing/reopening the tab: text, X/Y position, width, height, collapsed state, and hidden state.


## Workflowy roadmap cockpit

The localhost API supports explicit roadmap bindings and desktop-first launch:

- `GET /api/prompt?prompt_id=514458` returns the current Chrome/Codex binding.
- `POST /api/prompt/bind` binds a known Chrome context to that exact PROMPT_ID.
- `POST /api/prompt/arm` marks that exact prompt as the next Codex deep-link association.
- `POST /api/prompt/open-codex` opens the exact bound Codex thread.
- `POST /api/prompt/launch-codex` launches a roadmap prompt directly toward Codex Desktop.

The Chrome extension recognizes Workflowy action links under
`http://127.0.0.1:43817/ui/prompt/<PROMPT_ID>/...`. **🚀 Avvia is Codex-Desktop-first**:
it no longer creates or focuses a ChatGPT Chrome tab and it does not use the clipboard.
The daemon reads the canonical prompt plus `project_id`, `project_name`, `repo`,
`model`, and `reasoning` from the local Workflowy/roadmap bridge, records that exact
request as `pending_desktop_launch`, arms the next native Codex association, emits
`desktop_launch_requested`, and opens `codex://threads/new`.

Chrome pairing remains available only through the explicit Chrome/link actions; it is
not a prerequisite for **Avvia**. The staged desktop-launch record is the contract for
the native AT-SPI launcher tracked by roadmap PROMPT_ID `989559`: that runtime consumer
must select the requested Codex project/model/reasoning, fill the composer without
sending, and correlate the resulting thread to the explicit PROMPT_ID. Until that
consumer is deployed, the daemon still stages the exact request and opens a new Codex
thread, but it does not claim that UI configuration/injection succeeded. If an exact
requested setting is unavailable, the native launcher must fail closed rather than
silently select a fallback.
