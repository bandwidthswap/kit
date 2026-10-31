/* The sections every view shares (now, attention, the tables with their drill-downs) and the Mint's view. */
window.BW = window.BW || {};
(function(BW){
  'use strict';
  var H=BW.H, F=BW.fmt, C=BW.charts, S=BW.sec={}, V=BW.views=BW.views||{};
  var esc=F.esc, bytes=F.bytes, money=F.money, pct=F.pct, tstamp=F.tstamp, hm=F.hm, ago=F.ago, sortBy=F.sortBy;

  S.now=function(X, now){
    var D=BW.D, st=X.st, cells=[], up = st.time ? (now - st.time) : null, snap=D.snapshot;
    cells.push({k:'service', v: snap?'snapshot':(D.asOf?'as of':(up!=null&&up<120?'up':'stale')), s: st.time?('answered '+ago(st.time, now)):'no status', cls: (snap||D.asOf)?'':(up!=null&&up<120?'ok':'bad')});
    var lastR = X.R.length ? (X.R[X.R.length-1].received_at||0) : (X.myR.length ? X.myR[X.myR.length-1].receipt.t_last : 0), lastFrom = X.R.length ? X.R[X.R.length-1].from : (X.role==='payer'?'me':'');
    cells.push({k: X.role==='payer'?'last receipt signed':X.role==='hop'?'last receipt countersigned':'last receipt', v: lastR?hm(lastR):'–', s: lastR?(ago(lastR, now)+(lastFrom&&lastFrom!=='me'?', from '+esc(lastFrom):'')):'none yet', cls: lastR&&(now-lastR)>2*H?'warn':''});
    var rw = X.pending.filter(function(p){ return !p.ack && !p.lost; }).length, lost = X.pending.filter(function(p){ return p.lost; }).length;
    cells.push({k:'waiting for an ack', v: String(rw), s: (rw?('receipt'+(rw>1?'s':'')+' without a hop\'s countersignature'):'nothing pending')+(lost?' · '+lost+' lost window'+(lost>1?'s':''):''), cls: rw>1?'warn':''});
    var disputed = X.S.filter(function(s){ return s.status==='disputed'; }).length;
    cells.push({k:'windows settled', v: String(X.S.length), s: disputed+' disputed · '+X.C.length+' commitment'+(X.C.length===1?'':'s'), cls:''});
    if(X.role==='mint') cells.push({k:'next commitment', v: hm((Math.floor(now/H)+1)*H), s: 'the hour closes and is signed at the top of the hour', cls:''});
    if(X.role!=='mint') cells.push({k:'outbox', v: String(st.outbox||0), s: st.outbox?'waiting for the Mint; nothing is lost':'empty, the Mint has everything', cls: st.outbox?'warn':'ok'});
    cells.push({k:'clock', v: st.clock_offset_s!=null ? (Math.abs(st.clock_offset_s)<0.5 ? (st.clock_offset_s*1000).toFixed(1)+' ms' : st.clock_offset_s.toFixed(2)+' s') : '–', s: st.clock_offset_s!=null?('against its time source, stratum '+(st.stratum||'?')):'not reported', cls: st.clock_offset_s!=null&&Math.abs(st.clock_offset_s)>1?'bad':''});
    if(st.disk_free_bytes!=null) cells.push({k:'disk free', v: bytes(st.disk_free_bytes), s:'for the ledger', cls: st.disk_free_bytes<5e8?'bad':''});
    if(st.last_report_at!=null && X.role==='mint') cells.push({k:'status posted', v: ago(st.last_report_at, now), s:'to the public feed', cls:(now-st.last_report_at)>600?'warn':''});
    return '<h2>Now <small>as the node sees it</small></h2><div class="now">'+cells.map(function(c){ return '<div class="'+c.cls+'"><span class="k">'+c.k+'</span><span class="v">'+esc(c.v)+'</span><span class="s">'+c.s+'</span></div>'; }).join('')+'</div>';
  };

  S.attention=function(X, now){
    var items=BW.attention(X, now);
    if(!items.length) return '<h2>Attention</h2><div class="now"><div class="ok"><span class="k">nothing</span><span class="v">all clear</span><span class="s">every check the node and this page know passes</span></div></div>';
    return '<h2>Attention <small>'+items.length+' item'+(items.length===1?'':'s')+'</small></h2><div class="cards">'+items.map(function(i){ return '<div class="card '+i.level+'">'+i.text+'</div>'; }).join('')+'</div>';
  };

  S.pending=function(X, now){
    if(!X.pending.length) return '';
    var rows=sortBy(X.pending, function(p){ return p.r.receipt.t_first; }, true).map(function(p){ var r=p.r.receipt; return '<tr><td>'+tstamp(r.t_first)+'</td><td>'+esc(X.name(r.payer))+' → '+esc(X.name(r.hop))+'</td><td class="n">'+bytes(r.bytes)+'</td><td>'+(p.lost?'<span class="st declined">lost</span> <span class="m">the hop never countersigned; the window is unpaid</span>':p.ack?'<span class="st pending">acked, not settled</span>':'<span class="st pending">no ack yet</span>')+'</td><td class="m">'+(p.r.received_at?'received '+ago(p.r.received_at, now):'signed '+ago(r.t_last, now))+'</td></tr>'; });
    return '<h2>Open windows <small>received, not settled</small></h2><div class="tbl"><table><thead><tr><th>window</th><th>payer → hop</th><th class="n">payer signed</th><th>state</th><th></th></tr></thead><tbody>'+rows.join('')+'</tbody></table></div>';
  };

  // The settled windows, each row opening to its two signed records (app.js draws the detail row on a click).
  S.windows=function(X, now, title){
    var W=sortBy(X.S, function(s){ return s.t_first; }, true).slice(0, 48);
    if(!W.length) return '<h2>'+(title||'Windows')+'</h2><div class="empty">No window has settled yet.</div>';
    var rows=W.map(function(s){
      var gap = s.payer_bytes ? (s.hop_bytes - s.payer_bytes)/s.payer_bytes : 0;
      var prem = s.premium_usd ? money(s.premium_usd)+(s.refund_usd?' <span class="m">(−'+money(s.refund_usd)+')</span>':'') : '';
      return '<tr class="open" data-win="'+esc(BW.keyOf(s))+'" title="open the two signed records"><td>'+tstamp(s.t_first)+'</td><td>'+esc(X.name(s.payer))+' → '+esc(X.name(s.hop))+'</td><td class="n">'+bytes(s.payer_bytes)+'</td><td class="n">'+bytes(s.hop_bytes)+'</td><td class="n">'+(gap>=0?'+':'')+pct(gap)+'</td><td><span class="st '+s.status+'">'+s.status+'</span>'+(s.margin!=null?' <span class="m">'+pct(s.margin,0)+'</span>':'')+'</td><td class="n">'+bytes(s.settled_bytes)+'</td><td class="n">'+money(s.amount_usd)+'</td><td class="n">'+(s.fee_usd!=null?money(s.fee_usd):'<span class="m">none</span>')+'</td><td class="n">'+prem+'</td><td class="m">'+(s.purchase?s.purchase.slice(0,8):'')+'</td></tr>';
    });
    return '<h2>'+(title||'Windows')+' <small>newest first, the last '+W.length+' · a row opens to its receipt and ack</small></h2><div class="tbl"><table><thead><tr><th>window</th><th>payer → hop</th><th class="n">payer</th><th class="n">hop</th><th class="n">gap</th><th>status · margin</th><th class="n">settled</th><th class="n">paid</th><th class="n">fee</th><th class="n">premium</th><th>purchase</th></tr></thead><tbody>'+rows.join('')+'</tbody></table></div>';
  };

  // The two signed records of a window side by side, with the fields that differ marked.
  S.windowDetail=function(X, key){
    var r=X.recByKey[key], a=X.acked[key], s=X.settledKey[key]; if(!r&&!a) return '<div class="note">The node does not hold the records of this window.</div>';
    var fields=['payer','hop','hop_index','merkle_root','bytes','t_first','t_last','prev_sig'];
    var rec=function(title, env, other){ if(!env) return '<div class="rec"><h4>'+title+'</h4><div class="note">not held here</div></div>'; var t=env.receipt; return '<div class="rec"><h4>'+title+' · signed by '+esc(X.name(title.indexOf('receipt')>=0?t.payer:t.hop))+'</h4><dl>'+fields.map(function(f){ var same=other&&other.receipt&&String(other.receipt[f])===String(t[f]); var cls=other?(same?'same':'diff'):''; var v=t[f]; if(f==='bytes') v=t[f]+' ('+bytes(t[f])+')'; else if(f==='t_first'||f==='t_last') v=t[f]+' ('+F.hms(t[f])+')'; else if(f==='prev_sig'||f==='merkle_root') v=String(v).slice(0,22)+'…'; return '<div class="'+cls+'" style="display:contents"><dt>'+f+'</dt><dd>'+esc(v)+'</dd></div>'; }).join('')+'<dt>sig</dt><dd>'+esc(String(env.sig).slice(0,22))+'…</dd>'+(env.received_at?'<dt>received</dt><dd>'+F.hms(env.received_at)+'</dd>':'')+(env.meter?'<dt>meter</dt><dd>rx '+env.meter.rx0+' → '+env.meter.rx1+' · tx '+env.meter.tx0+' → '+env.meter.tx1+(env.meter.reset?' · reset':'')+'</dd>':'')+'</dl></div>'; };
    var set = s ? '<div class="rec"><h4>the settlement</h4><dl><dt>rule</dt><dd>the smaller of the two counts</dd><dt>settled</dt><dd>'+bytes(s.settled_bytes)+' · '+s.status+(s.margin!=null?' at a margin of '+pct(s.margin,0):'')+'</dd><dt>paid</dt><dd>'+money(s.amount_usd)+(s.fee_usd!=null?' · fee '+money(s.fee_usd):'')+(s.premium_usd?' · premium '+money(s.premium_usd):'')+(s.refund_usd?' · returned '+money(s.refund_usd):'')+'</dd><dt>at</dt><dd>'+F.hms(s.t)+'</dd>'+(s.link?'<dt>link</dt><dd>'+esc(String(s.link).slice(0,22))+'…</dd>':'')+'</dl></div>' : '<div class="rec"><h4>the settlement</h4><div class="note">not settled</div></div>';
    return '<div class="pair">'+rec('the receipt', r, a)+rec('the ack', a, r)+set+'</div>';
  };

  S.commitments=function(X, now){
    var Cs=sortBy(X.C, function(c){ return c.window; }, true).slice(0, 24);
    if(!Cs.length) return '<h2>Commitments</h2><div class="empty">No hour has been committed yet.</div>';
    var rows=Cs.map(function(c){ return '<tr><td>'+tstamp(c.window)+'</td><td class="n">'+c.count+'</td><td class="m">'+esc(String(c.root).slice(0,24))+'…</td><td class="m">'+(c.signature?'signed':'unsigned')+'</td><td class="m">'+tstamp(c.t)+'</td><td><button class="btn" data-verify="'+c.window+'">verify here</button> <a href="/proof?window='+c.window+'" class="m">proof</a></td><td id="vr-'+c.window+'" class="m"></td></tr>'; });
    return '<h2>Commitments <small>one signed Merkle root per hour · '+X.C.length+' so far</small></h2>'+C.chainChart(X, now)+'<div class="tbl"><table><thead><tr><th>hour</th><th class="n">settlements</th><th>root</th><th></th><th>signed at</th><th></th><th>checked</th></tr></thead><tbody>'+rows.join('')+'</tbody></table></div>'+
      '<p class="note">"Verify here" fetches the hour\'s proof and checks every settlement\'s path to the root and the root\'s signature against the Mint\'s NYM, in this browser, with the same code as the public verifier.</p>';
  };

  S.peers=function(X, now){
    var P=Object.keys(X.peers).map(function(k){ return X.peers[k]; }); if(!P.length) return '';
    P.sort(function(a,b){ return a.name<b.name?-1:1; });
    var rows=P.map(function(p){ return '<tr><td>'+esc(p.name)+'</td><td>'+Object.keys(p.roles).join(', ')+'</td><td>'+esc(p.place||'–')+'</td><td class="m">'+esc(p.nym)+'</td><td class="n">'+p.windows+'</td><td class="m">'+ago(p.lastSeen, now)+'</td></tr>'; });
    return '<h2>Peers <small>the NYMs this Mint accepts</small></h2><div class="tbl"><table><thead><tr><th>name</th><th>seen as</th><th>place</th><th>NYM</th><th class="n">windows</th><th>last heard</th></tr></thead><tbody>'+rows.join('')+'</tbody></table></div>';
  };

  S.announcements=function(X, links, title){
    var L=links||[]; if(!L.length) return '<h2>'+(title||'Announcements')+'</h2><div class="empty">No link is announced; windows settle on the pilot\'s defaults (one percent, ten cents a gigabyte, five seconds).</div>';
    var rows=L.map(function(l){ return '<tr><td>'+esc(X.name(l.relay))+'</td><td>'+(l.payer==='*'?'any payer':esc(X.name(l.payer)))+'</td><td class="n">'+l.price+' '+esc(l.unit||'')+'</td><td class="n">'+pct(l.margin,0)+' / '+bytes(l.min_bytes)+'</td><td class="n">'+(l.patience_windows||0)+'</td><td class="n">'+l.clock_tolerance_s+' s</td><td>'+esc(l.medium)+' · '+esc(l.range_class)+'</td><td>'+esc(l.window)+(l.light_time_s?' · '+l.light_time_s+' s light':'')+'</td><td class="m">'+tstamp(l.since)+'</td></tr>'; });
    return '<h2>'+(title||'Announcements')+' <small>the link parameters in force</small></h2><div class="tbl"><table><thead><tr><th>relay</th><th>for</th><th class="n">price</th><th class="n">margin / min</th><th class="n">patience</th><th class="n">clock</th><th>medium</th><th>window</th><th>since</th></tr></thead><tbody>'+rows.join('')+'</tbody></table></div>';
  };

  S.purchases=function(X, now, title){
    var P=X.purchases.filter(function(p){ return p.kind==='purchase'; }); if(!P.length) return '<h2>'+(title||'Purchases')+'</h2><div class="empty">No purchase has been posted.</div>';
    var m=BW.D.map, routeName=function(x){ if(x==null) return 'any'; return X.names[x]||(m&&m.places&&m.places[x]?m.places[x].name:x); };
    var rows=sortBy(P, function(p){ return p.posted_at; }, true).map(function(p){
      var paid=X.paidOn[p.id]||{premium:0,refund:0,windows:0,steward:0,own:0,drawn:0,rows:[]}, closed = p.closed || p.deadline<now || (p.windows && paid.windows>=p.windows);
      var left=Math.max(0, (p.escrow||0)-paid.own-paid.drawn);
      return '<tr class="open" data-pur="'+esc(p.id)+'" title="open the windows it paid"><td class="m">'+p.id.slice(0,8)+'</td><td>'+esc(p.program)+(p.topups&&p.topups.length?' <span class="m">+'+p.topups.length+' top-up'+(p.topups.length===1?'':'s')+'</span>':'')+'</td><td>'+esc(routeName(p.poster))+'</td><td class="w">'+esc(routeName(p.route.from))+' → '+esc(routeName(p.route.to))+(p.route.not_via&&p.route.not_via.length?' <span class="m">not via '+esc(p.route.not_via.map(routeName).join(', '))+'</span>':'')+'</td><td class="n">'+(p.min_bytes?bytes(p.min_bytes):'any')+'</td><td class="n">'+money(p.premium_per_gb)+'/GB</td><td class="n">'+money(p.escrow)+'</td><td class="n">'+paid.windows+' / '+p.windows+'<div class="pb"><i style="width:'+(p.windows?Math.min(100,100*paid.windows/p.windows):0).toFixed(0)+'%"></i></div></td><td class="n">'+money(paid.premium)+(paid.refund?' <span class="m">(−'+money(paid.refund)+')</span>':'')+'</td><td class="n">'+money(left)+(closed&&left>0?' <span class="m">returned</span>':'')+'</td><td><span class="st '+(closed?'closed':'open')+'">'+(closed?'closed':'open')+'</span>'+(p.closed_at?' <span class="m">'+tstamp(p.closed_at)+'</span>':'')+'</td><td class="m">'+tstamp(p.posted_at)+' → '+tstamp(p.deadline)+'</td></tr>';
    });
    return '<h2>'+(title||'Purchases')+' <small>'+P.length+' posted · a row opens to the windows it paid</small></h2><div class="tbl"><table><thead><tr><th>id</th><th>program</th><th>poster</th><th>route</th><th class="n">min / window</th><th class="n">premium</th><th class="n">escrow</th><th class="n">windows</th><th class="n">premium paid</th><th class="n">unspent</th><th></th><th>posted → deadline</th></tr></thead><tbody>'+rows.join('')+'</tbody></table></div>';
  };
  S.purchaseDetail=function(X, id){
    var paid=X.paidOn[id]; if(!paid||!paid.rows.length) return '<div class="note">No window has settled against this purchase yet.</div>';
    return '<table style="width:auto"><thead><tr><th>window</th><th class="n">settled</th><th class="n">rate</th><th class="n">premium</th><th class="n">from the Steward</th><th class="n">returned</th></tr></thead><tbody>'+sortBy(paid.rows, function(s){ return s.t_first; }, true).map(function(s){ return '<tr><td>'+tstamp(s.t_first)+'</td><td class="n">'+bytes(s.settled_bytes)+'</td><td class="n">'+F.rateOf(s).toFixed(2)+' Mbit/s</td><td class="n">'+money(s.premium_usd||0)+'</td><td class="n">'+money(s.from_steward_usd||0)+'</td><td class="n">'+money(s.refund_usd||0)+'</td></tr>'; }).join('')+'</tbody></table>';
  };

  S.steward=function(X, now){
    var s=BW.D.steward; if(!s) return '';
    var last=s.last_run;
    return '<h2>The Steward <small>runs on this host after each hour\'s commitment</small></h2><div class="now">'+
      [['income', money(s.income_usd), 'f = '+pct(s.f,0)+' of every fee'],['in open offers', money(s.in_offers_usd!=null?s.in_offers_usd:s.posted_usd), ''],['escrow', money(s.escrow_usd), 'unposted'],['match reserve', money(s.reserve_usd), 'm = '+pct(s.m,0)+' of income'],['paid out', money(s.paid_usd), 'against receipts'],['returned', money(s.refunded_usd), 'to the escrow'],['held by the cap', money(s.held_by_cap_usd), 'f − '+pct(s.margin||0,0)+' per payer'],['last run', last?ago(last, now):'never', last?tstamp(last):'']]
      .map(function(c){ return '<div><span class="k">'+c[0]+'</span><span class="v">'+esc(c[1])+'</span><span class="s">'+esc(c[2])+'</span></div>'; }).join('')+'</div>'+
      (s.skipped&&s.skipped.length?'<p class="note">Last run skipped: '+s.skipped.map(esc).join('; ')+'.</p>':'')+
      (BW.D.asOf?'<p class="note">The Steward\'s balances are as of now; the as-of control does not move them.</p>':'')+
      '<p class="note">NYM <code>'+esc(s.nym||'')+'</code> · the book in full: <code>bwkit steward book</code> on this host, and the public page at bandwidthswap.com/docs/steward.html.</p>';
  };

  S.invoice=function(X, now){
    var d=new Date(now*1000), month=d.getFullYear()+'-'+F.pad(d.getMonth()+1), by={};
    X.S.forEach(function(s){ var e=new Date(s.t_last*1000); if(e.getFullYear()+'-'+F.pad(e.getMonth()+1)!==month) return; var k=s.payer+'|'+s.hop; var o=by[k]=by[k]||{payer:s.payer,hop:s.hop,windows:0,disputed:0,bytes:0,amount:0,fee:0,premium:0,refund:0}; o.windows++; if(s.status==='disputed') o.disputed++; o.bytes+=s.settled_bytes; o.amount+=s.amount_usd||0; o.fee+=s.fee_usd||0; o.premium+=s.premium_usd||0; o.refund+=s.refund_usd||0; });
    var rows=Object.keys(by).map(function(k){ return by[k]; }); if(!rows.length) return '';
    var tf=0,ta=0,tp=0, html=rows.map(function(o){ tf+=o.fee; ta+=o.amount; tp+=o.premium; var relay=o.amount-o.fee+o.premium; return '<tr><td>'+esc(X.name(o.payer))+' pays '+esc(X.name(o.hop))+'</td><td class="n">'+o.windows+' <span class="m">('+o.disputed+' disputed)</span></td><td class="n">'+bytes(o.bytes)+'</td><td class="n">'+money(o.amount)+'</td><td class="n">'+money(o.premium)+(o.refund?' <span class="m">(−'+money(o.refund)+')</span>':'')+'</td><td class="n">'+money(relay)+'</td><td class="n">'+money(o.fee)+'</td></tr>'; });
    html+='<tr class="sum"><td>'+month+'</td><td></td><td></td><td class="n">'+money(ta)+'</td><td class="n">'+money(tp)+'</td><td class="n">'+money(ta-tf+tp)+'</td><td class="n">'+money(tf)+'</td></tr>';
    return '<h2>Invoice <small>'+month+', the month so far, as <code>bwkit invoice</code> sums it</small></h2><div class="tbl"><table><thead><tr><th>pair</th><th class="n">windows</th><th class="n">settled</th><th class="n">carriage</th><th class="n">premium</th><th class="n">the relay\'s</th><th class="n">the Steward\'s</th></tr></thead><tbody>'+html+'</tbody></table></div>';
  };

  S.mapTable=function(){
    var m=BW.D.map; if(!m||!m.places) return '';
    var byPlace={}; for(var n in (m.nodes||{})) (byPlace[m.nodes[n]]=byPlace[m.nodes[n]]||[]).push(n);
    var rows=Object.keys(m.places).map(function(id){ var p=m.places[id]; return '<tr><td>'+esc(p.name||id)+((p.name&&p.name!==id)?' <span class="m">'+esc(id)+'</span>':'')+'</td><td>'+esc(p.domain)+'</td><td>'+esc(p.medium)+' · '+esc(p.range_class)+'</td><td>'+(p.mapped?'mapped':'proposed')+'</td><td>'+esc((byPlace[id]||[]).join(', ')||'none')+'</td></tr>'; });
    return '<h2>The map, as a table <small>places by Mint consensus; here, the membership\'s file</small></h2><div class="tbl"><table><thead><tr><th>place</th><th>domain</th><th>medium</th><th></th><th>nodes</th></tr></thead><tbody>'+rows.join('')+'</tbody></table></div>';
  };

  // ---------------------------------------------------------------- the Mint's view
  V.mint=function(X, now){
    return [S.now(X, now), S.attention(X, now), C.hourTimeline(X, now), C.closeChart(X, now), S.pending(X, now), C.trafficChart(X, now), C.linkCards(X, now), S.windows(X, now), S.commitments(X, now), C.mapChart(X, now), S.peers(X, now), S.announcements(X, X.st.links), S.purchases(X, now), S.steward(X, now), C.moneyChart(X, now), S.invoice(X, now), S.mapTable()].join('');
  };
})(window.BW);
