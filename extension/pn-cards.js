/* Shared presentation for the dashboard, side panel and Chrome note. */
globalThis.PnCardsUI = (() => {
  const states = ['sconosciuto','non avviato','in corso','BLOCKED','FAIL','PASS','annullato'];
  const el = (tag, text, cls) => {const node=document.createElement(tag); if(text!=null)node.textContent=text; if(cls)node.className=cls; return node;};
  function date(value) {if(!value)return 'non disponibile'; const d=new Date(typeof value==='number'?value*1000:value); if(!Number.isFinite(d.getTime()))return 'non disponibile'; const seconds=Math.max(0,(Date.now()-d)/1000);const relative=seconds<60?'ora':seconds<3600?Math.floor(seconds/60)+' min fa':seconds<86400?Math.floor(seconds/3600)+' ore fa':Math.floor(seconds/86400)+' giorni fa';return d.toLocaleString('it-IT')+' · '+relative;}
  function toolbar(parent, render) {
    const bar=el('div',null,'pn-filters');const kind=el('select');kind.ariaLabel='Tipo scheda';
    for(const [value,label] of [['all','Tutte'],['pn','Pn'],['normal','Normali']]){const o=el('option',label);o.value=value;kind.append(o);}
    const state=el('select');state.ariaLabel='Stato Codex';let o=el('option','Tutti gli stati');o.value='';state.append(o);for(const label of states){o=el('option',label);o.value=label;state.append(o);}
    const sort=el('select');sort.ariaLabel='Ordina schede';for(const [value,label] of [['updated','Ultimo aggiornamento'],['id','ID Pn'],['status','Stato Codex']]){o=el('option',label);o.value=value;sort.append(o);}
    for(const control of [kind,state,sort])control.addEventListener('change',render);bar.append(kind,state,sort);parent.append(bar);return {kind,state,sort};
  }
  function rows(contexts, needle, controls, search) {
    return contexts.filter(item=>(controls.kind.value==='all'||(controls.kind.value==='pn')===!!item.pn_card)&&(!controls.state.value||item.pn_card?.status===controls.state.value)&&(!needle||[search(item),JSON.stringify(item.pn_card||{})].join(' ').toLowerCase().includes(needle))).sort((a,b)=>{const pa=a.pn_card,pb=b.pn_card;if(controls.sort.value==='id')return (pa?Number(pa.id.slice(1)):Infinity)-(pb?Number(pb.id.slice(1)):Infinity);if(controls.sort.value==='status'){const rank=p=>p?['BLOCKED','FAIL','in corso','non avviato','sconosciuto','PASS','annullato'].indexOf(p.status):99;return rank(pa)-rank(pb);}return (pb?.updated_at||b.updated_at||0)-(pa?.updated_at||a.updated_at||0);});
  }
  function details(p) {
    const d=el('details',null,'pn-details');d.onclick=e=>e.stopPropagation();d.append(el('summary','Importanza e dettagli'));
    for(const [label,value] of [['Fonte della descrizione',p.what_source],['Autore della descrizione',p.description_metadata?.source||'nessuno'],['Descrizione registrata',date(p.description_metadata?.created_at)],['Perché è importante',p.why],['Progetto',p.project+(p.project_id?' (#'+p.project_id+')':'')],['Stato Codex',p.status+' · '+p.status_source+' · '+date(p.status_at)+(p.status_stale?' · dato obsoleto':'')],['Ultimo aggiornamento',date(p.updated_at)],['Metadati',p.metadata_source+' · verificati '+date(p.verified_at)+(p.metadata_stale?' · cache obsoleta / offline':'')],['Registrazione amministrativa',p.registry_status+' · '+date(p.registry_updated_at)],['Blocker',p.blocker],['Prossimo passo',p.next_step]])d.append(el('p',label+': '+value));
    if(p.manual?.updated_at)d.append(el('p','Integrazioni manuali locali · '+date(p.manual.updated_at)));
    const links=el('div',null,'pn-links');for(const link of p.links||[]){if(!/^(https:\/\/chatgpt\.com\/|codex:\/\/threads\/)/.test(link.url))continue;const a=el('a',link.label);a.href=link.url;a.target='_blank';a.rel='noopener noreferrer';links.append(a);}d.append(links);
    if(!p.sync_ready){d.append(el('p','Collegamenti canonici: la prima riga deve contenere solo '+p.id+'. La nota originale è conservata.'));const copy=el('button','Copia nota compatibile');copy.type='button';copy.onclick=async()=>{try{await navigator.clipboard.writeText(p.sync_note);copy.textContent='Copiata: titolo alla seconda riga';}catch(_){copy.textContent='Copia dal testo seguente';const preview=el('textarea',p.sync_note);preview.readOnly=true;d.append(preview);}};d.append(copy);}
    return d;
  }
  function card(card, item, saveManual, saveNote) {
    const p=item.pn_card;if(!p)return false;card.classList.add('pn-card');if(['BLOCKED','FAIL'].includes(p.status))card.classList.add('pn-alert');
    const head=el('div',null,'pn-heading');head.append(el('strong',p.id,'pn-id'),el('span',p.title,'pn-title'),el('span',p.status+(p.status_stale?' · obsoleto':''),'pn-state'));card.append(head,el('p',p.what?'Cosa fa: '+p.what:'Descrizione di '+p.id+' da completare nella registrazione MegaVault.','pn-what'),el('div',p.status_source+' · '+date(p.status_at),'pn-source'));
    const d=details(p);card.append(d);
    const form=el('form',null,'pn-manual');form.onclick=e=>e.stopPropagation();
    for(const [key,label] of [['title','Titolo'],['what','Cosa fa (1–2 frasi)'],['why','Perché è importante'],['status','Stato Codex manuale'],['blocker','Blocker'],['next_step','Prossimo passo']]){const l=el('label',label);const input=el(key==='status'?'select':'textarea');input.name=key;input.maxLength=2000;if(key==='status'){const o=el('option','Nessuna dichiarazione manuale');o.value='';input.append(o);for(const state of states){const o=el('option',state);o.value=state;input.append(o);}}input.value=p.manual?.[key]||'';l.append(input);form.append(l);}
    const result=el('div');const save=el('button','Salva integrazione locale');save.type='submit';form.append(save,result);form.onsubmit=async e=>{e.preventDefault();const values=Object.fromEntries(new FormData(form));try{await saveManual({...values,prompt_id:p.id});result.textContent='Salvato · fonte: utente locale';}catch(error){result.textContent='Errore: '+error.message;}};const editor=el('details');editor.append(el('summary','Integra dati manualmente'),form);d.append(editor);
    if(saveNote){const editor=el('details');editor.append(el('summary','Modifica nota originale'));const input=el('textarea',item.note||'');input.ariaLabel='Nota originale';const save=el('button','Salva nota');save.type='button';save.onclick=async()=>{try{await saveNote(input.value);save.textContent='Nota salvata';}catch(e){save.textContent='Errore: '+e.message;}};editor.append(input,save);d.append(editor);}
    return true;
  }
  return {toolbar,rows,card,details,date};
})();
