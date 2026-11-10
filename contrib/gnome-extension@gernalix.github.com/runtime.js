const DAEMON_BASE = 'http://127.0.0.1:43817';
const SOURCE_VERSION = 10;
const CODEX_DESKTOP_APP_IDS = new Set([
    'chatgpt.desktop',
    'com.openai.chatgpt.desktop',
    'com.openai.chatgpt',
    'com.openai.codex.desktop',
    'com.openai.codex',
    'codex.desktop',
]);
const CODEX_DESKTOP_WM_CLASSES = new Set(['chatgpt', 'codex']);

function desktopWindowIdentity(win) {
    const result = {appId: '', wmClass: '', gtkApplicationId: '', sandboxedAppId: '', matches: false};
    if (!win) return result;
    const read = method => {
        try {
            return typeof win[method] === 'function' ? String(win[method]() || '').trim().toLowerCase() : '';
        } catch (_) {
            return '';
        }
    };
    result.wmClass = read('get_wm_class');
    result.gtkApplicationId = read('get_gtk_application_id');
    result.sandboxedAppId = read('get_sandboxed_app_id');
    try {
        const app = Shell.WindowTracker.get_default().get_window_app(win);
        result.appId = String(app?.get_id?.() || '').trim().toLowerCase();
    } catch (_) {}
    result.matches = CODEX_DESKTOP_WM_CLASSES.has(result.wmClass)
        || CODEX_DESKTOP_APP_IDS.has(result.appId)
        || CODEX_DESKTOP_APP_IDS.has(result.gtkApplicationId)
        || CODEX_DESKTOP_APP_IDS.has(result.sandboxedAppId);
    return result;
}

function isChatGptDesktopWindow(win) {
    return desktopWindowIdentity(win).matches;
}

function isCodexLink(text) {
    return /^codex:\/\/threads\/[^\s/?#]+(?:[^\s]*)?$/i.test((text || '').trim());
}

class ChromeCodexSwitcherBridge {
    enable() {
        this._lastRuntimeSignature = null;
        this._lastRuntimeReportAt = 0;
        this._runtimeReportInFlight = false;
        this._runtimeReportFailures = 0;
        this._runtimeReportBackoffUntil = 0;
        this._http = new Soup.Session();
        this._clipboard = St.Clipboard.get_default();
        this._selection = global.display.get_selection();
        this._selectionChangedId = this._selection.connect('owner-changed', (_selection, type) => {
            if (type !== Meta.SelectionType.CLIPBOARD) return;
            const sourceWindow = global.display.focus_window;
            if (!isChatGptDesktopWindow(sourceWindow)) return;
            this._clipboard.get_text(St.ClipboardType.CLIPBOARD, (_clipboard, text) => {
                if (global.display.focus_window !== sourceWindow || !isChatGptDesktopWindow(sourceWindow)) return;
                const value = (text || '').trim();
                if (isCodexLink(value)) this._forwardCodexLink(value);
            });
        });
        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
        this._lastNormalWindow = global.display.focus_window;
        this._focusChangedId = global.display.connect('notify::focus-window', () => this._onFocusChanged());
        this._refresh();
    }

    disable() {
        if (this._focusChangedId) global.display.disconnect(this._focusChangedId);
        this._focusChangedId = null;
        this._lastNormalWindow = null;
        if (this._timer) GLib.source_remove(this._timer);
        this._timer = null;
        if (this._selection && this._selectionChangedId) this._selection.disconnect(this._selectionChangedId);
        this._selectionChangedId = null;
        this._selection = null;
        this._clipboard = null;
        this._http = null;
    }

    _onFocusChanged() {
        const win = global.display.focus_window;
        // Null focus, menus and transient dialogs are not an application switch.
        if (!win || win.get_transient_for?.() || win.get_window_type?.() !== Meta.WindowType.NORMAL) return;
        const previous = desktopWindowIdentity(this._lastNormalWindow);
        const current = desktopWindowIdentity(win);
        this._lastNormalWindow = win;
        const isChrome = identity => ['google-chrome', 'google-chrome-stable'].includes(identity.wmClass);
        if (!isChrome(previous) || isChrome(current) || !current.appId) return;
        this._postForm('/api/gnome-heartbeat', {
            source_version: String(SOURCE_VERSION),
            focused_app_id: current.appId,
            focused_wm_class: current.wmClass,
            dashboard_blur_at: String(Date.now()),
        });
    }

    _postForm(path, payload, callback = null) {
        try {
            const encoded = Soup.form_encode_hash(payload);
            const message = Soup.Message.new_from_encoded_form('POST', DAEMON_BASE + path, encoded);
            message.get_request_headers().append('X-CCS-Request', '1');
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

    _forwardCodexLink(text) {
        this._postForm('/api/clipboard', {text});
    }

    _reportRuntime(state, focusedCodex, visible, identity = {}) {
        const payload = {
            source_version: String(SOURCE_VERSION),
            focused_codex: focusedCodex ? '1' : '0',
            focused_app_id: String(identity.appId || ''),
            focused_wm_class: String(identity.wmClass || ''),
            visible: visible ? '1' : '0',
            context_id: String(state?.context_id || ''),
            codex_thread: String(state?.codex_thread || ''),
            note: '',
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
        const identity = desktopWindowIdentity(global.display.focus_window);
        this._reportRuntime(null, identity.matches, false, identity);
    }


}

// Previously loaded GNOME loaders return this symbol until the next session.
// Keep their entrypoint compatible; the implementation creates no UI actors.
const ChromeCodexSwitcherOverlay = ChromeCodexSwitcherBridge;
