/* Formatting, and the rules: what needs attention, judged against fixed limits and against the node's own history. */
window.BW = window.BW || {};
(function(BW){
  'use strict';
  var H=BW.H, F=BW.fmt={};
  F.esc=function(s){ return String(s==null?'':s).replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); };
  F.pad=function(n){ return (n<10?'0':'')+n; };
  F.tstamp=function(t){ if(!t) return '–'; var d=new Date(t*1000); return F.pad(d.getMonth()+1)+'-'+F.pad(d.getDate())+' '+F.pad(d.getHours())+':'+F.pad(d.getMinutes()); };
  F.hm=function(t){ var d=new Date(t*1000); return F.pad(d.getHours())+':'+F.pad(d.getMinutes()); };
  F.hms=function(t){ var d=new Date(t*1000); return F.hm(t)+':'+F.pad(d.getSeconds()); };
  F.ago=function(t, now){ if(!t) return 'never'; var s=Math.max(0, now-t); if(s<90) return s+' s ago'; if(s<5400) return Math.round(s/60)+' min ago'; if(s<172800) return (s/3600).toFixed(1)+' h ago'; return Math.round(s/86400)+' d ago'; };
  F.dur=function(s){ s=Math.max(0,Math.round(s)); if(s<90) return s+' s'; if(s<5400) return Math.round(s/60)+' min'; if(s<172800) return (s/3600).toFixed(1)+' h'; return Math.round(s/86400)+' d'; };
  F.bytes=function(n){ if(n==null) return '–'; if(n>=1e9) return (n/1e9).toFixed(2)+' GB'; if(n>=1e6) return (n/1e6).toFixed(2)+' MB'; if(n>=1e3) return (n/1e3).toFixed(1)+' kB'; return n+' B'; };
  F.shortBytes=function(n){ if(n>=1e9) return (n/1e9).toFixed(n>=1e10?0:1)+' GB'; if(n>=1e6) return Math.round(n/1e6)+' MB'; if(n>=1e3) return Math.round(n/1e3)+' kB'; return Math.round(n)+' B'; };
  // Money by magnitude: dollars to two places from a dollar up, cents to two places from a mill up, cents to three below.
  F.money=function(x){ if(x==null||isNaN(x)) return '–'; var a=Math.abs(x), sign=x<0?'−':''; if(a===0) return '0'; if(a>=1) return sign+'$'+a.toFixed(2); if(a>=0.001) return sign+(a*100).toFixed(2)+'¢'; return sign+(a*100).toFixed(3)+'¢'; };
  F.usd=F.money;
  F.pct=function(x, d){ return (100*x).toFixed(d==null?1:d)+'%'; };
  F.sortBy=function(arr, key, desc){ return arr.slice().sort(function(a,b){ var x=key(a), y=key(b); return x<y ? (desc?1:-1) : x>y ? (desc?-1:1) : 0; }); };
  F.median=function(a){ if(!a.length) return 0; var s=a.slice().sort(function(x,y){ return x-y; }); return s[Math.floor(s.length/2)]; };
  F.gapOf=function(s){ return s.payer_bytes ? (s.hop_bytes-s.payer_bytes)/s.payer_bytes : 0; };
  F.rateOf=function(s){ var d=s.t_last-s.t_first; return d>0 ? s.settled_bytes*8/d/1e6 : 0; };

  // ---------------------------------------------------------------- baselines: what is normal for this node, from its last week
  BW.baselines=function(X, now){
    var week=now-7*86400, b={links:{}, arrival:null, close:null};
    for(var k in X.links){ var W=X.links[k].windows.filter(function(s){ return s.t_first>=week; }); if(W.length<6) continue;
      var gaps=W.map(F.gapOf), med=F.median(gaps), mad=F.median(gaps.map(function(g){ return Math.abs(g-med); }))||0.005;
      var rates=W.map(F.rateOf); b.links[k]={gap:med, mad:mad, rate:F.median(rates), n:W.length}; }
    var arr=X.R.filter(function(r){ return r.received_at && r.receipt.t_last>=week; }).map(function(r){ return r.received_at-r.receipt.t_last; });
    if(arr.length>=6) b.arrival=F.median(arr);
    var byW={}; X.S.forEach(function(s){ byW[Math.floor(s.t_first/H)*H]=s; });
    var closes=X.C.filter(function(c){ return c.window>=week && byW[c.window]; }).map(function(c){ return c.t-(c.window+H); });
    if(closes.length>=6) b.close=F.median(closes);
    return b;
  };

  BW.marginOf=function(l, inForce){ if(inForce&&inForce[l.hop]&&inForce[l.hop].margin!=null) return inForce[l.hop].margin; var newest=F.sortBy(l.windows, function(s){ return s.t_first; }, true)[0]; return (newest&&newest.margin!=null) ? newest.margin : 0.01; };

  // ---------------------------------------------------------------- attention: bwkit's health verdicts, then the page's own
  BW.attention=function(X, now){
    var D=BW.D, st=X.st, items=[], live=D.mode!=='snapshot', role=X.role;
    var add=function(level, text){ items.push({level:level, text:text}); };
    ((st.health&&st.health.items)||[]).forEach(function(i){ add(i.level||'warn', F.esc(i.text)); });   // bwkit's texts are plain; the page's own are HTML
    var have={}; items.forEach(function(i){ have[i.text]=1; });
    if(live && st.time && now-st.time>120 && !D.asOf) add('bad','The node\'s status is '+F.ago(st.time, now)+'; the service may be down.');
    if(st.clock_offset_s!=null && Math.abs(st.clock_offset_s)>1) add('bad','The clock is '+st.clock_offset_s.toFixed(2)+' s off its source; receipts are declined beyond the link\'s tolerance.');
    if(st.disk_free_bytes!=null && st.disk_free_bytes<5e8) add('bad','Only '+F.bytes(st.disk_free_bytes)+' of disk is free for the ledger.');
    if(role==='mint'){
      var hours={}; X.S.forEach(function(s){ var w=Math.floor(s.t_first/H)*H; (hours[w]=hours[w]||[]).push(s); });
      var committed={}; X.C.forEach(function(c){ committed[c.window]=c; });
      var firstC=X.C.length?Math.min.apply(null, X.C.map(function(c){ return c.window; })):Infinity, cur=Math.floor(now/H)*H;
      Object.keys(hours).map(Number).sort().forEach(function(w){ if(w>=firstC && w<cur && !committed[w] && now-w>H+300) add('bad','The '+F.tstamp(w)+' hour has '+hours[w].length+' settlement'+(hours[w].length===1?'':'s')+' and no commitment.'); });
    }
    X.pending.forEach(function(p){ if(!p.ack && !p.lost && now-(p.r.received_at||p.r.receipt.t_last)>600) add('warn','A receipt'+(p.r.from?' from '+F.esc(p.r.from):'')+' for the '+F.tstamp(p.r.receipt.t_first)+' window has waited '+F.dur(now-(p.r.received_at||p.r.receipt.t_last))+' for the hop\'s countersignature.'); });
    var lastR=(X.R.length?X.R[X.R.length-1].received_at:0)||(X.myR.length?X.myR[X.myR.length-1].receipt.t_last:0);
    if(lastR && now-lastR>2*H) add('warn','No receipt for '+F.dur(now-lastR)+'; one is due at the top of every hour.');
    if(st.last_report_at!=null && role==='mint' && now-st.last_report_at>600) add('warn','The public status was last posted '+F.ago(st.last_report_at, now)+'.');
    if(st.outbox && st.outbox>0 && role!=='mint') add(st.outbox>2?'bad':'warn', st.outbox+(st.outbox===1?' record waits':' records wait')+' in the outbox for the Mint; nothing is lost, they go when it answers.');
    if(role==='hop' && st.patience) for(var p in st.patience){ var ps=st.patience[p]; if(ps.declined) add('bad','Payer '+F.esc(X.name(p))+' is declined since '+F.tstamp(ps.declined_at)+' after '+ps.short+' short windows; service to it has stopped.'); else if(ps.short>0) add('info','Payer '+F.esc(X.name(p))+' has '+ps.short+' window'+(ps.short===1?'':'s')+' short beyond the margin against a patience of '+((st.link&&st.link.patience_windows)||0)+'.'); }
    // the margin against the gap, and the baselines
    var inForce={}; (st.links||[]).forEach(function(l){ inForce[l.relay]=l; }); if(st.link&&st.nym) inForce[st.nym]=st.link;
    var B=BW.baselines(X, now);
    for(var k in X.links){ var l=X.links[k], W=F.sortBy(l.windows, function(s){ return s.t_first; }, true).slice(0,24); if(W.length<3) continue;
      var m=BW.marginOf(l, inForce), gaps=W.map(F.gapOf), med=F.median(gaps), beyond=W.filter(function(s){ return s.status==='disputed'; }).length;
      if(med>m) add('warn',F.esc(X.name(l.payer))+' → '+F.esc(X.name(l.hop))+': the gap runs at '+(med>=0?'+':'')+F.pct(med)+' over the last '+W.length+' windows against a margin of '+F.pct(m,0)+', so '+beyond+' of them settled disputed; raise the margin in the link\'s announcement or mend the path.');
      var b=B.links[k]; if(b){ var last3=W.slice(0,3);
        var g3=F.median(last3.map(F.gapOf)); if(g3>b.gap+Math.max(3*b.mad, 0.02) && g3>m) add('warn',F.esc(X.name(l.payer))+' → '+F.esc(X.name(l.hop))+': the gap has moved to '+(g3>=0?'+':'')+F.pct(g3)+' from its usual '+(b.gap>=0?'+':'')+F.pct(b.gap)+' over the week.');
        var r3=F.median(last3.map(F.rateOf)); if(b.rate>0 && r3<0.5*b.rate) add('warn',F.esc(X.name(l.payer))+' → '+F.esc(X.name(l.hop))+': the rate has fallen to '+r3.toFixed(2)+' Mbit/s from its usual '+b.rate.toFixed(2)+'.'); } }
    if(B.arrival!=null && X.R.length){ var r=X.R[X.R.length-1], late=r.received_at-r.receipt.t_last; if(late>Math.max(5*B.arrival, B.arrival+10)) add('info','The last receipt arrived '+F.dur(late)+' after its window closed; they usually arrive in '+F.dur(B.arrival)+'.'); }
    if(B.close!=null && X.C.length){ var c=X.C[X.C.length-1], took=c.t-(c.window+H); if(took>Math.max(5*B.close, B.close+30)) add('info','The last hour took '+F.dur(took)+' to commit; they usually take '+F.dur(B.close)+'.'); }
    var openFirst={}; X.purchases.forEach(function(p){ if(p.kind==='purchase'&&p.program==='first_link'&&p.deadline>=now&&!p.closed) openFirst[p.route.to]=(openFirst[p.route.to]||0)+1; });
    Object.keys(openFirst).forEach(function(k){ if(openFirst[k]>1) add('warn',openFirst[k]+' first-link offers are open for '+F.esc(k)+'; the rule should hold one standing offer and top it up.'); });
    var seen={}; return items.filter(function(i){ if(seen[i.text]) return false; seen[i.text]=1; return true; }).sort(function(a,b){ var o={bad:0,warn:1,info:2}, r=function(l){ return l in o ? o[l] : 3; }; return r(a.level)-r(b.level); });
  };
})(window.BW);
