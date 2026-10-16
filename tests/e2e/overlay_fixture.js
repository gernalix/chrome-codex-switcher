// Browser-only presentation fixture. Product code runs unchanged; transport
// responses can be varied without touching the user's registry or note store.
window.__chromeCodexSwitcherLoaded = false;
window.fixtureContext = {
  id: 'overlay-fixture', title: 'Fixture', note: 'Chrome text', codex_note: 'Codex text',
  notes_independent: true, twin: null,
  geometry: {x: 30, y: 30, width: 320, height: 160},
  pn_card: {id: 'P250', title: 'Redundant title', status: 'in corso', what: 'Breve descrizione.', links: []}
};
window.fixtureMessages = [];
window.fixtureFailUnlink = false;
window.chrome = {runtime: {
  connect: () => ({onDisconnect: {addListener() {}}, disconnect() {}}),
  onMessage: {addListener(fn) { window.fixtureRefresh = () => fn({type: 'refreshContext'}, {}, () => {}); }},
  sendMessage(message, respond) {
    fixtureMessages.push(message);
    let response = {ok: true};
    if (message.type === 'context:get' || message.type === 'context:read') response.context = structuredClone(fixtureContext);
    if (message.type === 'context:ui' && message.contextId === fixtureContext.id) Object.assign(fixtureContext, message.ui);
    if (message.type === 'context:note' && message.contextId === fixtureContext.id) fixtureContext.note = message.note;
    if (message.type === 'side:unlink') {
      if (fixtureFailUnlink) response = {ok: false, error: 'fixture offline'};
      else fixtureContext.twin = null;
    }
    respond(response);
  }
}};
