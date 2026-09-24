const list = document.querySelector("#list");
const q = document.querySelector("#q");
let contexts = [];
let visibleRows = [];
let selectedIndex = 0;
const send = message => new Promise(resolve => chrome.runtime.sendMessage(message, resolve));

function searchText(item) {
  return [
    item.prompt_id,
    ...(item.prompt_ids || []),
    item.title,
    item.twin?.codex_title,
    item.note,
    item.codex_note,
    item.url,
    item.twin?.codex_thread
  ].filter(Boolean).join(" ").toLowerCase();
}

function noteText(item) {
  return [...new Set([item.note, item.codex_note].filter(Boolean))].join("\n\n");
}

function select(index) {
  if (!visibleRows.length) {
    selectedIndex = 0;
    return;
  }
  selectedIndex = (index + visibleRows.length) % visibleRows.length;
  for (const [i, card] of [...list.querySelectorAll(".card")].entries()) {
    const active = i === selectedIndex;
    card.classList.toggle("selected", active);
    card.setAttribute("aria-selected", active ? "true" : "false");
    if (active) card.scrollIntoView({block: "nearest"});
  }
}

async function openItem(item, target = "chrome") {
  if (!item) return;
  if (target === "codex" && item.twin) {
    await send({type: "side:codex", context: item});
    return;
  }
  await send({type: "side:focus", context: item});
}

async function editPromptId(item, mode, promptId = null) {
  let value = promptId;
  if (mode === "add") {
    value = (window.prompt("PROMPT_ID da associare (6 cifre)") || "").trim();
    if (!value) return;
    if (!/^\d{6}$/.test(value)) {
      window.alert("Il PROMPT_ID deve contenere esattamente 6 cifre.");
      return;
    }
  }
  const result = await send({
    type: mode === "add" ? "side:prompt-add" : "side:prompt-remove",
    contextId: item.id,
    promptId: value
  });
  if (!result?.ok) {
    window.alert("Aggiornamento PROMPT_ID fallito: " + (result?.error || "daemon non disponibile"));
    return;
  }
  await load();
}

function promptRow(item) {
  const row = document.createElement("div");
  row.className = "prompt-ids";
  const canonical = String(item.prompt_id || "");

  if (canonical) {
    const chip = document.createElement("span");
    chip.className = "prompt-chip bound";
    chip.title = "Binding canonico";
    chip.textContent = canonical;
    row.append(chip);
  }

  for (const entry of item.prompt_id_index || []) {
    const promptId = String(entry.prompt_id || "");
    if (!promptId || promptId === canonical) continue;
    const chip = document.createElement("span");
    chip.className = "prompt-chip";
    chip.title = entry.manual_added
      ? (entry.auto_detected ? "Rilevato e aggiunto manualmente" : "Aggiunto manualmente")
      : "Rilevato nella pagina";

    const label = document.createElement("span");
    label.textContent = promptId;

    const remove = document.createElement("button");
    remove.className = "prompt-remove";
    remove.textContent = "×";
    remove.title = "Rimuovi associazione";
    remove.onclick = event => {
      event.stopPropagation();
      editPromptId(item, "remove", promptId);
    };

    chip.append(label, remove);
    row.append(chip);
  }

  const add = document.createElement("button");
  add.className = "prompt-add";
  add.textContent = "+ ID";
  add.title = "Aggiungi PROMPT_ID";
  add.onclick = event => {
    event.stopPropagation();
    editPromptId(item, "add");
  };
  row.append(add);
  return row;
}

function render() {
  const needle = q.value.trim().toLowerCase();
  visibleRows = contexts.filter(item => !needle || searchText(item).includes(needle));
  list.textContent = "";
  if (!visibleRows.length) {
    selectedIndex = 0;
    const e = document.createElement("div");
    e.className = "empty";
    e.textContent = "No matching contexts";
    list.append(e);
    return;
  }

  selectedIndex = Math.min(selectedIndex, visibleRows.length - 1);
  for (const [index, item] of visibleRows.entries()) {
    const card = document.createElement("section");
    card.className = "card";
    card.setAttribute("role", "option");
    card.setAttribute("aria-selected", index === selectedIndex ? "true" : "false");
    if (index === selectedIndex) card.classList.add("selected");
    card.onclick = () => openItem(item, "chrome");

    const noteValue = noteText(item);
    if (noteValue) {
      const note = document.createElement("div");
      note.className = "note";
      note.textContent = noteValue;
      card.append(note);
    }

    const title = document.createElement("div");
    title.className = "title";
    title.textContent = item.title || item.url || "Untitled Chrome context";
    card.append(title, promptRow(item));

    const meta = document.createElement("div");
    meta.className = "meta";
    const metadata = [];
    if (item.twin?.codex_title) metadata.push(`Codex: ${item.twin.codex_title}`);
    else if (item.twin?.codex_thread) metadata.push(`Codex thread: ${item.twin.codex_thread}`);
    else metadata.push("No Codex twin");
    meta.textContent = metadata.join(" · ");
    card.append(meta);

    const buttons = document.createElement("div");
    buttons.className = "buttons";

    const focus = document.createElement("button");
    focus.textContent = "Chrome";
    focus.onclick = event => {
      event.stopPropagation();
      openItem(item, "chrome");
    };
    buttons.append(focus);

    if (item.twin) {
      const codex = document.createElement("button");
      codex.textContent = "Codex";
      codex.onclick = event => {
        event.stopPropagation();
        openItem(item, "codex");
      };
      buttons.append(codex);

      const unlink = document.createElement("button");
      unlink.textContent = "Unlink";
      unlink.onclick = async event => {
        event.stopPropagation();
        await send({type: "side:unlink", contextId: item.id});
        await load();
      };
      buttons.append(unlink);
    }

    card.onmouseenter = () => select(index);
    card.append(buttons);
    list.append(card);
  }
}

async function load() {
  const result = await send({type: "side:list"});
  contexts = result?.contexts || [];
  render();
}

q.addEventListener("input", () => {
  selectedIndex = 0;
  render();
});

q.addEventListener("keydown", event => {
  if (event.key === "ArrowDown") {
    event.preventDefault();
    select(selectedIndex + 1);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    select(selectedIndex - 1);
  } else if (event.key === "Enter") {
    event.preventDefault();
    openItem(visibleRows[selectedIndex] || visibleRows[0], event.shiftKey ? "codex" : "chrome");
  } else if (event.key === "Escape" && q.value) {
    event.preventDefault();
    q.value = "";
    selectedIndex = 0;
    render();
  }
});

q.focus();
load();
setInterval(load, 5000);
