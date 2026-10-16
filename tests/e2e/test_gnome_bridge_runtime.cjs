// Execute the GNOME runtime with actor creation forbidden, including the cached-loader entrypoint.
const fs = require('fs');
const assert = require('assert');
const source = fs.readFileSync('contrib/gnome-extension@gernalix.github.com/runtime.js', 'utf8');
let selectionCallback, timerCallback, heartbeat, focusCallback;
const identityWindow = {get_wm_class:()=> 'Codex'};
const stage = {connect:()=>{throw Error('desktop stage overlay handler must not exist')}};
const selection = {connect:(_name, cb)=>{selectionCallback=cb;return 1},disconnect:()=>{}};
global.global = {display:{get_selection:()=>selection,focus_window:identityWindow,connect:(_name,cb)=>{focusCallback=cb;return 2},disconnect:()=>{}},stage};
const GLib = {PRIORITY_DEFAULT:0,SOURCE_CONTINUE:true,timeout_add:(_p,_n,cb)=>{timerCallback=cb;return 1},source_remove:()=>{},build_filenamev:()=>'/missing',get_user_cache_dir:()=>'/tmp',file_get_contents:()=>[false,null]};
const St = {Clipboard:{get_default:()=>({get_text:(_type,cb)=>cb(null,'codex://threads/11111111-1111-4111-8111-111111111111')})},ClipboardType:{CLIPBOARD:1},BoxLayout:class {constructor(){throw Error('overlay actor creation forbidden')}}};
const Soup = {Session:class{send_and_read_async(message,_p,_c,cb){cb({send_and_read_finish:()=>{}},null)}},form_encode_hash:p=>{heartbeat=p;return p},Message:{new_from_encoded_form:()=>({get_status:()=>200}),new:()=>({get_status:()=>200})}};
const Main = {uiGroup:{add_child:()=>{throw Error('desktop overlay forbidden')}},activateWindow:()=>{}};
const factory = new Function('Clutter','GLib','Meta','Shell','Soup','St','Main',source+'\nreturn ChromeCodexSwitcherOverlay;');
const Runtime=factory({},GLib,{SelectionType:{CLIPBOARD:1},WindowType:{NORMAL:0}},{WindowTracker:{get_default:()=>({get_window_app:win=>({get_id:()=> win.appId || 'com.openai.codex'})})}},Soup,St,Main);
const bridge=new Runtime();bridge.enable();assert.equal(heartbeat.source_version,'10');assert.equal(heartbeat.visible,'0');assert.equal(heartbeat.focused_codex,'1');selectionCallback(null,1);timerCallback();
const chromeWindow={get_wm_class:()=> 'google-chrome',get_window_type:()=>0,appId:'google-chrome.desktop'};
const otherWindow={get_wm_class:()=> 'calculator',get_window_type:()=>0,appId:'org.gnome.Calculator.desktop'};
global.display.focus_window=chromeWindow;focusCallback();
global.display.focus_window=null;focusCallback();assert.equal(heartbeat.dashboard_blur_at,undefined);
global.display.focus_window={...otherWindow,get_transient_for:()=>chromeWindow};focusCallback();assert.equal(heartbeat.dashboard_blur_at,undefined);
global.display.focus_window={...otherWindow,get_window_type:()=>1};focusCallback();assert.equal(heartbeat.dashboard_blur_at,undefined);
global.display.focus_window=otherWindow;focusCallback();assert.ok(Number(heartbeat.dashboard_blur_at)>0);
heartbeat={};focusCallback();assert.equal(heartbeat.dashboard_blur_at,undefined);
bridge.disable();console.log('PASS: GNOME cached loader, clipboard/focus bridge, visible=false; overlay actor creation forbidden.');

// Exercise the exact Chrome close handler, including changes between readbacks.
(async () => {
    const background=fs.readFileSync('extension/background.js','utf8');
    const handler=background.slice(background.indexOf('async function closeBlurredDashboard('),background.indexOf('async function processEvent('));
    const run=new Function('chrome','BASE',handler+';return closeBlurredDashboard;');
    const base='http://127.0.0.1:43817';
    const dashboard={id:7,focused:false,tabs:[{id:70,url:base+'/ui/search'}]};
    const regular={id:8,focused:false,tabs:[{id:80,url:'https://example.org'}]};
    let all, last, current, removed;
    const close=run({windows:{getAll:async()=>all,getLastFocused:async()=>last,get:async()=>current,remove:async id=>removed.push(id)}},base);
    async function check({windows=[dashboard,regular],previous=dashboard,readback=previous,age=0,expected=[]}={}) {
        all=windows;last=previous;current=readback;removed=[];
        await close({issued_at:Date.now()-age});assert.deepEqual(removed,expected);
    }
    await check({expected:[7]});
    await check({previous:regular});
    await check({windows:[dashboard,{...regular,focused:true}]});
    await check({previous:{...dashboard,focused:true}});
    await check({previous:{...dashboard,tabs:[...dashboard.tabs,regular.tabs[0]]}});
    await check({previous:{...dashboard,tabs:[{...dashboard.tabs[0],pendingUrl:'https://example.org'}]}});
    await check({readback:regular});
    await check({readback:{...dashboard,focused:true}});
    await check({readback:{...dashboard,tabs:[...dashboard.tabs,regular.tabs[0]]}});
    await check({age:3000});
    console.log('PASS: dashboard window removal; other windows/tabs, refocus, navigation, stale events preserved.');
})().catch(error=>{console.error(error);process.exitCode=1;});
