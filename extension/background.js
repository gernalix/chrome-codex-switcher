const BASE = "http://127.0.0.1:43817";
const MAP_KEY = "tabContexts";
const PENDING_PROMPT_CAPTURE_KEY = "pendingPromptChromeCapture";
const PROMPT_CAPTURE_TTL_MS = 5 * 60 * 1000;
let eventLoopRunning = false;
let eventSeq = 0;
let promptCaptureBusy = false;

function canonicalUrl(raw) {
  try {
    const url = new URL(raw);
    url.hash = "";
    return url.toString();
  } catch {
    return raw || "";
  }
}

function isPromptChatTab(tab) {
  const raw = tab?.url || tab?.pendingUrl || "";
  try {
    const url = new URL(raw);
    return url.origin === "https://chatgpt.com" && /^\/(?:c|g)\//.test(url.pathname);
  } catch {
    return false;
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

async function heartbeat() {
  try {
    await api("/api/extension-heartbeat", {
      method: "POST",
      body: {version: chrome.runtime.getManifest().version}
    });
  } catch {}
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

async function ensureContext(tab, fallbackUrl = "") {
  const rawUrl = [tab?.url, tab?.pendingUrl, fallbackUrl].find(value => /^https?:/.test(value || ""));
  if (!tab?.id || !rawUrl) return null;
  const url = canonicalUrl(rawUrl);
  const map = await readTabMap();
  const direct = map[String(tab.id)];
  const openedChat = direct?.url === "https://chatgpt.com/" && /^https:\/\/chatgpt\.com\/(?:c\/|g\/)/.test(url);
  let record = direct && (direct.url === url || openedChat) ? direct : null;

  if (!record) {
    const openTabs = await chrome.tabs.query({});
    const openIds = new Set(openTabs.map(t => String(t.id)));
    const candidates = Object.entries(map)
      .filter(([tabId, value]) => value.url === url && !openIds.has(tabId))
      .sort((a, b) => (b[1].lastSeen || 0) - (a[1].lastSeen || 0));
    record = candidates.length ? candidates[0][1] : {contextId: crypto.randomUUID(), url};
  }

  record = {...record, url, lastSeen: Date.now()};

  const result = await api("/api/context", {
    method: "POST",
    body: {context_id: record.contextId, url, title: tab.title || ""}
  });

  if (result?.context?.id && result.context.id !== record.contextId) {
    record = {...record, contextId: result.context.id};
  }
  map[String(tab.id)] = record;
  await writeTabMap(map);
  return result.context;
}

async function currentTab() {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  return tab;
}

async function promptText(promptId) {
  const data = await api(`/api/prompt/text?prompt_id=${encodeURIComponent(promptId)}`);
  if (!data?.ok || !data?.prompt_text) throw new Error(data?.error || "prompt_text_missing");
  return data.prompt_text;
}

async function promptBinding(promptId) {
  return await api(`/api/prompt?prompt_id=${encodeURIComponent(promptId)}`);
}

async function bindPrompt(promptId, tab, context) {
  return await api("/api/prompt/bind", {
    method: "POST",
    body: {
      prompt_id: promptId,
      context_id: context.id,
      url: context.url,
      title: tab.title || ""
    }
  });
}

async function armPrompt(promptId, tab, context) {
  return await api("/api/prompt/arm", {
    method: "POST",
    body: {
      prompt_id: promptId,
      context_id: context?.id || null,
      url: context?.url || canonicalUrl(tab?.url || tab?.pendingUrl || ""),
      title: tab?.title || ""
    }
  });
}

async function clearPendingPromptCapture() {
  await chrome.storage.local.remove(PENDING_PROMPT_CAPTURE_KEY);
}

async function readPendingPromptCapture() {
  const stored = await chrome.storage.local.get(PENDING_PROMPT_CAPTURE_KEY);
  const pending = stored[PENDING_PROMPT_CAPTURE_KEY];
  if (!pending) return null;
  if (Number(pending.expiresAt || 0) <= Date.now()) {
    await clearPendingPromptCapture();
    return null;
  }
  return pending;
}

async function listPromptBindings() {
  const result = await api("/api/prompts");
  return Array.isArray(result?.bindings) ? result.bindings : [];
}

async function promptChromeCandidates(promptId, {excludeContextId = null} = {}) {
  const [tabs, map, bindings] = await Promise.all([
    chrome.tabs.query({}),
    readTabMap(),
    listPromptBindings()
  ]);
  const owners = new Map(
    bindings
      .filter(item => item?.context_id)
      .map(item => [String(item.context_id), String(item.prompt_id || "")])
  );
  return tabs
    .filter(isPromptChatTab)
    .filter(tab => {
      const contextId = map[String(tab.id)]?.contextId || null;
      if (excludeContextId && contextId === excludeContextId) return false;
      const owner = contextId ? owners.get(String(contextId)) : null;
      return !owner || owner === String(promptId);
    })
    .sort((a, b) => Number(b.lastAccessed || 0) - Number(a.lastAccessed || 0));
}

async function recoverPromptCodex(promptId, {force = false} = {}) {
  return await api("/api/prompt/recover-codex", {
    method: "POST",
    body: {prompt_id: promptId, force}
  });
}

async function beginPromptCodexBind(promptId, {force = false} = {}) {
  await clearPendingPromptCapture();
  const result = await recoverPromptCodex(promptId, {force});
  if (!result?.ok) return result || {ok: false, error: "prompt_codex_recovery_failed"};
  return result;
}

async function finishPromptChromeBind(promptId, tab) {
  const context = await ensureContext(tab);
  if (!context) throw new Error("context_creation_failed");

  const bindings = await listPromptBindings();
  const owner = bindings.find(item => item?.context_id === context.id);
  if (owner && String(owner.prompt_id || "") !== String(promptId)) {
    throw new Error("prompt_context_conflict");
  }

  const bound = await bindPrompt(promptId, tab, context);
  if (!bound?.ok || bound.binding?.context_id !== context.id) {
    throw new Error(bound?.error || "prompt_bind_failed");
  }

  if (bound.binding?.codex_thread && bound.binding?.codex_deep_link) {
    return {ok: true, stage: "complete", binding: bound.binding, context_id: context.id};
  }

  const codex = await beginPromptCodexBind(promptId);
  if (!codex?.ok) throw new Error(codex?.error || "prompt_codex_recovery_failed");
  return {
    ok: true,
    stage: codex.stage === "chrome" ? "complete" : codex.stage,
    source: codex.source,
    binding: codex.binding || bound.binding,
    context_id: context.id
  };
}

async function beginPromptChromeBind(promptId, sourceTab, {force = false} = {}) {
  const known = await promptBinding(promptId);
  const binding = known?.binding || {};
  const hasChrome = !!(binding.context_id && binding.url);

  if (hasChrome && !force) {
    if (binding.codex_thread && binding.codex_deep_link) {
      return {ok: true, stage: "complete", binding};
    }
    return await beginPromptCodexBind(promptId);
  }

  if (isPromptChatTab(sourceTab)) {
    return await finishPromptChromeBind(promptId, sourceTab);
  }

  const candidates = await promptChromeCandidates(promptId, {
    excludeContextId: force ? binding.context_id || null : null
  });
  if (candidates.length === 1) {
    return await finishPromptChromeBind(promptId, candidates[0]);
  }

  const pending = {
    promptId,
    force,
    armedAt: Date.now(),
    expiresAt: Date.now() + PROMPT_CAPTURE_TTL_MS,
    candidateCount: candidates.length
  };
  await chrome.storage.local.set({[PENDING_PROMPT_CAPTURE_KEY]: pending});
  return {
    ok: true,
    stage: "chrome",
    mode: candidates.length ? "choose_tab" : "wait_for_tab",
    candidate_count: candidates.length,
    pending,
    binding
  };
}

async function beginPromptLateBind(promptId, sourceTab = null) {
  const known = await promptBinding(promptId);
  const binding = known?.binding || {};
  const hasChrome = !!(binding.context_id && binding.url);
  const hasCodex = !!(binding.codex_thread && binding.codex_deep_link);

  if (hasChrome && hasCodex) {
    await clearPendingPromptCapture();
    return {ok: true, stage: "complete", binding};
  }
  if (!hasChrome) {
    return await beginPromptChromeBind(promptId, sourceTab, {force: false});
  }
  return await beginPromptCodexBind(promptId, {force: false});
}

async function maybeCapturePromptChromeTab(tab) {
  if (promptCaptureBusy || !isPromptChatTab(tab)) return null;
  const pending = await readPendingPromptCapture();
  if (!pending) return null;

  promptCaptureBusy = true;
  try {
    const promptId = String(pending.promptId || "");
    const result = await finishPromptChromeBind(promptId, tab);
    await clearPendingPromptCapture();
    try {
      await chrome.tabs.sendMessage(tab.id, {
        type: "promptLateBindStatus",
        promptId,
        stage: result.stage,
        source: result.source || null
      });
    } catch {}
    return {ok: true, promptId, ...result};
  } catch (error) {
    await clearPendingPromptCapture();
    try {
      await chrome.tabs.sendMessage(tab.id, {
        type: "promptLateBindStatus",
        stage: "error",
        error: String(error?.message || error)
      });
    } catch {}
    return {ok: false, error: String(error?.message || error)};
  } finally {
    promptCaptureBusy = false;
  }
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

async function focusContext(payload, preferredWindowId = null) {
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

  if (!target) {
    target = await chrome.tabs.create({
      url: payload.url,
      active: false,
      ...(preferredWindowId != null ? {windowId: preferredWindowId} : {})
    });
  } else if (preferredWindowId != null && target.windowId !== preferredWindowId) {
    try {
      target = await chrome.tabs.move(target.id, {windowId: preferredWindowId, index: -1});
    } catch {}
  }

  map[String(target.id)] = {contextId: payload.context_id, url: canonicalUrl(payload.url), lastSeen: Date.now()};
  await writeTabMap(map);
  await chrome.tabs.update(target.id, {active: true});
  if (target.windowId != null) await chrome.windows.update(target.windowId, {focused: true});
}


async function focusPrompt(promptId, sourceTab, {create = true, arm = false} = {}) {
  const known = await promptBinding(promptId);
  if (known?.ok && known.binding?.context_id && known.binding?.url) {
    if (arm && !known.binding?.codex_deep_link) {
      const armed = await api("/api/prompt/arm", {
        method: "POST",
        body: {
          prompt_id: promptId,
          context_id: known.binding.context_id,
          url: known.binding.url,
          title: known.binding.title || ""
        }
      });
      if (!armed?.ok || armed.pending?.context_id !== known.binding.context_id) {
        return {ok: false, error: "prompt_arm_failed"};
      }
    }
    await focusContext({
      context_id: known.binding.context_id,
      url: known.binding.url,
      title: known.binding.title || ""
    }, sourceTab?.windowId ?? null);
    return {ok: true, existing: true};
  }
  if (!create) return {ok: false, error: "chrome_not_linked"};
  const tab = await chrome.tabs.create({
    url: "https://chatgpt.com/",
    active: true,
    ...(sourceTab?.windowId != null ? {windowId: sourceTab.windowId} : {})
  });
  const context = await ensureContext(tab, "https://chatgpt.com/");
  if (!context) return {ok: false, error: "context_creation_failed"};
  const bound = await bindPrompt(promptId, tab, context);
  if (!bound?.ok || bound.binding?.context_id !== context.id) return {ok: false, error: "prompt_bind_failed"};
  if (arm) {
    const armed = await armPrompt(promptId, tab, context);
    if (!armed?.ok || armed.pending?.context_id !== context.id) return {ok: false, error: "prompt_arm_failed"};
  }
  return {ok: true, existing: false, context};
}

async function openPromptCodex(promptId) {
  return await api("/api/prompt/open-codex", {
    method: "POST",
    body: {prompt_id: promptId}
  });
}

async function launchPrompt(promptId, _sourceTab) {
  // Roadmap prompts are coding tasks: launch Codex Desktop directly. Chrome
  // remains available only through the explicit Chrome/link actions.
  const codexSide = await api("/api/prompt/launch-codex", {
    method: "POST",
    body: {prompt_id: promptId}
  });
  if (!codexSide?.ok) {
    return {ok: false, error: codexSide?.error || "codex_launch_failed", codex: codexSide};
  }
  return {ok: true, codex: codexSide};
}

async function findContextTab(payload) {
  const map = await readTabMap();
  for (const [tabId, value] of Object.entries(map)) {
    if (value.contextId !== payload.context_id) continue;
    try {
      const tab = await chrome.tabs.get(Number(tabId));
      const current = canonicalUrl(tab?.url || tab?.pendingUrl || "");
      const saved = canonicalUrl(value.url || payload.url || "");
      const openedChat = saved === "https://chatgpt.com/" && /^https:\/\/chatgpt\.com\/(?:c\/|g\/)/.test(current);
      if (tab && (current === saved || openedChat)) return tab;
    } catch {}
  }
  return null;
}

async function controlChrome(payload) {
  const action = payload.action || "probe";
  if (action === "workflowy") {
    const tabs = await chrome.tabs.query({url: ["https://workflowy.com/*", "https://*.workflowy.com/*"]});
    for (const tab of tabs) {
      try {
        const observed = await chrome.tabs.sendMessage(tab.id, {type: "controlWorkflowy", promptId: payload.prompt_id});
        if (observed?.action_present) return {ok: true, ...observed};
      } catch {}
    }
    return {ok: false, error: "workflowy_action_not_rendered", prompt_id: payload.prompt_id, action_present: false};
  }
  let binding = await promptBinding(payload.prompt_id);

  if (action === "ensure" && (!binding?.ok || !binding.binding?.context_id)) {
    const ensured = await focusPrompt(payload.prompt_id, await currentTab(), {create: true, arm: false});
    if (!ensured?.ok) return {ok: false, error: ensured?.error || "ensure_context_failed"};
    binding = await promptBinding(payload.prompt_id);
  }

  if (!binding?.ok || !binding.binding?.context_id || !binding.binding?.url) {
    return {ok: false, error: "chrome_context_missing"};
  }

  const contextPayload = {
    context_id: binding.binding.context_id,
    url: binding.binding.url,
    title: binding.binding.title || ""
  };
  if (action === "focus" || action === "ensure") {
    await focusContext(contextPayload);
  }

  const tab = await findContextTab(contextPayload);
  if (!tab) return {ok: false, error: "chrome_tab_not_found", context_id: contextPayload.context_id};

  let rendered = null;
  let lastError = null;
  const attempts = action === "ensure" ? 20 : 3;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      rendered = await chrome.tabs.sendMessage(tab.id, {type: "controlProbe"});
      if (rendered?.ok) break;
    } catch (error) {
      lastError = error;
    }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  if (!rendered?.ok) {
    return {
      ok: false,
      error: "chrome_content_unreachable",
      detail: String(lastError?.message || lastError || "no_probe_response"),
      context_id: contextPayload.context_id,
      tab_id: tab.id
    };
  }

  const [active] = await chrome.tabs.query({active: true, windowId: tab.windowId});
  return {
    ok: !!rendered?.ok,
    context_id: contextPayload.context_id,
    tab_id: tab.id,
    window_id: tab.windowId,
    url: canonicalUrl(tab.url || tab.pendingUrl || ""),
    active: active?.id === tab.id,
    rendered
  };
}

async function processEvent(event) {
  if (event.type === "focus_chrome") {
    await focusContext(event.payload);
  } else if (event.type === "control_chrome") {
    let result;
    try {
      result = await controlChrome(event.payload);
    } catch (error) {
      result = {ok: false, error: String(error?.message || error)};
    }
    try {
      await api("/api/control-ack", {
        method: "POST",
        body: {
          request_id: event.payload.request_id,
          surface: "chrome",
          prompt_id: event.payload.prompt_id,
          action: event.payload.action,
          ...result
        }
      });
    } catch {}
  } else if (["linked", "unlinked", "note_changed", "note_mode_changed"].includes(event.type)) {
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
  await heartbeat();
  try {
    const saved = await chrome.storage.local.get("eventSeq");
    eventSeq = Number(saved.eventSeq || 0);
    while (true) {
      try {
        const data = await api(`/api/events?after=${eventSeq}&timeout=20`, {timeoutMs: 24000});
        // A daemon restart preserves this worker but loses its in-memory
        // runtime observation. Renew the heartbeat once the bridge reconnects.
        await heartbeat();
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
  heartbeat();
runEventLoop();
});
chrome.runtime.onStartup.addListener(() => runEventLoop());
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name !== "bridge-keepalive") return;
  // Alarms wake an MV3 worker even while a previous long-poll is reconnecting.
  heartbeat();
  runEventLoop();
});

chrome.tabs.onActivated.addListener(async ({tabId}) => {
  try {
    const tab = await chrome.tabs.get(tabId);
    await maybeCapturePromptChromeTab(tab);
  } catch {}
});

chrome.tabs.onUpdated.addListener(async (_tabId, changeInfo, tab) => {
  if (!changeInfo.url && changeInfo.status !== "complete") return;
  try { await maybeCapturePromptChromeTab(tab); } catch {}
});

chrome.commands.onCommand.addListener(async command => {
  if (command === "switch-twin") await switchCurrent();
  if (command === "link-twin") await armCurrent();
  if (command === "open-search-dashboard") {
    const tab = await currentTab();
    if (tab?.windowId != null) {
      try { await chrome.sidePanel.open({windowId: tab.windowId}); } catch {}
    }
  }
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
        sendResponse(await api("/api/note", {method: "POST", body: {
          context_id: message.contextId,
          note: message.note,
          surface: message.surface || "chrome"
        }}));
      } else if (message.type === "context:note-mode") {
        sendResponse(await api("/api/note-mode", {method: "POST", body: {
          context_id: message.contextId,
          independent: !!message.independent,
          source: message.source || "chrome",
          note: message.note
        }}));
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
      } else if (message.type === "prompt:text") {
        sendResponse({ok: true, promptText: await promptText(message.promptId)});
      } else if (message.type === "prompt:launch") {
        sendResponse(await launchPrompt(message.promptId, sender.tab || await currentTab()));
      } else if (message.type === "prompt:bind-late") {
        sendResponse(await beginPromptLateBind(message.promptId, sender.tab || await currentTab()));
      } else if (message.type === "prompt:bind-chrome") {
        sendResponse(await beginPromptChromeBind(
          message.promptId,
          sender.tab || await currentTab(),
          {force: true}
        ));
      } else if (message.type === "prompt:bind-codex") {
        sendResponse(await beginPromptCodexBind(message.promptId, {force: true}));
      } else if (message.type === "prompt:focus") {
        sendResponse(await focusPrompt(message.promptId, sender.tab || await currentTab(), {create: false, arm: false}));
      } else if (message.type === "prompt:codex") {
        sendResponse(await openPromptCodex(message.promptId));
      } else if (message.type === "prompt:verify") {
        sendResponse(await api(`/api/verify/prompt/${encodeURIComponent(message.promptId)}`, {timeoutMs: 90000}));
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
