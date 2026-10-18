/* The data layer: what the page holds, how it loads it (a snapshot embedded in the page, the node's own JSON, or a
   fleet of nodes), and how it derives the books as they stood at any moment (the as-of control). */
window.BW = window.BW || {};
(function(BW){
  'use strict';
  var H = 3600; BW.H = H;
  var D = BW.D = {
    mode: 'live',          // 'live' (this node), 'snapshot' (data embedded in the page), 'fleet' (many nodes)
    base: '',              // the origin the JSON comes from; '' is this node
    status: null, ledger: [], commitments: [], purchases: [], steward: null, map: null,
    links: [], acks: [], receipts: [], outbox: [], proofs: {},     // a node's own records (hop, payer)
    mint: null,            // for a hop or payer: the Mint's ledger and commitments, as proxied by the node
    nodes: [],             // fleet: [{url, name, status, steward, map, ledger, error}]
    snapshot: false,       // true when the data came embedded in the page (then `now` is the moment it was taken)
    asOf: null,            // null = now; else the Unix second the books are drawn as of
    fetchedAt: 0, since: 0, firstAt: 0, error: null
  };

  function parseJsonl(text){ var out=[]; String(text||'').split('\n').forEach(function(l){ l=l.trim(); if(!l) return; try{ out.push(JSON.parse(l)); }catch(e){} }); return out; }
  BW.parseJsonl = parseJsonl;

  function get(base, path, text){
    return fetch(base+path, {cache:'no-store'}).then(function(r){ if(!r.ok) throw new Error(path+' answered '+r.status); return text ? r.text() : r.json(); });
  }
  function opt(base, path, text, dflt){ return get(base, path, text).catch(function(){ return dflt; }); }
  BW.get = get;

  // ---------------------------------------------------------------- the three ways in
  BW.loadSnapshot = function(){
    var el=document.getElementById('snapshot'); if(!el || !el.textContent.trim()) return false;
    var s=JSON.parse(el.textContent);
    D.mode='snapshot'; D.snapshot=true; D.status=s.status||null;
    D.ledger=typeof s.ledger==='string'?parseJsonl(s.ledger):(s.ledger||[]);
    D.commitments=s.commitments||[]; D.purchases=typeof s.purchases==='string'?parseJsonl(s.purchases):(s.purchases||[]);
    D.steward=s.steward||null; D.map=s.map||null; D.proofs=s.proofs||{};   // proofs are kept as the Mint's own text, never re-serialised
    D.links=typeof s.links==='string'?parseJsonl(s.links):(s.links||[]);
    D.acks=typeof s.acks==='string'?parseJsonl(s.acks):(s.acks||[]);
    D.receipts=typeof s.receipts==='string'?parseJsonl(s.receipts):(s.receipts||[]);
    D.outbox=s.outbox||[];
    if(s.mint){ D.mint={ledger: typeof s.mint.ledger==='string'?parseJsonl(s.mint.ledger):(s.mint.ledger||[]), commitments: s.mint.commitments||[], name: s.mint.name||null}; }
    if(s.nodes){ D.mode='fleet'; D.nodes=s.nodes.map(function(n){ return {url:n.url, name:n.name, status:n.status||null, steward:n.steward||null, map:n.map||null, ledger: typeof n.ledger==='string'?parseJsonl(n.ledger):(n.ledger||[]), error:n.error||null, mine: n.mine!==false}; }); }
    D.fetchedAt=s.taken_at||(s.status&&s.status.time)||Math.floor(Date.now()/1000);
    return true;
  };

  BW.loadLive = function(){
    var b=D.base;
    return get(b, '/status').then(function(st){
      D.status=st; var role=st.role;
      var jobs=[];
      if(role==='mint'){
        jobs.push(get(b, '/ledger'+(D.since?'?since='+D.since:''), true).then(function(t){ var more=parseJsonl(t); D.ledger = D.since ? D.ledger.concat(more) : more; D.since = D.ledger.length ? (D.ledger[D.ledger.length-1].received_at||D.ledger[D.ledger.length-1].t||0) : 0; }));
        jobs.push(opt(b, '/commitments', false, []).then(function(v){ D.commitments=v||[]; }));
        jobs.push(opt(b, '/purchases', true, '').then(function(v){ D.purchases=typeof v==='string'?parseJsonl(v):(v||[]); }));
        jobs.push(opt(b, '/steward', false, null).then(function(v){ D.steward=v; }));
        jobs.push(opt(b, '/map', false, null).then(function(v){ D.map=v; }));
      } else {
        jobs.push(opt(b, '/acks', true, '').then(function(v){ D.acks=parseJsonl(v); }));
        jobs.push(opt(b, '/receipts', true, '').then(function(v){ D.receipts=parseJsonl(v); }));
        jobs.push(opt(b, '/links', true, '').then(function(v){ D.links=parseJsonl(v); }));
        jobs.push(opt(b, '/purchases', true, '').then(function(v){ D.purchases=parseJsonl(v); }));
        jobs.push(opt(b, '/outbox', false, []).then(function(v){ D.outbox=v||[]; }));
        jobs.push(opt(b, '/mint/ledger', true, '').then(function(t){ D.mint={ledger: parseJsonl(t), commitments: [], name: st.mint_name||null}; return opt(b, '/mint/commitments', false, []).then(function(c){ D.mint.commitments=c||[]; }); }));
        jobs.push(opt(b, '/map', false, null).then(function(v){ D.map=v; }));
      }
      return Promise.all(jobs);
    }).then(function(){ D.fetchedAt=Math.floor(Date.now()/1000); D.error=null; });
  };

  BW.loadFleet = function(urls){
    D.mode='fleet'; var dayAgo=Math.floor(Date.now()/1000)-86400;
    return Promise.all(urls.map(function(u){
      var n={url:u, name:null, status:null, steward:null, map:null, ledger:[], error:null, mine:true};
      return get(u, '/status').then(function(st){ n.status=st; n.name=st.name;
        if(st.role!=='mint') return;
        return Promise.all([opt(u, '/steward', false, null).then(function(v){ n.steward=v; }), opt(u, '/map', false, null).then(function(v){ n.map=v; }), opt(u, '/ledger?since='+dayAgo, true, '').then(function(t){ n.ledger=parseJsonl(t); })]);
      }).catch(function(e){ n.error=e.message; }).then(function(){ return n; });
    })).then(function(nodes){ D.nodes=nodes; D.fetchedAt=Math.floor(Date.now()/1000); });
  };

  // ---------------------------------------------------------------- derive the books as of a moment
  BW.now = function(){ return D.asOf || (D.snapshot ? D.fetchedAt : Math.floor(Date.now()/1000)); };

  function keyOf(r){ return r.payer+'|'+r.hop+'|'+r.t_first; }
  BW.keyOf = keyOf;

  // One derivation serves every view: it takes the ledger to read (the Mint's own, or the Mint's as a node sees it) and
  // the node's own records, and keeps only what existed at `now`.
  BW.derive = function(now){
    var st=D.status||{}, role=st.role||'mint', me=st.nym||null;
    var L = role==='mint' ? D.ledger : (D.mint ? D.mint.ledger : []);
    var at=function(e){ return e.received_at || e.t || 0; };
    L = L.filter(function(e){ return at(e) <= now; });
    var names={};
    L.forEach(function(e){ if(e.kind==='receipt'&&e.receipt) names[e.receipt.payer]=e.from; if(e.kind==='ack'&&e.receipt) names[e.receipt.hop]=e.from; });
    D.receipts.forEach(function(e){ if(e.receipt) names[e.receipt.payer]=e.from; }); D.acks.forEach(function(e){ if(e.receipt) names[e.receipt.hop]=e.from; });
    if(role==='mint'&&st.nym) names[st.nym]=st.name; if(st.steward) names[st.steward]='steward';
    if(role!=='mint'&&me) names[me]=st.name;
    if(D.map&&D.map.nodes){ /* names of places are looked up by the views */ }
    var name=function(nym){ return names[nym] || (nym ? String(nym).replace(/^ed25519:/,'').slice(0,8)+'…' : '–'); };
    var S=L.filter(function(e){ return e.kind==='settlement'; }), R=L.filter(function(e){ return e.kind==='receipt'; }), A=L.filter(function(e){ return e.kind==='ack'; });
    var C=(role==='mint' ? (D.commitments&&D.commitments.length?D.commitments:L.filter(function(e){ return e.kind==='commitment'; })) : (D.mint?D.mint.commitments:[])).filter(function(c){ return (c.t||0)<=now; });
    if(role!=='mint' && me){ S=S.filter(function(s){ return s.payer===me||s.hop===me; }); R=R.filter(function(r){ return r.receipt.payer===me||r.receipt.hop===me; }); A=A.filter(function(a){ return a.receipt.payer===me||a.receipt.hop===me; }); }
    var myR = role==='payer' ? D.receipts.filter(function(r){ return at(r)<=now || !r.received_at; }) : [];
    var myA = role==='hop' ? D.acks.filter(function(a){ return at(a)<=now || !a.received_at; }) : (role==='payer' ? D.acks.filter(function(a){ return at(a)<=now || !a.received_at; }) : []);
    var acked={}; A.forEach(function(a){ acked[keyOf(a.receipt)]=a; }); myA.forEach(function(a){ if(!acked[keyOf(a.receipt)]) acked[keyOf(a.receipt)]=a; });
    var settledKey={}; S.forEach(function(s){ settledKey[keyOf(s)]=s; });
    var recByKey={}; R.forEach(function(r){ recByKey[keyOf(r.receipt)]=r; }); myR.forEach(function(r){ if(!recByKey[keyOf(r.receipt)]) recByKey[keyOf(r.receipt)]=r; });
    var pending=Object.keys(recByKey).filter(function(k){ return !settledKey[k]; }).map(function(k){ var r=recByKey[k]; return {r:r, ack:acked[k]||null, lost:(now-(r.received_at||r.receipt.t_last))>2*H}; });
    var links={}; S.forEach(function(s){ var k=s.payer+'>'+s.hop; (links[k]=links[k]||{payer:s.payer,hop:s.hop,windows:[]}).windows.push(s); });
    var lastSeen={}; R.concat(A).forEach(function(e){ if(e.from&&e.received_at) lastSeen[e.from]=Math.max(lastSeen[e.from]||0, e.received_at); });
    var place={}; if(D.map&&D.map.nodes) for(var k in D.map.nodes) place[k]=D.map.nodes[k];
    var peers={};
    for(var n in names){ var nm=names[n]; if(nm==='steward'||(role==='mint'&&n===st.nym)) continue; peers[n]={nym:n,name:nm,roles:{},windows:0,lastSeen:lastSeen[nm]||0,place:place[nm]||null}; }
    S.forEach(function(s){ if(peers[s.payer]){ peers[s.payer].roles.payer=1; peers[s.payer].windows++; } if(peers[s.hop]){ peers[s.hop].roles.hop=1; peers[s.hop].windows++; } });
    var paidOn={}; S.forEach(function(s){ if(s.purchase){ var p=paidOn[s.purchase]=paidOn[s.purchase]||{premium:0,refund:0,windows:0,steward:0,rows:[]}; p.premium+=s.premium_usd||0; p.refund+=s.refund_usd||0; p.steward+=s.from_steward_usd||0; p.windows++; p.rows.push(s); } });
    // purchases: the records as posted, with closes and top-ups applied as of `now`
    var recs=(D.purchases||[]).filter(function(p){ return (p.posted_at||0)<=now; }), byId={};
    recs.forEach(function(p){ if(p.kind==='purchase'){ var q=Object.assign({}, p); q.escrow_posted=p.escrow; q.topups=[]; q.closed=false; byId[q.id]=q; } });
    recs.forEach(function(p){ var q=byId[p.purchase]; if(!q) return; if(p.kind==='close'){ q.closed=true; q.closed_at=p.posted_at; } else if(p.kind==='top-up'){ q.escrow=(q.escrow||0)+(p.escrow||0); q.topups.push(p); } });
    var purchases=Object.keys(byId).map(function(k){ return byId[k]; });
    var firstAt=0; L.concat(myR, myA).forEach(function(e){ var t=at(e)||(e.receipt&&e.receipt.t_first)||0; if(t && (!firstAt || t<firstAt)) firstAt=t; }); D.firstAt=firstAt;
    return { st:st, role:role, me:me, names:names, name:name, S:S, R:R, A:A, C:C, myR:myR, myA:myA, pending:pending, links:links, peers:peers, paidOn:paidOn, place:place, recByKey:recByKey, acked:acked, settledKey:settledKey, purchases:purchases, now:now };
  };
})(window.BW);
