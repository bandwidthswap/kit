/* The shell: decides how the page is fed (a snapshot, this node, a fleet), draws the view for the role, runs the as-of
   control, the drill-downs and the in-page verification, and refreshes a live page every minute. */
window.BW = window.BW || {};
(function(BW){
  'use strict';
  var D=BW.D, F=BW.fmt, $=function(id){ return document.getElementById(id); };
  var open={};   // which drill-down rows are open, by key, so a redraw keeps them

  function header(X){
    var st=X.st||{}, fleet=D.mode==='fleet';
    $('h-name').textContent = fleet ? 'the fleet' : (st.name||'bwkit'); $('h-role').textContent = fleet ? (D.nodes.length+' nodes') : (st.role||'node');
    $('h-nym').textContent = fleet ? '' : (st.nym||''); $('h-ver').textContent = fleet ? '' : (st.impl||(st.bwkit?'bwkit/'+st.bwkit:''));
    $('h-at').textContent = (D.snapshot?'taken ':'read ')+F.tstamp(D.fetchedAt); $('h-snap').hidden = !D.snapshot;
    $('h-fleet').hidden = fleet || D.snapshot; $('h-snapshot').hidden = fleet || D.snapshot;
    document.title = (fleet?'the fleet':(st.name||'bwkit'))+' · '+(fleet?'fleet':(st.role||'node'));
  }

  function asofBar(){
    var bar=$('asof'); if(D.mode==='fleet' || !D.firstAt){ bar.hidden=true; return; }
    bar.hidden=false; var now=D.snapshot?D.fetchedAt:Math.floor(Date.now()/1000), r=$('asof-range');
    r.min=String(Math.floor(D.firstAt/3600)*3600); r.max=String(now); r.step='60'; if(!r.dataset.set){ r.value=String(now); r.dataset.set='1'; }
    $('asof-label').textContent = D.asOf ? ('as of '+F.tstamp(D.asOf)) : 'now';
  }

  function draw(){
    var now=BW.now(), html='';
    if(D.mode==='fleet'){ header({st:{}}); html=BW.views.fleet(now); }
    else { var X=BW.derive(now); BW.X=X; header(X); html = X.role==='mint' ? BW.views.mint(X, now) : BW.views.node(X, now); }
    $('sections').innerHTML=html; asofBar();
    Object.keys(open).forEach(function(k){ if(open[k]) toggle(k, true); });
  }

  // ---------------------------------------------------------------- drill-downs
  // The detail is a row under its own row; its content sticks to the table's left edge, so it stays in view while the
  // table scrolls sideways and is never cut off.
  function toggle(key, force){
    var row=document.querySelector('tr.open[data-win="'+CSS.escape(key)+'"], tr.open[data-pur="'+CSS.escape(key)+'"]'); if(!row) return;
    var next=row.nextElementSibling, isOpen=next&&next.classList.contains('detail');
    if(isOpen && !force){ next.remove(); open[key]=false; row.classList.remove('selected'); return; }
    if(isOpen) return;
    var box=row.closest('.tbl'), X=BW.X, html = row.dataset.win ? BW.sec.windowDetail(X, key) : BW.sec.purchaseDetail(X, key);
    var label = row.dataset.win ? 'the '+F.tstamp(+key.split('|')[2])+' window: '+F.esc(X.name(key.split('|')[0]))+' → '+F.esc(X.name(key.split('|')[1])) : 'purchase '+F.esc(key.slice(0,8));
    var tr=document.createElement('tr'); tr.className='detail'; tr.innerHTML='<td colspan="'+row.children.length+'"><div class="detail-in" style="width:'+Math.max(280, (box?box.clientWidth:600)-26)+'px"><div class="detail-head"><span>'+label+'</span><button class="btn" data-close="'+F.esc(key)+'">close</button></div>'+html+'</div></td>';
    row.after(tr); row.classList.add('selected'); open[key]=true;
  }

  // ---------------------------------------------------------------- verification in the page
  function verify(window_){
    var out=$('vr-'+window_); if(out) out.textContent='checking…';
    // a proof is checked as the bytes the Mint wrote: re-serialising it would rewrite its floats and change every leaf
    var p=D.proofs[window_], get = D.snapshot ? Promise.resolve(p ? (typeof p==='string' ? p : JSON.stringify(p)) : null)
            : fetch(D.base+(D.status&&D.status.role==='mint'?'/proof?window=':'/mint/proof?window=')+window_, {cache:'no-store'}).then(function(r){ return r.ok?r.text():null; });
    return get.then(function(text){
      if(!text){ if(out) out.textContent='no proof held for this hour'; return; }
      if(!window.bsp){ if(out) out.textContent='the verifier did not load'; return; }
      return window.bsp.verifyProof(text).then(function(r){
        if(!out) return; var n=(r.settlements||[]).length, okN=(r.settlements||[]).filter(function(s){ return s.ok; }).length;
        out.innerHTML = r.ok ? '<span class="st ok">verified</span> '+okN+' of '+n+' settlement'+(n===1?'':'s')+' in the root, root signed by the Mint' : '<span class="st declined">failed</span> '+F.esc(r.reason||(r.commitment&&r.commitment.reason)||((r.settlements||[]).map(function(s){ return s.reason; }).filter(Boolean)[0])||'');
      });
    }).catch(function(e){ if(out) out.textContent='could not check: '+e.message; });
  }

  // ---------------------------------------------------------------- events
  document.addEventListener('click', function(e){
    var b=e.target.closest('button'); if(b){
      if(b.dataset.verify){ verify(+b.dataset.verify); return; }
      if(b.id==='asof-now'){ D.asOf=null; $('asof-range').value=$('asof-range').max; draw(); return; }
      if(b.id==='fleet-add'){ var u=$('fleet-url').value.trim().replace(/\/+$/,''); if(!u) return; var list=BW.fleetList(); if(list.indexOf(u)<0) list.push(u); BW.fleetSave(list); BW.loadFleet(list).then(draw); return; }
      if(b.dataset.close){ toggle(b.dataset.close); return; }
      if(b.dataset.remove){ var l2=BW.fleetList().filter(function(x){ return x!==b.dataset.remove; }); BW.fleetSave(l2); BW.loadFleet(l2).then(draw); return; }
    }
    var tr=e.target.closest('tr.open'); if(tr && !e.target.closest('a,button')){ toggle(tr.dataset.win||tr.dataset.pur); }
  });
  var asofTimer=null;
  document.addEventListener('input', function(e){ if(e.target.id!=='asof-range') return; var v=+e.target.value, max=+e.target.max; D.asOf = v>=max ? null : v; $('asof-label').textContent = D.asOf?('as of '+F.tstamp(D.asOf)):'now'; clearTimeout(asofTimer); asofTimer=setTimeout(draw, 120); });

  // ---------------------------------------------------------------- boot
  function fail(msg){ var el=$('err'); el.hidden=false; el.textContent=msg; }
  function start(){
    var fleet = /\/fleet\/?$/.test(location.pathname) || new URLSearchParams(location.search).has('fleet') || new URLSearchParams(location.search).has('nodes');
    if(BW.loadSnapshot()){ draw(); return; }
    if(fleet){ D.mode='fleet'; var list=BW.fleetList(); BW.loadFleet(list).then(draw); setInterval(function(){ BW.loadFleet(BW.fleetList()).then(draw, function(){}); }, 60000); return; }
    BW.loadLive().then(function(){ $('err').hidden=true; draw(); }, function(e){ fail('Could not read the node: '+e.message+'. This page is served by bwkit itself; open it at the node\'s address and port.'); });
    setInterval(function(){ if(D.asOf) return; BW.loadLive().then(draw, function(){}); }, 60000);
  }
  start();
})(window.BW);
