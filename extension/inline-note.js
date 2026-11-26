(() => {
  const note = document.querySelector("#note");
  const status = document.querySelector("#status");
  const token = location.hash.slice(1);
  let saved = "";
  let revision = 0;
  let dirty = false;
  let retry = null;
  let pending = 0;
  const send = (type, fields = {}) => chrome.runtime.sendMessage({type, token, ...fields});

  async function load() {
    const before = revision;
    try {
      const result = await send("inline:load");
      if (!result?.ok) throw new Error("scope_changed");
      if (before !== revision || dirty || (!note.disabled && document.activeElement === note)) return;
      note.value = saved = result.note || "";
      note.disabled = false;
      status.textContent = "";
    } catch {
      if (note.disabled) status.textContent = "Nota non disponibile";
    }
  }

  // Submit every real input immediately. The background serializes writes,
  // including those already accepted when this frame is closed or navigates.
  async function save() {
    clearTimeout(retry);
    const version = revision;
    const value = note.value;
    pending++;
    try {
      const result = await send("inline:save", {note:value});
      if (!result?.ok) throw new Error("save_failed");
      if (version === revision) {
        saved = value;
        dirty = false;
        status.textContent = "";
      }
    } catch {
      if (version === revision) {
        dirty = true;
        status.textContent = "Non salvata · nuovo tentativo";
        retry = setTimeout(save, 1500);
      }
    } finally {
      pending--;
    }
  }
  note.addEventListener("input", event => {
    if (!event.isTrusted || note.disabled) return;
    revision++;
    dirty = true;
    save();
  });
  note.addEventListener("blur", () => { if (!dirty) load(); });
  window.addEventListener("message", event => {
    if (event.source === parent && event.data?.type === "ccs:refresh-note" && !dirty) load();
  });
  window.addEventListener("pagehide", () => {
    if (dirty && !pending) save();
    clearTimeout(retry);
  });
  if (window.parent !== window && token) load();
})();
