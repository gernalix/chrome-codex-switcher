const BASE = "http://127.0.0.1:43817";
const MAP_KEY = "tabContexts";
let eventLoopRunning = false;
let eventSeq = 0;

function canonicalUrl(raw) {
  try {
    const url = new URL(raw);
    url.hash = "";
    return url.toString();
  } catch {
    return raw || "";
  }
}

async function api(path, options = {}) {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? 5000;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${BASE}${path}`, {
      method: options.method || "GET",
      headers: options.body ? {"Content-Type": "application/json"} : undefined,
      body: options.body ? JSON.stringify(options.body) : undefined,
      signal: controller.signal
    });
    if (!response.ok) throw new Error(`daemon_http_${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

async function readTabMap() {
  return (await chrome.storage.local.get(MAP_KEY))[MAP_KEY] || {};
}

async function writeTabMap(map) {
  const entries = Object.entries(map);
  if (entries.length > 600) {
    entries.sort((a, b) => (b[1].lastSeen || 0) - (a[1].lastSeen || 0));
    map = Object.fromEntries(entries.slice(0, 500));
  }
  await chrome.storage.local.set({[MAP_KEY]: map});
}

async function ensureContext(tab) {
  if (!tab?.id || !tab.url || !/^https?:/.test(tab.url)) return null;
  const url = canonicalUrl(tab.url);
  const map = await readTabMap();
  const direct = map[String(tab.id)];
  let record = direct && direct.url === url ? direct : null;

  if (!record) {
    const openTabs = await chrome.tabs.query({});
    const openIds = new Set(openTabs.map(t => String(t.id)));
    const candidates = Object.entries(map)
      .filter(([tabId, value]) => value.url === url && !openIds.has(tabId))
      .sort((a, b) => (b[1].lastSeen || 0) - (a[1].lastSeen || 0));
    record = candidates.length ? candidates[0][1] : {contextId: crypto.randomUUID(), url};
  }

  record = {...record, url, lastSeen: Date.now()};
  map[String(tab.id)] = record;
  await writeTabMap(map);

  const result = await api("/api/context", {
    method: "POST",
    body: {context_id: record.contextId, url, title: tab.title || ""}
  });
  return result.context;
}

async function currentTab() {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  return tab;
}

async function currentContext() {
  const tab = await currentTab();
  const context = await ensureContext(tab);
  return {tab, context};
}

async function armCurrent() {
  const {tab, context} = await currentContext();
  if (!context) return {ok: false, error: "unsupported_tab"};
  const result = await api("/api/arm-link", {
    method: "POST",
    body: {context_id: context.id, url: canonicalUrl(tab.url), title: tab.title || ""}
  });
  try { await chrome.tabs.sendMessage(tab.id, {type: "linkArmed"}); } catch {}
  return result;
}

async function switchCurrent() {
  const {tab, context} = await currentContext();
  if (!context) return {ok: false, error: "unsupported_tab"};
  const result = await api("/api/switch-from-chrome", {
    method: "POST",
    body: {context_id: context.id, url: canonicalUrl(tab.url), title: tab.title || ""}
  });
  if (!result.ok && result.error === "not_linked") {
    try { await chrome.tabs.sendMessage(tab.id, {type: "notLinked"}); } catch {}
  }
  return result;
}

async function focusContext(payload) {
  const map = await readTabMap();
  let target = null;
  for (const [tabId, value] of Object.entries(map)) {
    if (value.contextId !== payload.context_id) continue;
    try {
      const tab = await chrome.tabs.get(Number(tabId));
      if (tab && canonicalUrl(tab.url || "") === canonicalUrl(payload.url)) { target = tab; break; }
    } catch {}
  }

  if (!target) {
    const tabs = await chrome.tabs.query({});
    const wanted = canonicalUrl(payload.url);
    const matches = tabs.filter(tab => canonicalUrl(tab.url || "") === wanted);
    matches.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
    target = matches[0] || null;
  }

  if (!target) target = await chrome.tabs.create({url: payload.url, active: false});

  map[String(target.id)] = {contextId: payload.context_id, url: canonicalUrl(payload.url), lastSeen: Date.now()};
  await writeTabMap(map);
  await chrome.tabs.update(target.id, {active: true});
  if (target.windowId != null) await chrome.windows.update(target.windowId, {focused: true});
}

async function processEvent(event) {
  if (event.type === "focus_chrome") {
    await focusContext(event.payload);
  } else if (event.type === "linked" || event.type === "unlinked") {
    const tabs = await chrome.tabs.query({});
    const map = await readTabMap();
    for (const tab of tabs) {
      if (map[String(tab.id)]?.contextId === event.payload.context_id) {
        try { await chrome.tabs.sendMessage(tab.id, {type: "refreshContext"}); } catch {}
      }
    }
  }
}

async function runEventLoop() {
  if (eventLoopRunning) return;
  eventLoopRunning = true;
  try {
    const saved = await chrome.storage.local.get("eventSeq");
    eventSeq = Number(saved.eventSeq || 0);
    while (true) {
      try {
        const data = await api(`/api/events?after=${eventSeq}&timeout=20`, {timeoutMs: 24000});
        for (const event of data.events || []) await processEvent(event);
        // The daemon sequence is process-local and can reset after service restart.
        eventSeq = Number(data.seq || 0);
        await chrome.storage.local.set({eventSeq});
      } catch {
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
    }
  } finally {
    eventLoopRunning = false;
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  try { await chrome.sidePanel.setPanelBehavior({openPanelOnActionClick: true}); } catch {}
  chrome.alarms.create("bridge-keepalive", {periodInMinutes: 0.5});
  runEventLoop();
});
chrome.runtime.onStartup.addListener(() => runEventLoop());
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === "bridge-keepalive") runEventLoop(); });

chrome.commands.onCommand.addListener(async command => {
  if (command === "switch-twin") await switchCurrent();
  if (command === "link-twin") await armCurrent();
  if (command === "toggle-note") {
    const tab = await currentTab();
    if (tab?.id) { try { await chrome.tabs.sendMessage(tab.id, {type: "toggleNote"}); } catch {} }
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      if (message.type === "context:get") {
        const tab = sender.tab || await currentTab();
        sendResponse({ok: true, context: await ensureContext(tab)});
      } else if (message.type === "context:note") {
        sendResponse(await api("/api/note", {method: "POST", body: {context_id: message.contextId, note: message.note}}));
      } else if (message.type === "context:ui") {
        sendResponse(await api("/api/ui", {method: "POST", body: {context_id: message.contextId, ...message.ui}}));
      } else if (message.type === "context:arm") {
        sendResponse(await armCurrent());
      } else if (message.type === "context:switch") {
        sendResponse(await switchCurrent());
      } else if (message.type === "side:list") {
        sendResponse(await api("/api/list"));
      } else if (message.type === "side:focus") {
        await focusContext(message.context);
        sendResponse({ok: true});
      } else if (message.type === "side:codex") {
        const result = await api("/api/switch-from-chrome", {method: "POST", body: {
          context_id: message.context.id, url: message.context.url, title: message.context.title || ""
        }});
        sendResponse(result);
      } else if (message.type === "side:unlink") {
        sendResponse(await api("/api/unlink", {method: "POST", body: {context_id: message.contextId}}));
      } else {
        sendResponse({ok: false, error: "unknown_message"});
      }
    } catch (error) {
      sendResponse({ok: false, error: String(error?.message || error)});
    }
  })();
  return true;
});

runEventLoop();
