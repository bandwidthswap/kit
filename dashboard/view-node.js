/* The node's view: a hop or a payer reading its own link. */
window.BW = window.BW || {};
(function(BW){
  'use strict';
  var H=BW.H, F=BW.fmt, C=BW.charts, S=BW.sec, V=BW.views;
  var esc=F.esc, bytes=F.bytes, money=F.money, pct=F.pct, tstamp=F.tstamp, ago=F.ago, sortBy=F.sortBy;

  // My month: what the link did for me and what I am owed or owe, from the Mint's settlements of my windows.
  function myMonth(X, now){
    var d=new Date(now*1000), month=d.getFullYear()+'-'+F.pad(d.getMonth()+1), me=X.me, hop=X.role==='hop';
    var W=X.S.filter(function(s){ var e=new Date(s.t_last*1000); return e.getFullYear()+'-'+F.pad(e.getMonth()+1)===month; });
    var gb=0, carriage=0, fee=0, prem=0, refund=0, disputed=0; W.forEach(function(s){ gb+=s.settled_bytes; carriage+=s.amount_usd||0; fee+=s.fee_usd||0; prem+=s.premium_usd||0; refund+=s.refund_usd||0; if(s.status==='disputed') disputed++; });
    var mine = hop ? carriage-fee+prem : carriage+prem-refund;
    var cells=[[hop?'owed to me':'I owe', money(mine), hop?'carriage less the fee, plus premiums':'carriage plus premiums, less what came back'],['windows', String(W.length), disputed+' settled disputed'],['carried', bytes(gb), 'settled bytes'],['the Steward\'s fee', money(fee), 'f of every window'+(hop?', out of my share':', out of what I paid')],['premiums', money(prem), hop?'earned on purchased windows':'paid on my purchases']];
    return '<h2>My month <small>'+month+', as the Mint settled it</small></h2><div class="now">'+cells.map(function(c){ return '<div><span class="k">'+c[0]+'</span><span class="v">'+esc(c[1])+'</span><span class="s">'+esc(c[2])+'</span></div>'; }).join('')+'</div>';
  }

  // My link: the parameters I announced (a hop) or settle under (a payer), and each payer's standing against my patience.
  function myLink(X, now){
    var st=X.st, out='';
    if(X.role==='hop'){
      var l=st.link ? [Object.assign({relay: st.nym, payer:'*', since: (BW.D.links[BW.D.links.length-1]||{}).since}, st.link)] : [];
      out+=S.announcements(X, l, 'My link');
      var pats=st.patience||{}, keys=Object.keys(pats);
      if(keys.length){ out+='<h2>Payers <small>their standing against my patience of '+((st.link&&st.link.patience_windows)||0)+' short windows</small></h2><div class="tbl"><table><thead><tr><th>payer</th><th class="n">short windows</th><th>standing</th></tr></thead><tbody>'+keys.map(function(p){ var ps=pats[p]; return '<tr><td>'+esc(X.name(p))+' <span class="m">'+esc(p)+'</span></td><td class="n">'+ps.short+'</td><td>'+(ps.declined?'<span class="st declined">declined</span> <span class="m">since '+tstamp(ps.declined_at)+'</span>':'<span class="st ok">served</span>')+'</td></tr>'; }).join('')+'</tbody></table></div><p class="note">A short window is one where the payer signed for less than my count by more than the margin. After the patience runs out I decline the payer\'s next receipt and run the on-decline hook, which on this node stops the service.</p>'; }
    } else {
      var hops={}; X.S.forEach(function(s){ hops[s.hop]=s; }); var hk=Object.keys(hops);
      out+='<h2>My link <small>the hop I buy from and the terms my windows settled under</small></h2>'+(hk.length?'<div class="tbl"><table><thead><tr><th>hop</th><th class="n">margin in force</th><th class="n">price</th><th class="n">windows</th><th>last settled</th></tr></thead><tbody>'+hk.map(function(h){ var s=hops[h], n=X.S.filter(function(x){ return x.hop===h; }).length; return '<tr><td>'+esc(X.name(h))+' <span class="m">'+esc(h)+'</span></td><td class="n">'+(s.margin!=null?pct(s.margin,0):'default')+'</td><td class="n">'+(s.price_usd_per_gb!=null?money(s.price_usd_per_gb)+'/GB':'–')+'</td><td class="n">'+n+'</td><td>'+tstamp(s.t_first)+'</td></tr>'; }).join('')+'</tbody></table></div>':'<div class="empty">No window of mine has settled yet.</div>');
    }
    return out;
  }

  // The Mint, from my side: who keeps my books, whether it answers, and whether my windows are in committed hours.
  function theMint(X, now){
    var D=BW.D, m=D.mint, name=(X.st.mint_name||(m&&m.name)||'the Mint');
    var cs=(m&&m.commitments&&m.commitments.length)?m.commitments:X.C, last=cs.length?sortBy(cs, function(c){ return c.window; }, true)[0]:null;
    var mine=sortBy(X.S, function(s){ return s.t_first; }, true), covered=0, uncovered=[];
    mine.slice(0,24).forEach(function(s){ var w=Math.floor(s.t_first/H)*H; if(cs.some(function(c){ return c.window===w; })) covered++; else if(now-w>H+300) uncovered.push(w); });
    var reach = D.mode==='live' ? (m&&m.ledger&&m.ledger.length ? 'answered just now' : 'did not answer') : (m&&m.ledger&&m.ledger.length?'held in this snapshot':'no copy here');
    var cells=[['Mint', name, last?'NYM '+String(last.mint||'').slice(0,20)+'…':''],['reachable', reach, 'through this node\'s proxy'],['my windows committed', covered+' of '+Math.min(24, mine.length), uncovered.length?uncovered.length+' settled hour'+(uncovered.length===1?'':'s')+' not yet committed':'the last 24 settled'],['latest commitment', last?tstamp(last.window):'–', last?(last.count+' settlement'+(last.count===1?'':'s')+', signed '+ago(last.t, now)):'none held']];
    var verify = last ? '<p class="note"><button class="btn" data-verify="'+last.window+'">verify the latest hour here</button> <span id="vr-'+last.window+'" class="m"></span> · the proof comes through this node from the Mint and is checked in this browser against the Mint\'s NYM; nothing is sent anywhere.</p>' : '';
    return '<h2>The Mint <small>who keeps my books</small></h2><div class="now">'+cells.map(function(c){ return '<div><span class="k">'+c[0]+'</span><span class="v">'+esc(c[1])+'</span><span class="s">'+esc(c[2])+'</span></div>'; }).join('')+'</div>'+verify;
  }

  function outbox(X, now){
    var D=BW.D, n=X.st.outbox||0, items=D.outbox||[];
    if(!n && !items.length) return '';
    return '<h2>The outbox <small>'+n+' record'+(n===1?'':'s')+' waiting for the Mint</small></h2>'+(items.length?C.outboxChart(items, now):'<div class="empty">'+n+' waiting; the node did not list them.</div>')+'<p class="note">Receipts and acks made while the Mint could not be reached wait here and go, in order, the next time it answers; the node retries on its own. Nothing is lost, and the windows settle when they arrive, which is the claim the paper makes for a link that comes and goes.</p>';
  }

  V.node=function(X, now){
    var purTitle = X.role==='payer' ? 'My purchases' : 'Purchases on my link';
    var purchases = X.role==='payer' ? S.purchases(X, now, purTitle) : (function(){ var ids={}; X.S.forEach(function(s){ if(s.purchase) ids[s.purchase]=1; }); var n=Object.keys(ids).length, prem=X.S.reduce(function(a,s){ return a+(s.premium_usd||0); },0); return n?'<h2>'+purTitle+' <small>as the Mint settled them</small></h2><div class="now"><div><span class="k">purchases</span><span class="v">'+n+'</span><span class="s">governing my windows</span></div><div><span class="k">premiums earned</span><span class="v">'+esc(money(prem))+'</span><span class="s">above my posted price</span></div></div>':''; })();
    return [S.now(X, now), S.attention(X, now), C.hourTimeline(X, now), outbox(X, now), myMonth(X, now), myLink(X, now), C.linkCards(X, now), S.pending(X, now), S.windows(X, now, 'My windows'), purchases, theMint(X, now), C.mapChart(X, now, null, [X.st.name])].join('');
  };
})(window.BW);
