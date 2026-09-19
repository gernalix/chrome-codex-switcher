import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Soup from 'gi://Soup?version=3.0';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const DAEMON_CLIPBOARD_URL = 'http://127.0.0.1:43817/api/clipboard';

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
        this._box = new St.BoxLayout({vertical: true, style_class: 'context-twin-overlay', visible: false});
        this._title = new St.Label({style_class: 'context-twin-title'});
        this._note = new St.Label({style_class: 'context-twin-note'});
        this._note.clutter_text.line_wrap = true;
        this._box.add_child(this._title);
        this._box.add_child(this._note);
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

        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
        this._refresh();
    }

    disable() {
        if (this._timer) GLib.source_remove(this._timer);
        this._timer = null;
        if (this._selection && this._selectionChangedId) this._selection.disconnect(this._selectionChangedId);
        this._selectionChangedId = null;
        this._selection = null;
        this._clipboard = null;
        this._http = null;
        this._box?.destroy();
        this._box = null;
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

    _refresh() {
        this._focusRequestedChrome();
        const win = global.display.focus_window;
        if (!isCodexWindow(win)) { this._box.hide(); return; }
        const path = GLib.build_filenamev([GLib.get_user_cache_dir(), 'chrome-codex-switcher', 'overlay.json']);
        try {
            const [ok, bytes] = GLib.file_get_contents(path);
            if (!ok) { this._box.hide(); return; }
            const state = JSON.parse(new TextDecoder().decode(bytes));
            if (!state.visible) { this._box.hide(); return; }
            this._title.text = state.title || 'Context Twin';
            this._note.text = state.note || '(no note)';
            const rect = win.get_frame_rect();
            const width = Math.min(390, Math.max(280, Math.floor(rect.width * 0.30)));
            this._box.set_width(width);
            this._box.set_position(Math.max(rect.x + 12, rect.x + rect.width - width - 24), rect.y + 72);
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
            const actor = global.get_window_actors().find(actor => {
                const win = actor.meta_window;
                const app = String(win.get_sandboxed_app_id?.() || win.get_wm_class?.() || '').toLowerCase();
                return app.includes('chrome') && String(win.get_title() || '').toLowerCase().includes(title);
            });
            if (!actor) return;
            actor.meta_window.activate(global.get_current_time());
            this._lastFocusRequest = request.id;
        } catch (_) {}
    }
}
