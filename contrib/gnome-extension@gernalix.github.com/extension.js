import GLib from 'gi://GLib';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

function isCodexWindow(win) {
    if (!win) return false;
    const fields = [];
    for (const method of ['get_wm_class', 'get_wm_class_instance', 'get_title', 'get_gtk_application_id', 'get_sandboxed_app_id']) {
        try { if (typeof win[method] === 'function') fields.push(win[method]() || ''); } catch (_) {}
    }
    return fields.join(' ').toLowerCase().match(/codex|chatgpt/);
}

export default class ChromeCodexSwitcherOverlay extends Extension {
    enable() {
        this._box = new St.BoxLayout({vertical: true, style_class: 'context-twin-overlay', visible: false});
        this._title = new St.Label({style_class: 'context-twin-title'});
        this._note = new St.Label({style_class: 'context-twin-note'});
        this._note.clutter_text.line_wrap = true;
        this._box.add_child(this._title);
        this._box.add_child(this._note);
        Main.uiGroup.add_child(this._box);
        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => { this._refresh(); return GLib.SOURCE_CONTINUE; });
        this._refresh();
    }

    disable() {
        if (this._timer) GLib.source_remove(this._timer);
        this._timer = null;
        this._box?.destroy();
        this._box = null;
    }

    _refresh() {
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
}
