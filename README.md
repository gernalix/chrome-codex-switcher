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
- Ships a GNOME Shell companion for the native Wayland clipboard and Chrome focus bridge. Desktop chat overlays are removed.

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

## MegaVault Pn link wake-up

When the first line of a Chrome floating note is `Pn` (for example `P6`),
MegaVault reads that CCS context's ChatGPT URL and its paired Codex deep link
from CCS SQLite. CCS never writes into MegaVault. Once a note/pairing changes,
the daemon coalesces related events for one second and writes a tiny
`~/.local/state/chrome-codex-switcher/pn-links-changed` wake-up marker.
MegaVault's user systemd path unit watches only that marker, not the busy
SQLite WAL; its five-minute timer reconciles missed wake-ups.

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

1. In Chrome Y, press **Alt+Shift+K** or click **❓** in the floating note.
2. Switch to Codex X.
3. In Codex Desktop press **Copy chat deep link** (`Ctrl+Alt+L` on the current Linux build).
4. The daemon stores the pair.

### Chrome → Codex

While Chrome Y is active, press **Alt+Shift+G** or click **🔗**.

### Codex → Chrome

While Codex X is active, press Codex's **Copy chat deep link** shortcut. When auto-switch is enabled (default), the active clipboard backend resolves X and asks the extension to focus Chrome Y.

On GNOME Wayland this does **not** depend on `wl-paste --watch`: the GNOME Shell companion listens to the compositor's clipboard ownership change signal and reads the clipboard through GNOME Shell itself.

Copying a Codex deep link intentionally acts as “switch to twin” outside pairing mode. This can be disabled from the API/CLI later if desired.

### Search dashboard

Press **Alt+Shift+S** globally from GNOME to open the context dashboard, regardless of which application currently has focus. The GNOME Settings Daemon custom binding owns this system-wide shortcut and opens the localhost dashboard in Google Chrome. The Chrome extension keeps the same shortcut as an in-Chrome fallback. When you switch from the standalone dashboard to another application, the GNOME companion and Chrome extension close that dashboard window. Menus and temporary dialogs leave it open. Windows with other tabs and the in-Chrome side panel are preserved.

The extension bundles its GSettings schema inside its own `schemas/` directory, so a plain system-level `gsettings get org.gnome.shell.extensions.chrome-codex-switcher open-search-dashboard` may report `No such schema`. Verify the bundled schema with:

```bash
EXT="$HOME/.local/share/gnome-shell/extensions/chrome-codex-switcher-v2@gernalix.github.com"
GSETTINGS_SCHEMA_DIR="$EXT/schemas" gsettings get org.gnome.shell.extensions.chrome-codex-switcher open-search-dashboard
```

Start typing immediately to filter by note text, canonical/page-detected `PROMPT_ID`, Chrome tab title, or Codex chat title. CSS performs one full-page scan and then incrementally inspects changed DOM subtrees for standalone six-digit IDs; discovered IDs remain searchable history for that context. Use **+ ID** to add an association manually or **×** on a detected/manual chip to remove it. Removing an automatically detected ID creates a persistent exclusion, so later scans do not immediately restore it. Use **↑/↓** to select a result, **Enter** to switch to its Chrome tab, or **Shift+Enter** to open its Codex twin. Mouse users can click a row for Chrome or the explicit **Chrome**/**Codex** buttons. Notes are rendered first and with stronger spacing, contrast and typography because they are the primary search/recognition field.

### Floating note

The Chrome floating note remains editable, draggable, resizable and hideable. Notes persist per tab-context and remain searchable. Existing shared/independent Chrome and Codex note data is preserved; no floating note is rendered on Codex or ChatGPT Desktop. The overlay contains only the editable note and a blank drag bar with right-aligned buttons: **❓ ❌** when unlinked, or **🔗 🗑 ❌** when linked. **❓** starts pairing, **🔗** opens the paired chat, **🗑** unlinks it, and **❌** hides the overlay. Unlink preserves both note texts and the existing separate-note mode. Each icon has a descriptive tooltip and keyboard-accessible label. Stored collapse preferences are ignored without deleting them; notes always remain editable when shown. The bar has no title, status, thread ID, grip, minimize button or footer, and the note has no placeholder.

### Pn prompt cards

A Chrome note whose **first line begins** with `[Pp][1-9][0-9]*` followed by whitespace, a separator or end of line becomes a prompt card, for example `p58 titolo`, `p58: titolo` or `P58`. Recognition uses canonical `P58` lookup/cache keys and never changes the original note text. `Piano`, `P05`, IDs on later lines and legacy six-digit IDs remain ordinary notes. The original text stays editable and is never normalized automatically.

The dashboard and side panel distinguish Pn cards with a colored border, ID, title, “Cosa fa”, a Codex status chip and expandable importance/details. Filter **Tutte / Pn / Normali**, filter by Codex status, search metadata and sort by update, numeric Pn ID or status. BLOCKED/FAIL receive visual emphasis without notifications. Existing keyboard navigation, Chrome/Codex buttons and global Alt+Shift+S are preserved.

MegaVault is read through its canonical `prompt show` verification (including body SHA256), with on-demand change detection and a verified projection cached in the existing CCS meta table. It is never written by this presentation. All associated canonical chat links are shown; chat UUIDs and Pn IDs stay distinct. No synthetic MegaVault URL is generated. Offline metadata is labeled obsolete; source and verification time are visible. Administrative registration (`registered`, `launched`, `cancelled`) is shown separately and **never establishes Codex PASS or any execution status**. Without a verified execution feed the status is explicitly **sconosciuto**.

Use **Integra dati manualmente** to persist a plain-language summary, practical benefit, status, blocker and next step in local CCS meta, attributed to the local user with a timestamp. ChatGPT authors “Cosa fa” from the complete prompt when registering it in MegaVault; CCS only reads the verified canonical description and caches it for offline use. Titles and notes never generate descriptions. Missing historical descriptions require upstream completion and display a separate registration message, never “Cosa fa: non disponibile”. Other unknown details remain **non disponibile**; status older than 24 hours is labeled obsolete. Manual fields never update MegaVault. Multiple contexts for the same Pn share these annotations. Pn descriptions, cards and supplementary details remain available in the dashboard and side panel. They are not rendered in the floating overlay.

Canonical link synchronization still requires a note first line **exactly** `Pn`. Lowercase `p58` is recognized for display but is not sync-ready. An inline title does not change this contract: **Copia nota compatibile** previews/copies `Pn\nTitle\n…` for user review, preserving the stored note. Paste/save it explicitly to opt into synchronization. The existing debounced `pn-links-changed` marker remains unchanged.

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

`contrib/gnome-extension@gernalix.github.com/` supports GNOME Shell 49/50. It bridges copied Codex links and Chrome focus requests; it creates no overlay actors and reads no desktop note cache. Alt+Shift+S is owned by the existing GNOME Settings Daemon custom binding configured by the installer. The obsolete companion is disabled rather than restored on installation fallback.

The clipboard access is deliberate and limited to values matching `codex://threads/...`; unrelated clipboard text is ignored. On GNOME Wayland this companion is the preferred Codex → Chrome backend because GNOME does not expose the wlroots data-control protocol required by `wl-paste --watch`.

## Tests

```bash
PYTHONPATH=host python3 -m unittest discover -s tests -v
python3 -m py_compile host/chrome_codex_switcher/*.py
```

CI also runs real-browser end-to-end persistence tests with Chrome for Testing plus the unpacked extension. They also check the minimal overlay, exact button ordering, keyboard activation, blank-header dragging, small viewports, legacy collapse preferences and unlink preservation of separate notes. They verify the complete floating-note state after both a page refresh and closing/reopening the tab: text, X/Y position, width, height and hidden state.


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

Pn presentation tests: `PYTHONPATH=host python3 -m unittest discover -s tests -p 'test_pn_cards.py' -v`. GNOME actor-free runtime test: `node tests/e2e/test_gnome_bridge_runtime.cjs`. These supplement the existing note persistence E2E and regression suite. No CCS schema migration is required.

## Project release number

Root `NV.txt` is the release source, independent of the extension compatibility
version in its manifest. The installer copies it alongside the deployed host.
The search dashboard displays the derived NV; a missing or invalid source shows
`NV unavailable`. Normal commits retain NV. Explicit releases increment once and
reconcile the integrated Git SHA through MegaVault's existing version registry.

Local browser tests can use `CCS_E2E_PORT=43827` to isolate their temporary daemon and extension from a running user bridge. Set `CCS_E2E_HEADLESS=1` for tests without windows. For headed tests on Wayland, use Xvfb; the test browser explicitly uses X11 to avoid opening windows on the native compositor. The GNOME runtime test also covers application-focus filtering and scoped dashboard window removal; real GNOME Wayland shortcut/focus acceptance remains a separate desktop check.

## Monitor elaborazioni Web e Desktop

La pagina locale `http://127.0.0.1:43817/ui/activity` (link **Elaborazioni**
nella dashboard e nel pannello dell'estensione) mostra in alto le chat
effettivamente osservate in elaborazione, poi quelle terminate negli ultimi
30 minuti. I pulsanti **Apri chat** riutilizzano i comandi CCS esistenti:
focus della scheda Chrome o deep link esatto `codex://threads/<UUID>`.
Aggiornamento ogni 4 secondi; dati e stati non vengono copiati in MegaVault.

- **Web:** il content script rileva il pulsante di interruzione generazione
  nelle schede ChatGPT aperte, senza leggere il testo della conversazione.
  Richiede l'estensione attiva e un tab non sospeso dal browser. Se una scheda
  sparisce/non risponde, lo stato diventa non osservabile, *mai* 'completata'.
  L'interfaccia di chatgpt.com può cambiare: verificare dopo i suoi aggiornamenti.
- **Desktop:** la bridge legge il DB indice Codex in sola lettura e la sequenza
  di `task_started/task_complete/turn_aborted` nei JSONL di
  `~/.codex/sessions`. Lo stato non è la classificazione PASS/FAIL del task.
  Dopo un'ora senza aggiornamenti una run non è più dichiarata attiva.
- **Telegram:** una notifica per fine/interruzione osservata, con titolo e link;
  per Desktop, messaggi identici della stessa chat vengono soppressi anche
  fra turni automatici e dopo il riavvio del servizio. Un titolo o uno stato
  diverso consentono una nuova notifica. Nessuna notifica per ripristino storico,
  pagina inizialmente inattiva, chiusura scheda o disconnessione.
  Il servizio user systemd usa
  `LoadCredential=telegram.env` tramite la credenziale utente già esistente.
  In sua assenza la dashboard continua a funzionare, ma la notifica non parte.

Non è possibile conoscere in modo affidabile lo stato delle chat **Web**
chiuse/non montate usando la sola estensione, né garantire che lo stesso schema
di interfaccia ChatGPT Web resti invariato nel tempo.


The activity dashboard distinguishes the last Telegram send accepted by the
provider from rejected, unavailable and uncertain outcomes. It preserves only
the last receipt (status, time, source/chat link and Telegram message ID) in the
existing private CCS store; message text and credentials are excluded. Acceptance
confirms the API result, not that a particular device displayed the notification.
Ambiguous timeouts are not automatically retried, to avoid duplicate sends.
