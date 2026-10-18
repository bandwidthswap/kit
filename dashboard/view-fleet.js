/* The fleet's view: several nodes, perhaps several Mints, read from the operator's browser. The list of nodes lives
   in the browser (localStorage, or ?nodes=), never on a node. */
window.BW = window.BW || {};
(function(BW){
  'use strict';
  var H=BW.H, F=BW.fmt, C=BW.charts, S=BW.sec, V=BW.views;
  var esc=F.esc, bytes=F.bytes, money=F.money, pct=F.pct, tstamp=F.tstamp, hm=F.hm, ago=F.ago, sortBy=F.sortBy;

  BW.fleetList=function(){ try{ var q=new URLSearchParams(location.search).get('nodes'); if(q) return q.split(',').map(function(s){ return s.trim(); }).filter(Boolean); var v=localStorage.getItem('bw.fleet.nodes'); return v?JSON.parse(v):[]; }catch(e){ return []; } };
  BW.fleetSave=function(list){ try{ localStorage.setItem('bw.fleet.nodes', JSON.stringify(list)); }catch(e){} };

  // What a node's status alone says needs attention; its own `health` first when it carries one.
  function nodeAttention(n, now){
    var items=[], st=n.status; if(!st) return [{level:'bad', text:(n.error||'did not answer')}];
    ((st.health&&st.health.items)||[]).forEach(function(i){ items.push({level:i.level||'warn', text:i.text}); });
    if(!BW.D.snapshot && st.time && now-st.time>120) items.push({level:'bad', text:'status is '+ago(st.time, now)});
    if(st.clock_offset_s!=null && Math.abs(st.clock_offset_s)>1) items.push({level:'bad', text:'clock '+st.clock_offset_s.toFixed(2)+' s off its source'});
    if(st.disk_free_bytes!=null && st.disk_free_bytes<5e8) items.push({level:'bad', text:'disk '+bytes(st.disk_free_bytes)+' free'});
    if(st.outbox) items.push({level: st.outbox>2?'bad':'warn', text: st.outbox+' in the outbox'});
    if(st.patience) for(var p in st.patience){ if(st.patience[p].declined) items.push({level:'bad', text:'a payer declined'}); }
    if(st.pending && st.pending.receipts_without_ack>1) items.push({level:'warn', text: st.pending.receipts_without_ack+' receipts without an ack'});
    return items;
  }

  function grid(nodes, now, placeOf){
    return '<h2>The fleet <small>'+nodes.length+' node'+(nodes.length===1?'':'s')+'</small></h2><div class="fleet">'+nodes.map(function(n){
      var st=n.status, items=nodeAttention(n, now), bad=items.filter(function(i){ return i.level==='bad'; }).length;
      var state = !st ? 'none' : ((!BW.D.snapshot && now-st.time>120) ? 'stale' : 'up');
      var kv = st ? [['place', placeOf(st.name)||'–'],['clock', st.clock_offset_s!=null?(Math.abs(st.clock_offset_s)<0.5?(st.clock_offset_s*1000).toFixed(1)+' ms':st.clock_offset_s.toFixed(2)+' s'):'–'],['disk', st.disk_free_bytes!=null?bytes(st.disk_free_bytes):'–'],['outbox', st.outbox!=null?String(st.outbox):'–'],[st.role==='mint'?'settled':'sample', st.role==='mint'?(st.settlements+' windows · '+st.commitments+' commitments'):(st.sample&&st.sample.t?ago(st.sample.t, now):'–')],['version', st.impl||('bwkit/'+st.bwkit)]] : [['last error', n.error||'no answer']];
      return '<div class="card'+(state==='up'?'':' stale')+(bad?' bad':(items.length?' warn':''))+'"><h3><a href="'+esc(n.url)+'/" target="_blank" rel="noopener">'+esc(n.name||n.url)+'</a> <span class="role" style="font-family:var(--mono);font-size:11px;background:var(--soft);border-radius:4px;padding:1px 6px;text-transform:uppercase">'+esc(st?st.role:'?')+'</span> <span class="st '+state+'">'+(state==='none'?'no answer':state)+'</span></h3><div class="kv">'+kv.map(function(k){ return '<span>'+esc(k[0])+'</span><span>'+esc(k[1])+'</span>'; }).join('')+'</div><span class="who">'+(items.length?items.length+' attention item'+(items.length===1?'':'s'):'nothing needs attention')+' · '+esc(n.url)+'</span></div>';
    }).join('')+'</div>';
  }

  function attentionAll(nodes, now){
    var all=[]; nodes.forEach(function(n){ nodeAttention(n, now).forEach(function(i){ all.push({level:i.level, text:i.text, node:n.name||n.url}); }); });
    all.sort(function(a,b){ var o={bad:0,warn:1,info:2}; return (o[a.level]||3)-(o[b.level]||3); });
    if(!all.length) return '<h2>Attention across the fleet</h2><div class="now"><div class="ok"><span class="k">nothing</span><span class="v">all clear</span><span class="s">every node answered and nothing needs attention</span></div></div>';
    return '<h2>Attention across the fleet <small>'+all.length+' item'+(all.length===1?'':'s')+'</small></h2><div class="cards">'+all.map(function(i){ return '<div class="card '+i.level+'">'+esc(i.text)+'<span class="who">'+esc(i.node)+'</span></div>'; }).join('')+'</div>';
  }

  // Each Mint's last day, derived the same way the Mint's own page derives it.
  function mintBooks(n, now){
    var saved={ledger:BW.D.ledger, commitments:BW.D.commitments, purchases:BW.D.purchases, status:BW.D.status, map:BW.D.map, steward:BW.D.steward};
    BW.D.ledger=n.ledger||[]; BW.D.commitments=[]; BW.D.purchases=(n.steward&&n.steward.purchases)||[]; BW.D.status=n.status; BW.D.map=n.map; BW.D.steward=n.steward;
    var X=BW.derive(now); X.purchases=[];
    BW.D.ledger=saved.ledger; BW.D.commitments=saved.commitments; BW.D.purchases=saved.purchases; BW.D.status=saved.status; BW.D.map=saved.map; BW.D.steward=saved.steward;
    return X;
  }

  function position(nodes, mints, now){
    var mine={}; nodes.forEach(function(n){ if(n.status&&n.mine!==false) mine[n.status.name]=n.status; });
    var rows='', paid=0, earned=0, fees=0;
    mints.forEach(function(m){ var X=m.X; var by={};
      X.S.forEach(function(s){ var k=X.name(s.payer)+'|'+X.name(s.hop); var o=by[k]=by[k]||{payer:X.name(s.payer),hop:X.name(s.hop),windows:0,bytes:0,amount:0,fee:0,premium:0,refund:0}; o.windows++; o.bytes+=s.settled_bytes; o.amount+=s.amount_usd||0; o.fee+=s.fee_usd||0; o.premium+=s.premium_usd||0; o.refund+=s.refund_usd||0; });
      Object.keys(by).forEach(function(k){ var o=by[k], p=mine[o.payer]?o.amount+o.premium-o.refund:0, e=mine[o.hop]?o.amount-o.fee+o.premium:0; paid+=p; earned+=e; fees+=o.fee;
        rows+='<tr><td>'+esc(m.name)+'</td><td>'+esc(o.payer)+(mine[o.payer]?' <span class="m">mine</span>':'')+' pays '+esc(o.hop)+(mine[o.hop]?' <span class="m">mine</span>':'')+'</td><td class="n">'+o.windows+'</td><td class="n">'+bytes(o.bytes)+'</td><td class="n">'+money(o.amount+o.premium)+'</td><td class="n">'+money(o.fee)+'</td><td class="n">'+(p?'−'+money(p):'')+'</td><td class="n">'+(e?'+'+money(e):'')+'</td></tr>'; }); });
    if(!rows) return '<h2>Position</h2><div class="empty">No Mint in the fleet has settled a window in the last day.</div>';
    var cells=[['paid by my payers', '−'+money(paid), 'carriage and premiums, less refunds'],['earned by my hops', '+'+money(earned), 'carriage less the fee, plus premiums'],['net', (earned-paid>=0?'+':'−')+money(Math.abs(earned-paid)), 'the last 24 hours'],['fees to the Stewards', money(fees), 'across my Mints']];
    return '<h2>Position <small>the last 24 hours, across every Mint in the fleet</small></h2><div class="now">'+cells.map(function(c){ return '<div><span class="k">'+c[0]+'</span><span class="v">'+esc(c[1])+'</span><span class="s">'+esc(c[2])+'</span></div>'; }).join('')+'</div><div class="tbl" style="margin-top:10px"><table><thead><tr><th>Mint</th><th>pair</th><th class="n">windows</th><th class="n">settled</th><th class="n">paid</th><th class="n">fee</th><th class="n">I paid</th><th class="n">I earned</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
  }

  function hourAcross(mints, now){
    if(!mints.length) return '';
    var due=Math.floor((now-H-300)/H)*H, rows=mints.map(function(m){ var X=m.X, last=X.C.length?sortBy(X.C, function(c){ return c.window; }, true)[0]:null, st=m.node.status||{}; var ok=last&&last.window>=due; /* a window is due to be committed five minutes after it closes */ return '<tr><td>'+esc(m.name)+'</td><td>'+(last?tstamp(last.window)+' <span class="m">'+last.count+' settlement'+(last.count===1?'':'s')+', signed '+ago(last.t, now)+'</span>':'none')+'</td><td>'+(ok?'<span class="st ok">on time</span>':(last?'<span class="st pending">behind</span>':'<span class="st none">none yet</span>'))+'</td><td class="n">'+(st.settlements!=null?st.settlements:'–')+'</td><td class="n">'+(st.pending?st.pending.receipts_without_ack:'–')+'</td><td>'+(m.node.steward?money(m.node.steward.income_usd)+' income · '+money(m.node.steward.in_offers_usd!=null?m.node.steward.in_offers_usd:m.node.steward.posted_usd)+' in offers':'no Steward')+'</td></tr>'; });
    return '<h2>The hour across the fleet <small>each Mint\'s last commitment</small></h2><div class="tbl"><table><thead><tr><th>Mint</th><th>last committed hour</th><th></th><th class="n">windows settled</th><th class="n">waiting for an ack</th><th>the Steward</th></tr></thead><tbody>'+rows.join('')+'</tbody></table></div>';
  }

  function listEditor(nodes){
    if(BW.D.snapshot) return '';
    return '<div class="fleet-add"><input id="fleet-url" type="url" placeholder="http://a-node:8710" aria-label="a node\'s address"><button class="btn" id="fleet-add">add</button>'+nodes.map(function(n){ return '<button class="btn" data-remove="'+esc(n.url)+'" title="remove from this browser\'s list">× '+esc(n.name||n.url)+'</button>'; }).join('')+'<span class="note" style="margin:0">the list lives in this browser only</span></div>';
  }

  V.fleet=function(now){
    var D=BW.D, nodes=D.nodes||[];
    if(!nodes.length) return '<h2>The fleet</h2><div class="empty">No nodes yet. Add each node\'s address below; the page reads their status and, for a Mint, its books, from this browser.</div>'+listEditor(nodes);
    var mints=nodes.filter(function(n){ return n.status&&n.status.role==='mint'; }).map(function(n){ return {node:n, name:n.status.name, X:mintBooks(n, now)}; });
    var placeOf=function(name){ for(var i=0;i<mints.length;i++){ var m=mints[i].node.map; if(m&&m.nodes&&m.nodes[name]) return (m.places[m.nodes[name]]||{}).name||m.nodes[name]; } return null; };
    var mine=nodes.filter(function(n){ return n.status; }).map(function(n){ return n.status.name; });
    var maps=mints.map(function(m){ return m.node.map ? C.mapChart(m.X, now, m.node.map, mine) : ''; }).join('');
    return [grid(nodes, now, placeOf), listEditor(nodes), attentionAll(nodes, now), hourAcross(mints, now), position(nodes, mints, now), maps].join('');
  };
})(window.BW);
