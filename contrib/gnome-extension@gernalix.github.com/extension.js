import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import Soup from 'gi://Soup?version=3.0';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

function loadRuntime(extensionPath) {
    const path = GLib.build_filenamev([extensionPath, 'runtime.js']);
    const [ok, bytes] = GLib.file_get_contents(path);
    if (!ok) throw new Error(`Unable to read ${path}`);
    const source = new TextDecoder().decode(bytes);
    const factory = new Function(
        'Clutter', 'GLib', 'Meta', 'Shell', 'Soup', 'St', 'Main',
        `${source}\nreturn ChromeCodexSwitcherOverlay;`,
    );
    return factory(Clutter, GLib, Meta, Shell, Soup, St, Main);
}

export default class ChromeCodexSwitcherLoader extends Extension {
    enable() {
        const Runtime = loadRuntime(this.path);
        this._runtime = new Runtime();
        this._runtime.enable();
    }

    disable() {
        this._runtime?.disable();
        this._runtime = null;
    }
}
