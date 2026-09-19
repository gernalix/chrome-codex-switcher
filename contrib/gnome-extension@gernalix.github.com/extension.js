import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Soup from 'gi://Soup?version=3.0';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const DAEMON_BASE = 'http://127.0.0.1:43817';
const DAEMON_CLIPBOARD_URL = `${DAEMON_BASE}/api/clipboard`;
const DAEMON_CODEX_ACTIVITY_URL = `${DAEMON_BASE}/api/codex-activity`;

function isCodexWindow(win) {
    if (!win) return false;
    const fields = [];
    for (const method of ['get_wm_class', 'get_wm_class_instance', 'get_title', 'get_gtk_application_id', 'get_sandboxed_app_id']) {
        try { if (typeof win[method] === 'function') fields.push(win[method]() || ''); } catch (_) {}
    }
    return fields.join(' ').toLowerCase().match(/codex|chatgpt/);
}

function isCodexLink(text) {
    return /^codex:\/\/threads\/[^\s/?#]+(?:[^\s]*)?$/i.test((text || '').trim());
}

export default class ChromeCodexSwitcherOverlay extends Extension {
    enable() {
        this._lastFocusRequest = null;
        this._contextId = null;
        this._notesIndependent = false;
        this._noteDirty = false;
        this._applyingState = false;
        this._saveTimer = null;

        this._box = new St.BoxLayout({
            vertical: true,
            style_class: 'context-twin-overlay',
            visible: false,
        });
        this._title = new St.Label({style_class: 'context-twin-title'});
        this._note = new St.Entry({
            style_class: 'context-twin-note',
            can_focus: true,
            track_hover: true,
            hint_text: 'Context note',
        });
        this._noteText = this._note.clutter_text;
        this._noteText.set_single_line_mode(false);
        this._noteText.set_line_wrap(true);
        this._noteText.set_editable(true);
        this._noteText.set_selectable(true);
        this._noteChangedId = this._noteText.connect('text-changed', () => this._onNoteChanged());

        this._modeLabel = new St.Label();
        this._mode = new St.Button({
            style_class: 'context-twin-mode',
            reactive: true,
            can_focus: true,
            child: this._modeLabel,
        });
        this._modeClickedId = this._mode.connect('clicked', () => this._toggleNoteMode());
        this._updateModeLabel();

        this._box.add_child(this._title);
        this._box.add_child(this._note);
        this._box.add_child(this._mode);
        Main.uiGroup.add_child(this._box);

        this._http = new Soup.Session();
        this._clipboard = St.Clipboard.get_default();
        this._selection = global.display.get_selection();
        this._selectionChangedId = this._selection.connect('owner-changed', (_selection, type) => {
            if (type !== Meta.SelectionType.CLIPBOARD) return;
            this._clipboard.get_text(St.ClipboardType.CLIPBOARD, (_clipboard, text) => {
                const value = (text || '').trim();
                if (isCodexLink(value)) this._forwardCodexLink(value);
            });
        });

        // A click in Codex's left navigation can change the thread before the
        // accessibility watcher resolves the new selected chat. Hide the old
        // overlay immediately rather than showing the previous chat's note.
        this._stageEventId = global.stage.connect('captured-event', (_actor, event) => {
            try {
                if (event.type() !== Clutter.EventType.BUTTON_PRESS) return Clutter.EVENT_PROPAGATE;
                const win = global.display.focus_window;
                if (!isCodexWindow(win)) return Clutter.EVENT_PROPAGATE;
                const [x, y] = event.get_coords();
                const rect = win.get_frame_rect();
                const sidebarWidth = Math.min(460, Math.max(250, Math.floor(rect.width * 0.32)));
                const inWindow = x >= rect.x && x <= rect.x + rect.width && y >= rect.y && y <= rect.y + rect.height;
                const inSidebar = inWindow && x <= rect.x + sidebarWidth;
                if (inSidebar) {
                    this._box.hide();
                    this._signalPossibleThreadChange();
                }
            } catch (_) {}
            return Clutter.EVENT_PROPAGATE;
        });

        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
        this._refresh();
    }

    disable() {
        if (this._timer) GLib.source_remove(this._timer);
        this._timer = null;
        if (this._saveTimer) GLib.source_remove(this._saveTimer);
        this._saveTimer = null;
        if (this._selection && this._selectionChangedId) this._selection.disconnect(this._selectionChangedId);
        this._selectionChangedId = null;
        if (this._stageEventId) global.stage.disconnect(this._stageEventId);
        this._stageEventId = null;
        if (this._noteText && this._noteChangedId) this._noteText.disconnect(this._noteChangedId);
        this._noteChangedId = null;
        if (this._mode && this._modeClickedId) this._mode.disconnect(this._modeClickedId);
        this._modeClickedId = null;
        this._selection = null;
        this._clipboard = null;
        this._http = null;
        this._box?.destroy();
        this._box = null;
        this._note = null;
        this._noteText = null;
        this._mode = null;
        this._modeLabel = null;
    }

    _postForm(path, payload, callback = null) {
        try {
            const encoded = Soup.form_encode_hash(payload);
            const message = Soup.Message.new_from_encoded_form('POST', DAEMON_BASE + path, encoded);
            this._http.send_and_read_async(message, GLib.PRIORITY_DEFAULT, null, (session, result) => {
                try {
                    session.send_and_read_finish(result);
                    const status = Number(message.get_status());
                    callback?.(status >= 200 && status < 300);
                } catch (_) {
                    callback?.(false);
                }
            });
        } catch (_) {
            callback?.(false);
        }
    }

    _onNoteChanged() {
        if (this._applyingState || !this._contextId) return;
        this._noteDirty = true;
        if (this._saveTimer) GLib.source_remove(this._saveTimer);
        this._saveTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 280, () => {
            this._saveTimer = null;
            const contextId = this._contextId;
            const note = this._noteText.get_text();
            this._postForm('/api/note', {
                context_id: contextId,
                note,
                surface: 'codex',
            }, ok => {
                if (ok && this._contextId === contextId) this._noteDirty = false;
            });
            return GLib.SOURCE_REMOVE;
        });
    }

    _toggleNoteMode() {
        if (!this._contextId) return;
        const previous = this._notesIndependent;
        const next = !previous;
        const contextId = this._contextId;
        const note = this._noteText.get_text();
        this._notesIndependent = next;
        this._updateModeLabel();
        this._postForm('/api/note-mode', {
            context_id: contextId,
            independent: next ? '1' : '0',
            source: 'codex',
            note,
        }, ok => {
            if (!ok && this._contextId === contextId) {
                this._notesIndependent = previous;
                this._updateModeLabel();
            }
        });
    }

    _updateModeLabel() {
        if (!this._modeLabel) return;
        this._modeLabel.text = (this._notesIndependent ? '☑ ' : '☐ ') + 'Separate Chrome/Codex notes';
    }

    _forwardCodexLink(text) {
        try {
            const url = `${DAEMON_CLIPBOARD_URL}?text=${encodeURIComponent(text)}`;
            const message = Soup.Message.new('POST', url);
            this._http.send_and_read_async(message, GLib.PRIORITY_DEFAULT, null, (session, result) => {
                try {
                    session.send_and_read_finish(result);
                } catch (error) {
                    console.debug(`chrome-codex-switcher: clipboard bridge unavailable: ${error}`);
                }
            });
        } catch (error) {
            console.debug(`chrome-codex-switcher: clipboard bridge failed: ${error}`);
        }
    }

    _signalPossibleThreadChange() {
        try {
            const url = DAEMON_CODEX_ACTIVITY_URL + '?kind=sidebar_pointer';
            const message = Soup.Message.new('POST', url);
            this._http.send_and_read_async(message, GLib.PRIORITY_DEFAULT, null, (session, result) => {
                try {
                    session.send_and_read_finish(result);
                } catch (_) {}
            });
        } catch (_) {}
    }

    _applyOverlayState(state) {
        const changedContext = this._contextId !== state.context_id;
        if (changedContext) {
            this._contextId = state.context_id || null;
            this._noteDirty = false;
        }

        this._title.text = state.title || 'Context Twin';
        this._notesIndependent = !!state.notes_independent;
        this._updateModeLabel();

        if (changedContext || !this._noteDirty) {
            const wanted = String(state.note || '');
            if (this._noteText.get_text() !== wanted) {
                this._applyingState = true;
                this._noteText.set_text(wanted);
                this._applyingState = false;
            }
        }
    }

    _refresh() {
        this._focusRequestedChrome();
        const win = global.display.focus_window;
        if (!isCodexWindow(win)) {
            this._box.hide();
            return;
        }
        const path = GLib.build_filenamev([GLib.get_user_cache_dir(), 'chrome-codex-switcher', 'overlay.json']);
        try {
            const [ok, bytes] = GLib.file_get_contents(path);
            if (!ok) {
                this._box.hide();
                return;
            }
            const state = JSON.parse(new TextDecoder().decode(bytes));
            if (!state.visible || !state.context_id) {
                this._contextId = null;
                this._noteDirty = false;
                this._box.hide();
                return;
            }
            this._applyOverlayState(state);
            const rect = win.get_frame_rect();
            const width = Math.min(430, Math.max(300, Math.floor(rect.width * 0.32)));
            this._box.set_width(width);
            this._box.set_position(
                Math.max(rect.x + 12, rect.x + rect.width - width - 24),
                rect.y + 72,
            );
            this._box.show();
        } catch (_) {
            this._box.hide();
        }
    }

    _focusRequestedChrome() {
        const path = GLib.build_filenamev([GLib.get_user_cache_dir(), 'chrome-codex-switcher', 'focus_request.json']);
        try {
            const [ok, bytes] = GLib.file_get_contents(path);
            if (!ok) return;
            const request = JSON.parse(new TextDecoder().decode(bytes));
            if (request.id === this._lastFocusRequest || Date.now() / 1000 - request.issued_at > 10) return;
            const title = String(request.title || '').toLowerCase();
            if (!title) return;
            const windows = global.get_window_actors().map(actor => actor.meta_window).filter(win => {
                const app = [win.get_sandboxed_app_id?.(), win.get_wm_class?.(), win.get_wm_class_instance?.()]
                    .join(' ').toLowerCase();
                return app.includes('chrome');
            });
            const win = windows.find(win => String(win.get_title() || '').toLowerCase().includes(title))
                || (windows.length === 1 ? windows[0] : null);
            if (!win) return;
            if (global.display.focus_window !== win) Main.activateWindow(win);
            if (global.display.focus_window === win) this._lastFocusRequest = request.id;
        } catch (_) {}
    }
}
