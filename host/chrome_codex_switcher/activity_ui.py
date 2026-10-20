"""Small standalone view backed by the CCS activity endpoint."""
from __future__ import annotations


def activity_dashboard_html() -> str:
    return """<!doctype html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ChatGPT · Elaborazioni</title>
<style>
:root{font-family:system-ui,sans-serif;color-scheme:light dark}
*{box-sizing:border-box}body{margin:0;background:Canvas;color:CanvasText}
header{position:sticky;top:0;z-index:1;background:Canvas;border-bottom:1px solid #8885;padding:20px max(16px,calc((100vw - 950px)/2))}
h1{font-size:1.25rem;margin:0 0 5px}p{font-size:.86rem;opacity:.72;margin:0 0 12px}
.toolbar{display:flex;flex-wrap:wrap;gap:8px}input,select{min-width:0;border:1px solid #8888;border-radius:8px;padding:9px;font:inherit;color:CanvasText;background:Canvas}
input{flex:1;min-width:190px}#count{margin-top:10px;font-size:.84rem;opacity:.75}
main{max-width:950px;margin:auto;padding:18px 16px 40px}
h2{font-size:1rem;margin:12px 0 10px}.group{margin-bottom:25px}
.card{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px;margin:8px 0;border:1px solid #8885;border-radius:12px}
.card.running{border-left:4px solid #d2a03f}
.details{min-width:0;flex:1}.title{font-size:.99rem;font-weight:650;overflow-wrap:anywhere;margin-bottom:5px}
.meta{font-size:.78rem;opacity:.7;overflow-wrap:anywhere}
.state{font-size:.8rem;font-weight:650;white-space:nowrap}.running .state{color:#d3a33f}
button{cursor:pointer;border:1px solid #8888;background:ButtonFace;color:ButtonText;border-radius:8px;padding:8px 12px;font:inherit}
.empty{font-size:.88rem;opacity:.6;padding:16px;border:1px dashed #8887;border-radius:9px}
small{font-size:.78rem;opacity:.64}
@media(max-width:520px){.card{align-items:flex-start;flex-wrap:wrap}.card button{margin-left:auto}}
</style></head>
<body><header><h1>ChatGPT · Elaborazioni</h1><p>Web e Desktop nello stesso posto. Stato rilevato, non dedotto dalla cronologia.</p>
<div class="toolbar"><input id="filter" type="search" placeholder="Filtra per titolo…" aria-label="Filtra chat">
<select id="source" aria-label="Origine"><option value="all">Web + Desktop</option><option value="web">Solo Web</option><option value="desktop">Solo Desktop</option></select>
<a href="/ui/search">Contesti e note</a></div><div id="count">Caricamento…</div><div id="delivery" class="meta" aria-live="polite"></div></header>
<main><section class="group"><h2>In elaborazione</h2><div id="running"></div></section>
<section class="group"><h2>Terminate di recente <small>(ultimi 30 minuti)</small></h2><div id="finished"></div></section>
<small>Web: sono osservabili soltanto le chat aperte in Chrome con l'estensione attiva. Schede chiuse o non osservabili non vengono segnate come terminate. Desktop: il segnale viene dai file JSONL locali; un'elaborazione senza nuovi eventi per oltre un'ora non viene dichiarata ancora attiva.</small></main>
<script>
const filter=document.querySelector('#filter'),source=document.querySelector('#source');
let snapshot={active:0,recent:0,chats:[]};
function card(item){
 const row=document.createElement('article');row.className='card '+item.status;
 const left=document.createElement('div');left.className='details';
 const title=document.createElement('div');title.className='title';title.textContent=item.title;
 const meta=document.createElement('div');meta.className='meta';
 meta.textContent=(item.source==='web'?'Web':'Desktop')+' · '+new Date(item.updated_at*1000).toLocaleString()+' · '+item.url;
 left.append(title,meta);
 const right=document.createElement('div');right.className='state';
 right.textContent=({running:'In elaborazione',finished:'Terminata',interrupted:'Interrotta'})[item.status]||'Non verificato';
 const open=document.createElement('button');open.textContent='Apri chat';
 open.onclick=async()=>{
  open.disabled=true;
  try{
   const r=await fetch('/api/activity/open',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:item.url})});
   const data=await r.json();if(!data.ok)throw Error(data.error||'Apertura non disponibile');
  }catch(error){window.alert(String(error.message||error))}
  finally{open.disabled=false}
 };
 row.append(left,right,open);return row;
}
function render(){
 const query=filter.value.trim().toLocaleLowerCase(),origin=source.value;
 const items=snapshot.chats.filter(x=>(origin==='all'||x.source===origin)&&(!query||(x.title+' '+x.url).toLocaleLowerCase().includes(query)));
 const running=document.querySelector('#running'),finished=document.querySelector('#finished');
 for(const [container,status] of [[running,'running'],[finished,'done']]){
  container.textContent='';
  const group=items.filter(x=>status==='running'?x.status==='running':x.status!=='running');
  if(group.length){for(const x of group)container.append(card(x))}
  else{const empty=document.createElement('div');empty.className='empty';empty.textContent=status==='running'?'Nessuna elaborazione osservata':'Nessuna elaborazione terminata di recente';container.append(empty)}
 }
 document.querySelector('#count').textContent=snapshot.active+' in elaborazione · '+snapshot.recent+' terminate di recente';
 const delivery=snapshot.notification_delivery||{};
 const label=({accepted:'Ultimo invio Telegram accettato',rejected:'Ultimo invio Telegram rifiutato',uncertain:'Esito dell’ultimo invio Telegram incerto',unavailable:'Invio Telegram non disponibile'})[delivery.status]||'Nessun invio Telegram ancora verificato';
 document.querySelector('#delivery').textContent=label+(delivery.observed_at?' · '+new Date(delivery.observed_at*1000).toLocaleString():'');

}
async function refresh(){
 try{const r=await fetch('/api/activity',{cache:'no-store'});if(!r.ok)throw Error('bridge offline');snapshot=await r.json();render()}
 catch{document.querySelector('#count').textContent='Servizio locale non raggiungibile'}
}
filter.addEventListener('input',render);source.addEventListener('change',render);
refresh();setInterval(refresh,4000);
</script></body></html>"""
