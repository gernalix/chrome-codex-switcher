import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import Soup from 'gi://Soup?version=3.0';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const DAEMON_BASE = 'http://127.0.0.1:43817';
const DAEMON_CLIPBOARD_URL = `${DAEMON_BASE}/api/clipboard`;
const DAEMON_CODEX_ACTIVITY_URL = `${DAEMON_BASE}/api/codex-activity`;

function isChatGptDesktopWindow(win) {
    if (!win) return false;

    // Never use the window title as app identity. Titles can legitimately
    // contain words such as "codex" (for example an Obsidian vault name),
    // which previously made the Shell overlay leak over unrelated apps.
    const fields = [];
    for (const method of ['get_wm_class', 'get_wm_class_instance', 'get_gtk_application_id', 'get_sandboxed_app_id']) {
        try {
            if (typeof win[method] === 'function') fields.push(String(win[method]() || ''));
        } catch (_) {}
    }
    try {
        const app = Shell.WindowTracker.get_default().get_window_app(win);
        if (app) {
            fields.push(String(app.get_id?.() || ''));
            fields.push(String(app.get_name?.() || ''));
        }
    } catch (_) {}

    const identity = fields.join(' ').toLowerCase();
    return /(^|[.\s_-])(chatgpt|codex)([.\s_-]|$)/.test(identity);
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
        this._dismissedContextId = null;
        this._collapsedContextIds = new Set();
        this._expandedHeights = new Map();
        this._geometryByContext = new Map();
        this._interaction = null;
        this._applyingState = false;
        this._saveTimer = null;
        this._lastRuntimeSignature = null;
        this._lastRuntimeReportAt = 0;
        this._runtimeReportInFlight = false;
        this._runtimeReportFailures = 0;
        this._runtimeReportBackoffUntil = 0;

        this._box = new St.BoxLayout({
            vertical: true,
            style_class: 'context-twin-overlay',
            visible: false,
            reactive: true,
            track_hover: true,
            width: 360,
            height: 260,
        });

        this._header = new St.BoxLayout({
            style_class: 'context-twin-header',
            x_expand: true,
        });
        this._title = new St.Label({
            style_class: 'context-twin-title',
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
            reactive: true,
            track_hover: true,
        });
        this._collapse = new St.Button({
            style_class: 'context-twin-control',
            reactive: true,
            can_focus: true,
            child: new St.Label({text: '−'}),
        });
        this._close = new St.Button({
            style_class: 'context-twin-control',
            reactive: true,
            can_focus: true,
            child: new St.Label({text: '×'}),
        });
        this._header.add_child(this._title);
        this._header.add_child(this._collapse);
        this._header.add_child(this._close);

        this._note = new St.Entry({
            style_class: 'context-twin-note',
            can_focus: true,
            track_hover: true,
            hint_text: 'Context note',
            x_expand: true,
            y_expand: true,
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

        this._resize = new St.Label({
            text: '↘',
            style_class: 'context-twin-resize',
            reactive: true,
            track_hover: true,
            x_align: Clutter.ActorAlign.END,
        });

        this._titlePressId = this._title.connect(
            'button-press-event',
            (_actor, event) => this._beginOverlayInteraction('move', event),
        );
        this._resizePressId = this._resize.connect(
            'button-press-event',
            (_actor, event) => this._beginOverlayInteraction('resize', event),
        );
        this._collapseClickedId = this._collapse.connect('clicked', () => this._toggleCollapsed());
        this._closeClickedId = this._close.connect('clicked', () => this._dismissOverlay());

        this._box.add_child(this._header);
        this._box.add_child(this._note);
        this._box.add_child(this._mode);
        this._box.add_child(this._resize);
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
                if (this._interaction && this._handleOverlayInteraction(event)) {
                    return Clutter.EVENT_STOP;
                }
                if (event.type() !== Clutter.EventType.BUTTON_PRESS) return Clutter.EVENT_PROPAGATE;
                const win = global.display.focus_window;
                if (!isChatGptDesktopWindow(win)) return Clutter.EVENT_PROPAGATE;
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
            try {
                this._refresh();
            } catch (error) {
                console.error(`Chrome Codex Switcher refresh failed: ${error}`);
                this._box?.hide();
            }
            return GLib.SOURCE_CONTINUE;
        });
        try {
            this._refresh();
        } catch (error) {
            console.error(`Chrome Codex Switcher initial refresh failed: ${error}`);
            this._box?.hide();
        }
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
        if (this._title && this._titlePressId) this._title.disconnect(this._titlePressId);
        this._titlePressId = null;
        if (this._resize && this._resizePressId) this._resize.disconnect(this._resizePressId);
        this._resizePressId = null;
        if (this._collapse && this._collapseClickedId) this._collapse.disconnect(this._collapseClickedId);
        this._collapseClickedId = null;
        if (this._close && this._closeClickedId) this._close.disconnect(this._closeClickedId);
        this._closeClickedId = null;
        this._selection = null;
        this._clipboard = null;
        this._http = null;
        this._runtimeReportInFlight = false;
        this._runtimeReportFailures = 0;
        this._runtimeReportBackoffUntil = 0;
        this._box?.destroy();
        this._box = null;
        this._note = null;
        this._noteText = null;
        this._mode = null;
        this._modeLabel = null;
        this._header = null;
        this._collapse = null;
        this._close = null;
        this._resize = null;
        this._geometryByContext = null;
        this._expandedHeights = null;
        this._collapsedContextIds = null;
        this._interaction = null;
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

    _beginOverlayInteraction(mode, event) {
        if (!this._contextId || !this._box?.visible) return Clutter.EVENT_PROPAGATE;
        try {
            if (typeof event.get_button === 'function' && event.get_button() !== 1) {
                return Clutter.EVENT_PROPAGATE;
            }
            const [pointerX, pointerY] = event.get_coords();
            this._interaction = {
                mode,
                pointerX,
                pointerY,
                x: this._box.get_x(),
                y: this._box.get_y(),
                width: this._box.get_width(),
                height: this._box.get_height(),
            };
            return Clutter.EVENT_STOP;
        } catch (_) {
            this._interaction = null;
            return Clutter.EVENT_PROPAGATE;
        }
    }

    _handleOverlayInteraction(event) {
        if (!this._interaction) return false;
        const type = event.type();
        if (type === Clutter.EventType.BUTTON_RELEASE) {
            this._rememberOverlayGeometry();
            this._interaction = null;
            return true;
        }
        if (type !== Clutter.EventType.MOTION) return false;

        const win = global.display.focus_window;
        if (!isChatGptDesktopWindow(win)) {
            this._interaction = null;
            return false;
        }

        const rect = win.get_frame_rect();
        const margin = 8;
        const [pointerX, pointerY] = event.get_coords();
        const dx = pointerX - this._interaction.pointerX;
        const dy = pointerY - this._interaction.pointerY;

        if (this._interaction.mode === 'move') {
            const width = this._box.get_width();
            const height = this._box.get_height();
            const x = Math.max(
                rect.x + margin,
                Math.min(rect.x + rect.width - width - margin, this._interaction.x + dx),
            );
            const y = Math.max(
                rect.y + margin,
                Math.min(rect.y + rect.height - height - margin, this._interaction.y + dy),
            );
            this._box.set_position(Math.round(x), Math.round(y));
        } else {
            const maxWidth = Math.max(300, rect.x + rect.width - this._interaction.x - margin);
            const maxHeight = Math.max(180, rect.y + rect.height - this._interaction.y - margin);
            const width = Math.max(300, Math.min(maxWidth, this._interaction.width + dx));
            const height = Math.max(180, Math.min(maxHeight, this._interaction.height + dy));
            this._box.set_size(Math.round(width), Math.round(height));
        }
        return true;
    }

    _rememberOverlayGeometry() {
        if (!this._contextId || !this._box) return;
        const collapsed = this._collapsedContextIds?.has(this._contextId);
        const previous = this._geometryByContext?.get(this._contextId) || {};
        const expandedHeight = this._expandedHeights?.get(this._contextId);
        this._geometryByContext?.set(this._contextId, {
            x: this._box.get_x(),
            y: this._box.get_y(),
            width: this._box.get_width(),
            height: collapsed
                ? Number(expandedHeight || previous.height || 260)
                : this._box.get_height(),
        });
    }

    _toggleCollapsed() {
        if (!this._contextId) return;
        const collapsed = this._collapsedContextIds.has(this._contextId);
        if (collapsed) {
            this._collapsedContextIds.delete(this._contextId);
            this._note.show();
            this._mode.show();
            this._resize.show();
            const height = Math.max(180, Number(this._expandedHeights.get(this._contextId) || 260));
            this._box.set_height(height);
        } else {
            this._rememberOverlayGeometry();
            this._expandedHeights.set(this._contextId, Math.max(180, this._box.get_height()));
            this._collapsedContextIds.add(this._contextId);
            this._note.hide();
            this._mode.hide();
            this._resize.hide();
            this._box.set_height(48);
        }
        this._updateCollapsedControl();
        this._rememberOverlayGeometry();
    }

    _updateCollapsedControl() {
        if (!this._collapse || !this._contextId) return;
        const collapsed = this._collapsedContextIds.has(this._contextId);
        const label = this._collapse.get_child();
        if (label) label.text = collapsed ? '+' : '−';
    }

    _dismissOverlay() {
        if (!this._contextId) return;
        this._dismissedContextId = this._contextId;
        this._interaction = null;
        this._box.hide();
    }

    _placeOverlay(win) {
        if (!this._contextId || !win || this._interaction) return;
        const rect = win.get_frame_rect();
        const margin = 12;
        const collapsed = this._collapsedContextIds.has(this._contextId);
        const saved = this._geometryByContext.get(this._contextId) || {};

        let width = Number(saved.width);
        if (!Number.isFinite(width)) width = Math.min(430, Math.max(320, Math.floor(rect.width * 0.32)));
        width = Math.max(300, Math.min(width, Math.max(300, rect.width - margin * 2)));

        let height = Number(saved.height);
        if (!Number.isFinite(height)) height = 260;
        height = Math.max(180, Math.min(height, Math.max(180, rect.height - 96)));

        let x = Number(saved.x);
        if (!Number.isFinite(x)) x = rect.x + rect.width - width - 24;
        x = Math.max(rect.x + margin, Math.min(x, rect.x + rect.width - width - margin));

        const renderedHeight = collapsed ? 48 : height;
        let y = Number(saved.y);
        if (!Number.isFinite(y)) y = rect.y + 72;
        y = Math.max(rect.y + margin, Math.min(y, rect.y + rect.height - renderedHeight - margin));

        this._box.set_size(Math.round(width), Math.round(renderedHeight));
        this._box.set_position(Math.round(x), Math.round(y));
        if (collapsed) {
            this._note.hide();
            this._mode.hide();
            this._resize.hide();
        } else {
            this._note.show();
            this._mode.show();
            this._resize.show();
        }
        this._updateCollapsedControl();
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
            this._dismissedContextId = null;
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

    _reportRuntime(state, focusedCodex, visible) {
        const payload = {
            focused_codex: focusedCodex ? '1' : '0',
            visible: visible ? '1' : '0',
            context_id: String(state?.context_id || ''),
            codex_thread: String(state?.codex_thread || ''),
            note: visible ? String(this._noteText?.get_text?.() || '') : String(state?.note || ''),
            notes_independent: state?.notes_independent ? '1' : '0',
        };
        const signature = JSON.stringify(payload);
        const nowMs = Date.now();
        if (this._runtimeReportInFlight || nowMs < this._runtimeReportBackoffUntil) return;
        if (signature === this._lastRuntimeSignature && nowMs - this._lastRuntimeReportAt < 1500) return;
        this._lastRuntimeSignature = signature;
        this._lastRuntimeReportAt = nowMs;
        this._runtimeReportInFlight = true;
        this._postForm('/api/gnome-heartbeat', payload, ok => {
            this._runtimeReportInFlight = false;
            if (ok) {
                this._runtimeReportFailures = 0;
                this._runtimeReportBackoffUntil = 0;
                return;
            }
            this._runtimeReportFailures = Math.min(6, this._runtimeReportFailures + 1);
            this._runtimeReportBackoffUntil = Date.now() + Math.min(
                30000,
                500 * (2 ** this._runtimeReportFailures),
            );
        });
    }

    _refresh() {
        this._focusRequestedChrome();
        const win = global.display.focus_window;
        if (!isChatGptDesktopWindow(win)) {
            this._dismissedContextId = null;
            this._interaction = null;
            this._box.hide();
            this._reportRuntime(null, false, false);
            return;
        }
        const path = GLib.build_filenamev([GLib.get_user_cache_dir(), 'chrome-codex-switcher', 'overlay.json']);
        try {
            const [ok, bytes] = GLib.file_get_contents(path);
            if (!ok) {
                this._box.hide();
                this._reportRuntime(null, true, false);
                return;
            }
            const state = JSON.parse(new TextDecoder().decode(bytes));
            if (!state.visible || !state.context_id) {
                this._contextId = null;
                this._noteDirty = false;
                this._box.hide();
                this._reportRuntime(state, true, false);
                return;
            }
            this._applyOverlayState(state);
            if (this._dismissedContextId === this._contextId) {
                this._box.hide();
                this._reportRuntime(state, true, false);
                return;
            }
            this._placeOverlay(win);
            this._box.show();
            this._reportRuntime(state, true, true);
        } catch (_) {
            this._box.hide();
            this._reportRuntime(null, true, false);
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
