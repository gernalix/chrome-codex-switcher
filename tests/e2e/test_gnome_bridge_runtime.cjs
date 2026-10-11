// Execute the GNOME runtime with actor creation forbidden, including the cached-loader entrypoint.
const fs = require('fs');
const assert = require('assert');
const source = fs.readFileSync('contrib/gnome-extension@gernalix.github.com/runtime.js', 'utf8');
let selectionCallback, timerCallback, heartbeat;
const identityWindow = {get_wm_class:()=> 'Codex'};
const stage = {connect:()=>{throw Error('desktop stage overlay handler must not exist')}};
const selection = {connect:(_name, cb)=>{selectionCallback=cb;return 1},disconnect:()=>{}};
global.global = {display:{get_selection:()=>selection,focus_window:identityWindow},stage};
const GLib = {PRIORITY_DEFAULT:0,SOURCE_CONTINUE:true,timeout_add:(_p,_n,cb)=>{timerCallback=cb;return 1},source_remove:()=>{},build_filenamev:()=>'/missing',get_user_cache_dir:()=>'/tmp',file_get_contents:()=>[false,null]};
const St = {Clipboard:{get_default:()=>({get_text:(_type,cb)=>cb(null,'codex://threads/11111111-1111-4111-8111-111111111111')})},ClipboardType:{CLIPBOARD:1},BoxLayout:class {constructor(){throw Error('overlay actor creation forbidden')}}};
const Soup = {Session:class{send_and_read_async(message,_p,_c,cb){cb({send_and_read_finish:()=>{}},null)}},form_encode_hash:p=>{heartbeat=p;return p},Message:{new_from_encoded_form:()=>({get_status:()=>200}),new:()=>({get_status:()=>200})}};
const Main = {uiGroup:{add_child:()=>{throw Error('desktop overlay forbidden')}},activateWindow:()=>{}};
const factory = new Function('Clutter','GLib','Meta','Shell','Soup','St','Main',source+'\nreturn ChromeCodexSwitcherOverlay;');
const Runtime=factory({},GLib,{SelectionType:{CLIPBOARD:1}},{WindowTracker:{get_default:()=>({get_window_app:()=>({get_id:()=> 'com.openai.codex'})})}},Soup,St,Main);
const bridge=new Runtime();bridge.enable();assert.equal(heartbeat.source_version,'9');assert.equal(heartbeat.visible,'0');assert.equal(heartbeat.focused_codex,'1');selectionCallback(null,1);timerCallback();bridge.disable();console.log('PASS: GNOME cached loader, clipboard/focus bridge, visible=false; overlay actor creation forbidden.');
