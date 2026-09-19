(() => {
  if (window.__chromeCodexSwitcherLoaded) return;
  window.__chromeCodexSwitcherLoaded = true;

  const host = document.createElement("div");
  host.id = "chrome-codex-switcher-host";
  host.style.cssText = "all:initial;position:fixed;z-index:2147483647;left:calc(100vw - 350px);top:84px;width:320px;height:200px;";
  const shadow = host.attachShadow({mode: "open"});
  document.documentElement.appendChild(host);

  shadow.innerHTML = `
    <style>
      *{box-sizing:border-box} .box{width:100%;height:100%;min-width:230px;min-height:105px;resize:both;overflow:auto;background:#17181b;color:#f2f2f2;border:1px solid #4b4d55;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.36);font:13px/1.35 system-ui,sans-serif;display:flex;flex-direction:column}
      .head{display:flex;align-items:center;gap:6px;padding:7px 8px;background:#23252a;cursor:move;user-select:none;border-radius:9px 9px 0 0}.title{font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}.status{font-size:11px;color:#aaa}.btn{appearance:none;border:0;border-radius:6px;background:#33363d;color:#eee;padding:4px 7px;cursor:pointer;font:inherit}.btn:hover{background:#464a53}.icon{padding:3px 6px}.body{display:flex;flex:1;min-height:0;flex-direction:column;padding:7px;gap:7px}.note{width:100%;height:100%;min-height:52px;flex:1;resize:none;border:1px solid #3e4148;border-radius:7px;background:#101114;color:#f4f4f4;padding:7px;font:13px/1.4 system-ui,sans-serif;outline:none}.note:focus{border-color:#777b87}.actions{display:flex;gap:6px;align-items:center}.mode{display:flex;align-items:center;gap:6px;font-size:11px;color:#c6c8ce;user-select:none}.mode input{margin:0}.hint{font-size:11px;color:#aeb1b8;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.collapsed .body{display:none}.collapsed{min-height:38px!important;height:38px!important;resize:none}.flash{position:absolute;left:8px;right:8px;bottom:8px;padding:6px 8px;border-radius:6px;background:#363941;color:#fff;opacity:0;pointer-events:none;transition:opacity .15s}.flash.show{opacity:1}
    </style>
    <div class="box">
      <div class="head">
        <div class="title">Context Twin</div><div class="status"></div>
        <button class="btn icon collapse" title="Collapse">−</button>
        <button class="btn icon close" title="Hide">×</button>
      </div>
      <div class="body">
        <textarea class="note" placeholder="What is this tab for?"></textarea>
        <label class="mode"><input class="independent" type="checkbox"> Separate Chrome/Codex notes</label>
        <div class="actions">
          <button class="btn link">Link Codex</button>
          <button class="btn switch">↔ Codex</button>
          <div class="hint"></div>
        </div>
      </div>
      <div class="flash"></div>
    </div>`;

  const box = shadow.querySelector(".box");
  const title = shadow.querySelector(".title");
  const status = shadow.querySelector(".status");
  const note = shadow.querySelector(".note");
  const independent = shadow.querySelector(".independent");
  const hint = shadow.querySelector(".hint");
  const flashEl = shadow.querySelector(".flash");
  let context = null;
  let noteTimer = null;
  let uiTimer = null;
  let lastUrl = location.href;

  let extensionContextAlive = true;

  const send = message => new Promise(resolve => {
    if (!extensionContextAlive) {
      resolve({ok: false, error: "extension_context_invalidated"});
      return;
    }
    try {
      chrome.runtime.sendMessage(message, response => {
        const lastError = chrome.runtime.lastError;
        if (lastError) {
          const error = String(lastError.message || "runtime_message_failed");
          if (/extension context invalidated/i.test(error)) extensionContextAlive = false;
          resolve({ok: false, error});
          return;
        }
        resolve(response ?? {ok: false, error: "empty_response"});
      });
    } catch (error) {
      const messageText = String(error?.message || error);
      if (/extension context invalidated/i.test(messageText)) extensionContextAlive = false;
      resolve({ok: false, error: messageText});
    }
  });

  function flash(text, ms = 2200) {
    flashEl.textContent = text;
    flashEl.classList.add("show");
    setTimeout(() => flashEl.classList.remove("show"), ms);
  }

  function geometry() {
    const rect = host.getBoundingClientRect();
    return {x: Math.round(rect.left), y: Math.round(rect.top), width: Math.round(rect.width), height: Math.round(rect.height)};
  }

  function saveUi(extra = {}) {
    if (!context) return;
    clearTimeout(uiTimer);
    uiTimer = setTimeout(() => send({type: "context:ui", contextId: context.id, ui: {geometry: geometry(), ...extra}}), 180);
  }

  function applyContext(next) {
    if (!next) return;
    context = next;
    title.textContent = next.title || document.title || "Context Twin";
    if (shadow.activeElement !== note) note.value = next.note || "";
    independent.checked = !!next.notes_independent;
    status.textContent = next.twin ? "linked" : "unlinked";
    hint.textContent = next.twin ? next.twin.codex_thread : "";
    box.classList.toggle("collapsed", !!next.collapsed);
    host.style.display = next.hidden ? "none" : "block";
    const g = next.geometry || {};
    if (Number.isFinite(g.x)) host.style.left = `${Math.max(0, g.x)}px`;
    if (Number.isFinite(g.y)) host.style.top = `${Math.max(0, g.y)}px`;
    if (Number.isFinite(g.width)) host.style.width = `${Math.max(230, g.width)}px`;
    if (Number.isFinite(g.height)) host.style.height = `${Math.max(105, g.height)}px`;
  }

  async function refresh() {
    const result = await send({type: "context:get"});
    if (result?.ok) applyContext(result.context);
  }

  note.addEventListener("input", () => {
    if (!context) return;
    clearTimeout(noteTimer);
    noteTimer = setTimeout(() => send({
      type: "context:note",
      contextId: context.id,
      note: note.value,
      surface: "chrome"
    }), 220);
  });

  independent.addEventListener("change", async () => {
    if (!context) return;
    const result = await send({
      type: "context:note-mode",
      contextId: context.id,
      independent: independent.checked,
      source: "chrome",
      note: note.value
    });
    if (result?.ok && result.context) {
      applyContext(result.context);
    } else {
      independent.checked = !!context.notes_independent;
      flash("Note mode failed: " + (result?.error || "daemon unavailable"));
    }
  });

  shadow.querySelector(".link").addEventListener("click", async () => {
    const result = await send({type: "context:arm"});
    flash(result?.ok ? "Now open the target Codex chat and press Copy chat deep link" : `Link failed: ${result?.error || "daemon unavailable"}`, 3500);
  });

  shadow.querySelector(".switch").addEventListener("click", async () => {
    const result = await send({type: "context:switch"});
    if (!result?.ok) flash(result?.error === "not_linked" ? "This tab has no Codex twin yet" : `Switch failed: ${result?.error || "daemon unavailable"}`);
  });

  shadow.querySelector(".close").addEventListener("click", () => {
    host.style.display = "none";
    saveUi({hidden: true});
  });

  shadow.querySelector(".collapse").addEventListener("click", () => {
    const collapsed = !box.classList.contains("collapsed");
    box.classList.toggle("collapsed", collapsed);
    saveUi({collapsed});
  });

  let drag = null;
  shadow.querySelector(".head").addEventListener("pointerdown", event => {
    if (event.target.closest("button")) return;
    const rect = host.getBoundingClientRect();
    drag = {dx: event.clientX - rect.left, dy: event.clientY - rect.top};
    event.currentTarget.setPointerCapture(event.pointerId);
  });
  shadow.querySelector(".head").addEventListener("pointermove", event => {
    if (!drag) return;
    host.style.left = `${Math.max(0, Math.min(innerWidth - 80, event.clientX - drag.dx))}px`;
    host.style.top = `${Math.max(0, Math.min(innerHeight - 40, event.clientY - drag.dy))}px`;
  });
  shadow.querySelector(".head").addEventListener("pointerup", event => {
    if (!drag) return;
    drag = null;
    try { event.currentTarget.releasePointerCapture(event.pointerId); } catch {}
    saveUi();
  });

  new ResizeObserver(() => { if (context && host.style.display !== "none") saveUi(); }).observe(box);

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === "controlProbe") {
      sendResponse({
        ok: true,
        context_id: context?.id || null,
        note: note.value,
        notes_independent: !!independent.checked,
        visible: host.style.display !== "none",
        title: title.textContent || "",
        url: location.href
      });
      return;
    }
    if (message.type === "refreshContext") refresh();
    if (message.type === "linkArmed") flash("Go to the target Codex chat and press Copy chat deep link", 3500);
    if (message.type === "notLinked") flash("This tab has no Codex twin yet");
    if (message.type === "toggleNote") {
      const hidden = host.style.display === "none";
      host.style.display = hidden ? "block" : "none";
      saveUi({hidden: !hidden});
    }
  });

  async function copyPrompt(promptId) {
    const result = await send({type: "prompt:text", promptId});
    if (!result?.ok || !result.promptText) throw new Error(result?.error || "prompt unavailable");
    await navigator.clipboard.writeText(result.promptText);
  }

  if (location.hostname === "workflowy.com" || location.hostname.endsWith(".workflowy.com")) {
    document.addEventListener("click", async event => {
      const anchor = event.target.closest?.("a");
      if (!anchor?.href) return;
      let url;
      try { url = new URL(anchor.href); } catch { return; }
      if (url.origin !== "http://127.0.0.1:43817") return;
      const match = url.pathname.match(/^\/ui\/prompt\/(\d{6})\/(copy|launch|chrome|codex)$/);
      if (!match) return;
      event.preventDefault();
      event.stopPropagation();
      const [, promptId, action] = match;
      try {
        if (action === "copy") {
          await copyPrompt(promptId);
        } else if (action === "launch") {
          await copyPrompt(promptId);
          const result = await send({type: "prompt:launch", promptId});
          if (!result?.ok) throw new Error(result?.error || "Prompt launch failed");
        } else if (action === "chrome") {
          const result = await send({type: "prompt:focus", promptId});
          if (!result?.ok) throw new Error(result?.error || "Chrome tab unavailable");
        } else if (action === "codex") {
          const result = await send({type: "prompt:codex", promptId});
          if (!result?.ok) throw new Error(result?.error || "Codex chat unavailable");
        }
      } catch (error) {
        flash(`Prompt action failed: ${String(error?.message || error)}`, 3500);
      }
    }, true);
  }

  const urlWatchTimer = setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      refresh().catch(() => {});
    }
  }, 750);

  window.addEventListener("pagehide", () => {
    clearInterval(urlWatchTimer);
    clearTimeout(noteTimer);
    clearTimeout(uiTimer);
  }, {once: true});

  refresh().catch(() => {
    status.textContent = "daemon offline";
    flash("Start chrome-codex-switcher.service", 3500);
  });
})();
