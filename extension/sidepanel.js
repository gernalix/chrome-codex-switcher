const list = document.querySelector("#list");
const q = document.querySelector("#q");
let contexts = [];
const send = message => new Promise(resolve => chrome.runtime.sendMessage(message, resolve));

function text(item) {
  return [item.title, item.note, item.url, item.twin?.codex_thread].filter(Boolean).join(" ").toLowerCase();
}

function render() {
  const needle = q.value.trim().toLowerCase();
  const rows = contexts.filter(item => !needle || text(item).includes(needle));
  list.textContent = "";
  if (!rows.length) { const e=document.createElement("div");e.className="empty";e.textContent="No matching contexts";list.append(e);return; }
  for (const item of rows) {
    const card = document.createElement("section"); card.className = "card";
    const title = document.createElement("div"); title.className="title"; title.textContent=item.title || item.url || "Untitled";
    const note = document.createElement("div"); note.className="note"; note.textContent=item.note || "";
    const meta = document.createElement("div"); meta.className="meta"; meta.textContent=item.twin ? `Codex: ${item.twin.codex_thread}` : "No Codex twin";
    const buttons = document.createElement("div"); buttons.className="buttons";
    const focus = document.createElement("button"); focus.textContent="Chrome"; focus.onclick=()=>send({type:"side:focus",context:item});
    buttons.append(focus);
    if (item.twin) {
      const codex=document.createElement("button");codex.textContent="Codex";codex.onclick=()=>send({type:"side:codex",context:item});buttons.append(codex);
      const unlink=document.createElement("button");unlink.textContent="Unlink";unlink.onclick=async()=>{await send({type:"side:unlink",contextId:item.id});await load();};buttons.append(unlink);
    }
    card.append(title,note,meta,buttons); list.append(card);
  }
}

async function load(){const result=await send({type:"side:list"});contexts=result?.contexts||[];render();}
q.addEventListener("input",render);
load(); setInterval(load,5000);
