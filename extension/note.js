const form = document.querySelector("form");
const note = document.querySelector("#note");
const save = form.querySelector("button");
const status = form.querySelector("[role=status]");
const send = message => chrome.runtime.sendMessage(message);
let saved = "";

async function load() {
  const contextId = decodeURIComponent(location.hash.slice(1));
  if (!contextId) throw new Error("missing_context");
  const result = await send({type: "context:read", contextId});
  if (!result?.ok || result.context?.id !== contextId) throw new Error("missing_context");
  document.querySelector("#context").textContent = result.context.url;
  note.value = saved = result.context.note || "";
  note.disabled = false;
  save.disabled = false;
  window.addEventListener("beforeunload", event => {
    if (note.value === saved) return;
    event.preventDefault();
    event.returnValue = "";
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!event.isTrusted || save.disabled) return;
    const value = note.value;
    save.disabled = true;
    try {
      const result = await send({type: "context:note", contextId, note: value, surface: "chrome"});
      if (!result?.ok) throw new Error("save_failed");
      saved = value;
      status.textContent = "Salvata";
    } catch {
      status.textContent = "Salvataggio fallito. Riprova.";
    } finally {
      save.disabled = false;
    }
  });
}

load().catch(() => { status.textContent = "Nota non disponibile."; });
