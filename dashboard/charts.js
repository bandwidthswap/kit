/* The visuals: inline SVG drawn by the page, nothing loaded from anywhere. Every chart takes the derived books X and
   the moment `now` they are drawn for, and returns HTML. */
window.BW = window.BW || {};
(function(BW){
  'use strict';
  var H=BW.H, F=BW.fmt, C=BW.charts={};
  var esc=F.esc, bytes=F.bytes, money=F.money, pct=F.pct, tstamp=F.tstamp, hm=F.hm, pad=F.pad, sortBy=F.sortBy, gapOf=F.gapOf, rateOf=F.rateOf;
  var PAL=['var(--accent)','var(--info)','#8a6fcf','var(--warn)'];
  C.col=function(i){ return PAL[i%PAL.length]; };
  function svgo(w,h){ return '<svg viewBox="0 0 '+w+' '+h+'" role="img">'; }
  C.linkOrder=function(X){ return Object.keys(X.links).sort(); };
  function chip(x, y, text, cls, anchor){
    var w=text.length*6.6+14, h=18, left=anchor==='start'?x:anchor==='end'?x-w:x-w/2; left=Math.max(4, Math.min(960-w-4, left));
    return '<g class="chip '+(cls||'')+'"><rect x="'+left.toFixed(1)+'" y="'+(y-13)+'" width="'+w.toFixed(1)+'" height="'+h+'" rx="4"/><text x="'+(left+w/2).toFixed(1)+'" y="'+y+'" text-anchor="middle">'+esc(text)+'</text></g>';
  }
  C.chip=chip;

  // The hour: the window being metered, then the minutes after it in which the books close; the events listed under it.
  C.hourTimeline=function(X, now){
    var R=X.R.length?X.R:X.myR; if(!R.length) return '';
    var last=R[R.length-1].receipt, w0=last.t_first-(last.t_first%H), w1=w0+H, cur=Math.floor(now/H)*H, isCur=(cur===w0);
    var W=960, Hh=76, L=10, Rm=10, split=W*0.62, after=600;
    var xw=function(t){ return L+Math.max(0,Math.min(1,(t-w0)/H))*(split-L); }, xa=function(t){ return split+12+Math.max(0,Math.min(1,(t-w1)/after))*(W-Rm-split-12); };
    var out=svgo(W,Hh);
    out+='<rect class="span" x="'+L+'" y="30" width="'+(split-L)+'" height="18"/><text class="lbl" x="'+L+'" y="22">the '+hm(w0)+' window, metered</text><text class="ax" x="'+split+'" y="22" text-anchor="end">'+hm(w1)+'</text>';
    out+='<rect class="span after" x="'+(split+12)+'" y="30" width="'+(W-Rm-split-12)+'" height="18"/><text class="lbl" x="'+(split+12)+'" y="22">the ten minutes after, in which the hour closes</text>';
    var ev=[], key=BW.keyOf;
    R.filter(function(r){ return r.receipt.t_first>=w0 && r.receipt.t_first<w1; }).forEach(function(r){
      ev.push([r.received_at||r.receipt.t_last, (X.role==='payer'?'receipt signed':'receipt from '+r.from),'']);
      var a=X.acked[key(r.receipt)]; if(a) ev.push([a.received_at||r.receipt.t_last, (X.role==='hop'?'countersigned':'ack from '+a.from),'']);
      var s=X.settledKey[key(r.receipt)]; if(s) ev.push([s.t,'settled '+bytes(s.settled_bytes)+(s.status==='disputed'?', disputed':''),'']);
    });
    X.C.filter(function(c){ return c.window===w0; }).forEach(function(c){ ev.push([c.t,'commitment, '+c.count+' settlement'+(c.count===1?'':'s'),'']); });
    var sw=BW.D.steward; if(sw&&sw.last_run&&sw.last_run>=w1&&sw.last_run<w1+after) ev.push([sw.last_run,'the Steward ran','steward']);
    ev.sort(function(a,b){ return a[0]-b[0]; });
    ev.forEach(function(e){ var x=e[0]<w1?xw(e[0]):xa(e[0]); out+='<line class="tick '+e[2]+'" x1="'+x.toFixed(1)+'" x2="'+x.toFixed(1)+'" y1="28" y2="54"/>'; });
    if(now>=w0 && now<w1+after){ var xn=now<w1?xw(now):xa(now); out+='<line class="tick now" x1="'+xn.toFixed(1)+'" x2="'+xn.toFixed(1)+'" y1="24" y2="58"/><text class="ax" x="'+xn.toFixed(1)+'" y="70" text-anchor="middle">now</text>'; }
    out+='</svg>';
    var list=ev.length?'<table style="width:auto;margin-top:6px"><tbody>'+ev.map(function(e){ return '<tr><td class="m">'+F.hms(e[0])+'</td><td>'+esc(e[1])+'</td></tr>'; }).join('')+'</tbody></table>':'<p class="cap">nothing has happened in this window yet</p>';
    var next=isCur?('this window closes at '+hm(w1)+'; the receipt, the ack, the settlement and the commitment follow within seconds, the Steward at five past'):('the '+hm(cur)+' window is being metered now; its receipt is due at '+hm(cur+H));
    return '<h2>This hour <small>how a window becomes a settled, committed hour</small></h2><div class="chart">'+out+list+'<p class="cap">'+next+'.</p></div>';
  };

  // Time to close: receipt to commitment for each of the last 48 hours; a path that is degrading shows here first.
  C.closeChart=function(X, now){
    var byW={}; X.S.forEach(function(s){ byW[Math.floor(s.t_first/H)*H]=s; });
    var end=Math.floor(now/H)*H-H, start=end-47*H, rows=[];
    for(var h=start; h<=end; h+=H){ var c=X.C.filter(function(c){ return c.window===h; })[0]; var rs=X.R.filter(function(r){ return Math.floor(r.receipt.t_first/H)*H===h; }); var first=rs.length?Math.min.apply(null, rs.map(function(r){ return r.received_at||r.receipt.t_last; })):null; rows.push({h:h, took: c&&first!=null ? Math.max(0, c.t-(h+H)) : null, recv: first!=null ? first-(h+H) : null}); }
    if(!rows.some(function(r){ return r.took!=null; })) return '';
    var W=960, Hh=120, L=58, R=12, T=10, B=24, max=Math.max(5, Math.max.apply(null, rows.map(function(r){ return r.took||0; }))), slow=Math.max(30, 5*F.median(rows.filter(function(r){ return r.took!=null; }).map(function(r){ return r.took; })));
    var y=function(v){ return T+(Hh-T-B)*(1-v/max); }, x=function(i){ return L+i/48*(W-L-R); }, bw=(W-L-R)/48*0.8;
    var out=svgo(W,Hh);
    [max/2, max].forEach(function(v){ out+='<line class="grid" x1="'+L+'" x2="'+(W-R)+'" y1="'+y(v).toFixed(1)+'" y2="'+y(v).toFixed(1)+'"/><text class="ax" x="'+(L-6)+'" y="'+(y(v)+4).toFixed(1)+'" text-anchor="end">'+F.dur(v)+'</text>'; });
    rows.forEach(function(r,i){ if(r.took==null) return; out+='<rect class="close'+(r.took>slow?' slow':'')+'" x="'+x(i).toFixed(1)+'" y="'+y(r.took).toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+Math.max(1,(Hh-B)-y(r.took)).toFixed(1)+'"><title>'+tstamp(r.h)+': receipt '+F.dur(r.recv)+' after the hour, committed after '+F.dur(r.took)+'</title></rect>'; });
    out+='<line class="zero" x1="'+L+'" x2="'+(W-R)+'" y1="'+(Hh-B)+'" y2="'+(Hh-B)+'"/><text class="ax" x="'+L+'" y="'+(Hh-8)+'">'+tstamp(start)+'</text><text class="ax" x="'+(W-R)+'" y="'+(Hh-8)+'" text-anchor="end">'+tstamp(end)+'</text></svg>';
    return '<div class="chart" style="margin-top:10px">'+out+'<p class="cap">time from the hour\'s end to its commitment, the last 48 hours; orange is five times the usual</p></div>';
  };

  // Seven days of traffic: settled bytes per hour, one colour per link, stacked; a disputed window is outlined.
  C.trafficChart=function(X, now){
    var links=C.linkOrder(X); if(!links.length) return '';
    var end=Math.floor(now/H)*H, start=end-167*H, W=960, Hh=190, L=84, R=12, T=12, B=28;
    var by={}; links.forEach(function(k){ X.links[k].windows.forEach(function(s){ var h=Math.floor(s.t_first/H)*H; if(h<start||h>end) return; (by[h]=by[h]||{})[k]=s; }); });
    var max=1; Object.keys(by).forEach(function(h){ var tot=0; links.forEach(function(k){ if(by[h][k]) tot+=by[h][k].settled_bytes; }); if(tot>max) max=tot; });
    var x=function(h){ return L+(h-start)/(168*H)*(W-L-R); }, bw=(W-L-R)/168*0.9, y=function(v){ return T+(Hh-T-B)*(1-v/max); };
    var out=svgo(W,Hh);
    for(var g=1; g<=4; g++){ var v=max*g/4; out+='<line class="grid" x1="'+L+'" x2="'+(W-R)+'" y1="'+y(v).toFixed(1)+'" y2="'+y(v).toFixed(1)+'"/><text class="ax" x="'+(L-6)+'" y="'+(y(v)+4).toFixed(1)+'" text-anchor="end">'+F.shortBytes(v)+'</text>'; }
    for(var h=start; h<=end; h+=H){ var d=new Date(h*1000); if(d.getHours()===0){ out+='<line class="grid" x1="'+x(h).toFixed(1)+'" x2="'+x(h).toFixed(1)+'" y1="'+T+'" y2="'+(Hh-B)+'"/><text class="ax" x="'+(x(h)+3).toFixed(1)+'" y="'+(Hh-10)+'">'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'</text>'; } }
    Object.keys(by).forEach(function(h){ var acc=0; links.forEach(function(k,i){ var s=by[h][k]; if(!s) return; var top=y(acc+s.settled_bytes), bot=y(acc); out+='<rect class="bar'+(s.status==='disputed'?' disputed':'')+'" x="'+x(+h).toFixed(1)+'" y="'+top.toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+Math.max(1,bot-top).toFixed(1)+'" fill="'+C.col(i)+'"><title>'+tstamp(s.t_first)+' '+esc(X.name(s.payer))+' → '+esc(X.name(s.hop))+' '+bytes(s.settled_bytes)+' '+s.status+'</title></rect>'; acc+=s.settled_bytes; }); });
    out+='<line class="zero" x1="'+L+'" x2="'+(W-R)+'" y1="'+(Hh-B)+'" y2="'+(Hh-B)+'"/></svg>';
    var legend=links.map(function(k,i){ var l=X.links[k]; return '<i style="background:'+C.col(i)+'"></i>'+esc(X.name(l.payer))+' → '+esc(X.name(l.hop)); }).join('')+'<i style="background:transparent;border:1.5px solid var(--warn)"></i>disputed';
    return '<h2>Traffic <small>the last seven days, settled bytes per hour</small></h2><div class="chart">'+out+'<p class="cap legend">'+legend+'</p></div>';
  };

  // Per link: the gap between the two counts against the margin in force, and the rate against the purchase's minimum.
  C.gapChart=function(l, margin){
    var Wd=sortBy(l.windows, function(s){ return s.t_first; }).slice(-96); if(Wd.length<2) return '';
    var W=440, Hh=130, L=46, R=8, T=10, B=24, raw=Wd.map(gapOf), gaps=raw.map(function(g){ return Math.max(-1, Math.min(1, g)); }), beyond=raw.filter(function(g){ return g>1||g<-1; }).length;
    var lo=Math.min(-margin*1.2, Math.min.apply(null,gaps)), hi=Math.max(margin*1.6, Math.max.apply(null,gaps)); if(hi-lo<0.01) hi=lo+0.01;
    var y=function(v){ return T+(Hh-T-B)*(1-(v-lo)/(hi-lo)); }, x=function(i){ return L+(i+0.5)/Wd.length*(W-L-R); }, med=F.median(raw);
    var out=svgo(W,Hh);
    out+='<rect class="band" x="'+L+'" y="'+y(margin).toFixed(1)+'" width="'+(W-L-R)+'" height="'+(y(-margin)-y(margin)).toFixed(1)+'"/>';
    [lo,0,margin,hi].forEach(function(v){ out+='<line class="grid" x1="'+L+'" x2="'+(W-R)+'" y1="'+y(v).toFixed(1)+'" y2="'+y(v).toFixed(1)+'"/><text class="ax" x="'+(L-4)+'" y="'+(y(v)+4).toFixed(1)+'" text-anchor="end">'+(v>0?'+':'')+pct(v,0)+'</text>'; });
    out+='<line class="zero" x1="'+L+'" x2="'+(W-R)+'" y1="'+y(0).toFixed(1)+'" y2="'+y(0).toFixed(1)+'"/><line class="mean" x1="'+L+'" x2="'+(W-R)+'" y1="'+y(Math.max(-1,Math.min(1,med))).toFixed(1)+'" y2="'+y(Math.max(-1,Math.min(1,med))).toFixed(1)+'"/>';
    Wd.forEach(function(s,i){ var g=gapOf(s), c=Math.max(-1,Math.min(1,g)); out+='<circle class="dot '+s.status+'" cx="'+x(i).toFixed(1)+'" cy="'+y(c).toFixed(1)+'" r="'+(c!==g?4.5:3)+'"'+(c!==g?' stroke="var(--bad)" stroke-width="1.5"':'')+'><title>'+tstamp(s.t_first)+' '+(g>=0?'+':'')+pct(g)+' '+s.status+(c!==g?' (beyond the chart)':'')+'</title></circle>'; });
    out+='<text class="ax" x="'+L+'" y="'+(Hh-8)+'">'+tstamp(Wd[0].t_first)+'</text><text class="ax" x="'+(W-R)+'" y="'+(Hh-8)+'" text-anchor="end">'+tstamp(Wd[Wd.length-1].t_first)+'</text></svg>';
    return out+'<p class="cap">hop over payer per window; the band is the margin in force ('+pct(margin,0)+'), the dashed line the median ('+(med>=0?'+':'')+pct(med)+'); orange settled disputed'+(beyond?'; '+beyond+' window'+(beyond===1?'':'s')+' beyond ±100%, pinned at the edge':'')+'</p>';
  };
  C.rateChart=function(l, X){
    var Wd=sortBy(l.windows, function(s){ return s.t_first; }).slice(-96); if(Wd.length<2) return '';
    var W=440, Hh=120, L=46, R=8, T=10, B=24, rates=Wd.map(rateOf), max=Math.max.apply(null,rates)||1;
    var pur=X.purchases.filter(function(p){ return p.kind==='purchase' && p.program==='payer' && p.route.from===l.payer && p.route.to===l.hop && p.min_bytes; })[0];
    var minRate=pur ? pur.min_bytes*8/H/1e6 : null; if(minRate && minRate>max) max=minRate*1.1;
    var y=function(v){ return T+(Hh-T-B)*(1-v/max); }, x=function(i){ return L+i/Wd.length*(W-L-R); }, bw=(W-L-R)/Wd.length*0.8;
    var out=svgo(W,Hh);
    for(var g=1; g<=3; g++){ var v=max*g/3; out+='<line class="grid" x1="'+L+'" x2="'+(W-R)+'" y1="'+y(v).toFixed(1)+'" y2="'+y(v).toFixed(1)+'"/><text class="ax" x="'+(L-4)+'" y="'+(y(v)+4).toFixed(1)+'" text-anchor="end">'+v.toFixed(v<1?2:1)+'</text>'; }
    Wd.forEach(function(s,i){ out+='<rect class="bar'+(s.status==='disputed'?' disputed':'')+'" x="'+x(i).toFixed(1)+'" y="'+y(rateOf(s)).toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+Math.max(1,(Hh-B)-y(rateOf(s))).toFixed(1)+'"><title>'+tstamp(s.t_first)+' '+rateOf(s).toFixed(2)+' Mbit/s</title></rect>'; });
    if(minRate!=null) out+='<line class="min" x1="'+L+'" x2="'+(W-R)+'" y1="'+y(minRate).toFixed(1)+'" y2="'+y(minRate).toFixed(1)+'"/>';
    out+='<line class="zero" x1="'+L+'" x2="'+(W-R)+'" y1="'+(Hh-B)+'" y2="'+(Hh-B)+'"/><text class="ax" x="'+L+'" y="'+(Hh-8)+'">Mbit/s, settled bytes over the window</text></svg>';
    return out+'<p class="cap">'+(minRate!=null?'the dotted line is the purchase\'s minimum, '+bytes(pur.min_bytes)+' a window ('+minRate.toFixed(2)+' Mbit/s); below it the premium is paid in proportion':'no purchase governs this link')+'</p>';
  };
  C.linkCards=function(X, now){
    var out='', inForce={}; (X.st.links||[]).forEach(function(l){ inForce[l.relay]=l; }); if(X.st.link&&X.st.nym) inForce[X.st.nym]=X.st.link;
    C.linkOrder(X).forEach(function(k,i){
      var l=X.links[k], m=BW.marginOf(l, inForce), disputed=l.windows.filter(function(s){ return s.status==='disputed'; }).length;
      var paid=l.windows.reduce(function(a,s){ return a+(s.amount_usd||0); },0), fee=l.windows.reduce(function(a,s){ return a+(s.fee_usd||0); },0), gb=l.windows.reduce(function(a,s){ return a+s.settled_bytes; },0);
      out+='<div class="chart"><h3 style="margin:0 0 6px;font-size:15px"><i style="display:inline-block;width:10px;height:10px;border-radius:2px;background:'+C.col(i)+';margin-right:6px"></i>'+esc(X.name(l.payer))+' → '+esc(X.name(l.hop))+' <small class="cap" style="display:inline">'+l.windows.length+' windows · '+bytes(gb)+' · '+disputed+' disputed · '+money(paid)+' paid, '+money(fee)+' of it the Steward\'s</small></h3>'+C.gapChart(l, m)+C.rateChart(l, X)+'</div>';
    });
    return out ? '<h2>Links <small>as settled, the last 96 windows of each</small></h2><div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(min(440px,100%),1fr))">'+out+'</div>' : '';
  };

  // The chain of hours: one block per hour of the last three days.
  C.chainChart=function(X, now){
    var end=Math.floor(now/H)*H-H, start=end-71*H, W=960, Hh=54, L=10, R=10, n=72, bw=(W-L-R)/n;
    var hours={}; X.S.forEach(function(s){ var w=Math.floor(s.t_first/H)*H; (hours[w]=hours[w]||[]).push(s); });
    var committed={}; X.C.forEach(function(c){ committed[c.window]=c; });
    var firstC=X.C.length?Math.min.apply(null, X.C.map(function(c){ return c.window; })):Infinity;
    var out=svgo(W,Hh), i=0;
    for(var h=start; h<=end; h+=H, i++){
      var c=committed[h], had=(hours[h]||[]).length, cls=c?'block':(had&&h>=firstC?'block missing':'block none'), x=L+i*bw;
      out+='<rect class="'+cls+'" x="'+(x+1).toFixed(1)+'" y="8" width="'+(bw-2).toFixed(1)+'" height="22" rx="2"><title>'+tstamp(h)+(c?': committed, '+c.count+' settlement'+(c.count===1?'':'s')+', root '+String(c.root).slice(0,12)+'…':had?': '+had+' settlement'+(had===1?'':'s')+', no commitment':': no settlement')+'</title></rect>';
      if(c&&c.count>1) out+='<text class="ax" x="'+(x+bw/2).toFixed(1)+'" y="23" text-anchor="middle" fill="var(--accent-ink)">'+c.count+'</text>';
      var d=new Date(h*1000); if(d.getHours()===0) out+='<text class="ax" x="'+(x+2).toFixed(1)+'" y="46">'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'</text>';
    }
    out+='</svg>';
    return '<div class="chart" style="margin-bottom:10px">'+out+'<p class="cap legend"><i style="background:var(--accent)"></i>committed hour, with its settlement count when more than one <i style="background:transparent;border:1.5px solid var(--bad)"></i>settled but not committed <i style="background:var(--line)"></i>nothing settled</p></div>';
  };

  // The map: each place a card holding its nodes; the settled links between the cards with their labels in chips above
  // the row; the offers below the row. `mine` marks the places this operator's nodes sit at (the node's and fleet's views).
  C.mapChart=function(X, now, map, mine){
    var m=map||BW.D.map; if(!m||!m.places) return '';
    var ids=Object.keys(m.places), n=ids.length, W=960;
    var byPlace={}; for(var nd in (m.nodes||{})) (byPlace[m.nodes[nd]]=byPlace[m.nodes[nd]]||[]).push(nd);
    var maxNodes=Math.max(1, Math.max.apply(null, ids.map(function(id){ return (byPlace[id]||[]).length; })));
    var cw=Math.min(230, W/n-36), ch=60+18*maxNodes, top=92, cy=top+ch/2, bottom=top+ch;
    var cx=function(i){ return W*(i+0.5)/n; }, pos={}; ids.forEach(function(id,i){ pos[id]=i; });
    var placeOfNym=function(nym){ var nm=X.names[nym]; return nm && m.nodes ? m.nodes[nm] : null; };
    var P=X.purchases.filter(function(p){ return p.kind==='purchase' && p.deadline>=now && !p.closed; });
    var firstLinks={}; P.forEach(function(p){ if(p.program==='first_link') firstLinks[p.route.to]=(firstLinks[p.route.to]||0)+1; });
    var Hh=bottom+150, out=svgo(W,Hh), links='', labels='', offers='', mineSet={}; (mine||[]).forEach(function(nm){ if(m.nodes&&m.nodes[nm]) mineSet[m.nodes[nm]]=1; });
    C.linkOrder(X).forEach(function(k,i){
      var l=X.links[k], a=placeOfNym(l.payer), b=placeOfNym(l.hop); if(a==null||b==null||pos[a]==null||pos[b]==null) return;
      var gb=l.windows.reduce(function(s,w){ return s+w.settled_bytes; },0), sw=Math.max(2, Math.min(10, 2+Math.log10(1+gb/1e6)*2)), text=X.name(l.payer)+' → '+X.name(l.hop)+' · '+bytes(gb);
      var pur=P.filter(function(p){ return p.program==='payer' && p.route.from===l.payer && p.route.to===l.hop; })[0];
      if(a===b){ var x=cx(pos[a]); links+='<path class="link" d="M'+(x-22)+' '+top+' C '+(x-34)+' '+(top-52)+', '+(x+34)+' '+(top-52)+', '+(x+22)+' '+top+'" stroke-width="'+sw.toFixed(1)+'" stroke="'+C.col(i)+'"><title>'+esc(text)+', inside '+esc(m.places[a].name)+'</title></path>'; labels+=chip(x, top-64, text+' · inside the place'); }
      else { var l0=Math.min(pos[a],pos[b]), l1=Math.max(pos[a],pos[b]), x1=cx(l0)+cw/2, x2=cx(l1)-cw/2, xm=(x1+x2)/2;
        if(l1-l0===1){ links+='<line class="link" x1="'+x1+'" y1="'+cy+'" x2="'+x2+'" y2="'+cy+'" stroke-width="'+sw.toFixed(1)+'" stroke="'+C.col(i)+'"><title>'+esc(text)+'</title></line><line class="grid" x1="'+xm+'" y1="'+(cy-4)+'" x2="'+xm+'" y2="'+(top-12)+'"/>'; labels+=chip(xm, top-20, text); if(pur) labels+=chip(xm, top-42, 'purchase · ≥ '+bytes(pur.min_bytes)+' a window · '+money(pur.premium_per_gb)+'/GB · '+money(pur.escrow)+' escrow', 'offer'); }
        else { var xa=cx(l0), xb=cx(l1); links+='<path class="link" d="M'+xa+' '+top+' C '+xa+' '+(top-70)+', '+xb+' '+(top-70)+', '+xb+' '+top+'" stroke-width="'+sw.toFixed(1)+'" stroke="'+C.col(i)+'"><title>'+esc(text)+'</title></path>'; labels+=chip((xa+xb)/2, top-58, text); }
        if(pos[a]>pos[b]) links+='<text class="ax" x="'+(x1+6)+'" y="'+(cy+4)+'">◀</text>'; else links+='<text class="ax" x="'+(x2-14)+'" y="'+(cy+4)+'">▶</text>';
      }
    });
    P.forEach(function(p){
      var from = p.route.from==null ? null : (m.places[p.route.from]?p.route.from:placeOfNym(p.route.from)), to = m.places[p.route.to]?p.route.to:placeOfNym(p.route.to);
      if(p.program==='second_bridge' && from!=null && to!=null && pos[from]!=null && pos[to]!=null){ var xa=cx(pos[from])+cw*0.3, xb=cx(pos[to])-cw*0.3, y0=bottom+44, dip=bottom+130; offers+='<path class="offer" d="M'+xa+' '+(bottom+2)+' L '+xa+' '+y0+' C '+xa+' '+dip+', '+xb+' '+dip+', '+xb+' '+y0+' L '+xb+' '+(bottom+2)+'"><title>'+esc(p.id.slice(0,8))+': not via '+esc((p.route.not_via||[]).join(', '))+'</title></path>'; offers+=chip((xa+xb)/2, bottom+112, 'second path wanted, through a third place · '+money(p.escrow)+' at '+money(p.premium_per_gb)+'/GB', 'offer'); }
    });
    var cards='';
    ids.forEach(function(id,i){ var pl=m.places[id], x=cx(i)-cw/2, nodes=byPlace[id]||[];
      cards+='<rect class="place'+(firstLinks[id]?' wanted':'')+(mineSet[id]?' mine':'')+'" x="'+x.toFixed(1)+'" y="'+top+'" width="'+cw.toFixed(1)+'" height="'+ch+'" rx="8"/>';
      cards+='<text class="lbl" x="'+(x+12)+'" y="'+(top+22)+'" font-weight="600">'+esc(pl.name||id)+'</text><text class="ax" x="'+(x+12)+'" y="'+(top+38)+'">'+esc(pl.medium)+' · '+esc(pl.range_class)+(pl.mapped?'':' · proposed')+'</text>';
      if(!nodes.length) cards+='<text class="ax" x="'+(x+12)+'" y="'+(top+60)+'">no node here yet</text>';
      nodes.forEach(function(nd,j){ var yy=top+60+j*18, isMe=(mine||[]).indexOf(nd)>=0; cards+='<circle class="node" cx="'+(x+17)+'" cy="'+(yy-4)+'" r="4"'+(isMe?' stroke="var(--fg)" stroke-width="1.5"':'')+'/><text class="lbl m" x="'+(x+27)+'" y="'+yy+'"'+(isMe?' font-weight="700"':'')+'>'+esc(nd)+(isMe?' · mine':'')+'</text>'; });
      if(firstLinks[id]) cards+=chip(cx(i), bottom+22, 'first link wanted'+(firstLinks[id]>1?' · '+firstLinks[id]+' offers open':''), 'offer');
    });
    out+=links+offers+cards+labels+'</svg>';
    return '<h2>The map <small>places, the links that have settled, and the offers open on them</small></h2><div class="chart">'+out+'<p class="cap legend"><i style="background:var(--accent)"></i>a link, width by bytes carried <i style="background:transparent;border:2px dashed var(--info)"></i>an offer open on the map'+((mine&&mine.length)?' <i style="background:transparent;border:2px solid var(--accent)"></i>a place where a node of mine sits':'')+' · places are accepted by Mint consensus, here the membership\'s file</p></div>';
  };

  // Money: the month so far per pair as a stacked bar, and the month's cumulative line by day.
  C.moneyChart=function(X, now){
    var d=new Date(now*1000), month=d.getFullYear()+'-'+pad(d.getMonth()+1), by={}, days={};
    X.S.forEach(function(s){ var e=new Date(s.t_last*1000); if(e.getFullYear()+'-'+pad(e.getMonth()+1)!==month) return; var k=s.payer+'|'+s.hop; var o=by[k]=by[k]||{payer:s.payer,hop:s.hop,amount:0,fee:0,premium:0}; o.amount+=s.amount_usd||0; o.fee+=s.fee_usd||0; o.premium+=s.premium_usd||0; var dk=e.getDate(); days[dk]=(days[dk]||0)+(s.amount_usd||0)+(s.premium_usd||0); });
    var rows=Object.keys(by).map(function(k){ return by[k]; }); if(!rows.length) return '';
    var W=960, L=230, R=90, rh=26, Hh=rows.length*rh+16, max=Math.max.apply(null, rows.map(function(o){ return o.amount+o.premium; }))||1, sx=function(v){ return v/max*(W-L-R); };
    var out=svgo(W,Hh);
    rows.forEach(function(o,i){ var y=8+i*rh, x=L, relay=o.amount-o.fee; out+='<text class="lbl" x="'+(L-8)+'" y="'+(y+15)+'" text-anchor="end">'+esc(X.name(o.payer))+' pays '+esc(X.name(o.hop))+'</text>';
      [['relay',relay],['fee',o.fee],['premium',o.premium]].forEach(function(seg){ var w=sx(seg[1]); if(w>0){ out+='<rect class="seg '+seg[0]+'" x="'+x.toFixed(1)+'" y="'+y+'" width="'+w.toFixed(1)+'" height="18"><title>'+seg[0]+' '+money(seg[1])+'</title></rect>'; x+=w; } });
      out+='<text class="lbl m" x="'+(x+6).toFixed(1)+'" y="'+(y+14)+'">'+money(o.amount+o.premium)+'</text>'; });
    out+='</svg>';
    var last=d.getDate(), cum=0, pts=[], W2=960, H2=120, L2=70, R2=12, T2=10, B2=24, tot=Object.keys(days).reduce(function(a,k){ return a+days[k]; },0)||1;
    var xd=function(day){ return L2+(day-1)/Math.max(1,last-1)*(W2-L2-R2); }, yd=function(v){ return T2+(H2-T2-B2)*(1-v/tot); };
    for(var day=1; day<=last; day++){ cum+=days[day]||0; pts.push(xd(day).toFixed(1)+','+yd(cum).toFixed(1)); }
    var line=svgo(W2,H2);
    [0.5,1].forEach(function(f){ line+='<line class="grid" x1="'+L2+'" x2="'+(W2-R2)+'" y1="'+yd(tot*f).toFixed(1)+'" y2="'+yd(tot*f).toFixed(1)+'"/><text class="ax" x="'+(L2-6)+'" y="'+(yd(tot*f)+4).toFixed(1)+'" text-anchor="end">'+money(tot*f)+'</text>'; });
    line+='<polyline fill="none" stroke="var(--accent)" stroke-width="2" points="'+pts.join(' ')+'"/>';
    [1,last].forEach(function(day){ line+='<text class="ax" x="'+xd(day).toFixed(1)+'" y="'+(H2-8)+'" text-anchor="'+(day===1?'start':'end')+'">'+month+'-'+pad(day)+'</text>'; });
    line+='</svg>';
    return '<h2>Money <small>'+month+', the month so far</small></h2><div class="chart">'+out+'<p class="cap legend"><i style="background:var(--accent)"></i>the relay\'s share <i style="background:var(--warn)"></i>the Steward\'s fee <i style="background:var(--info)"></i>premiums on purchases</p></div><div class="chart" style="margin-top:10px">'+line+'<p class="cap">paid for carriage and premiums, cumulative by day</p></div>';
  };

  // The outbox: what waits for the Mint, by age.
  C.outboxChart=function(items, now){
    if(!items||!items.length) return '';
    var rows=sortBy(items, function(o){ return o.t||0; }).map(function(o){ return '<tr><td class="m">'+esc(o.file||o.name||'')+'</td><td>'+esc(o.kind||'')+'</td><td>'+(o.t?tstamp(o.t):'–')+'</td><td class="n">'+(o.t?F.dur(now-o.t):'–')+'</td></tr>'; });
    return '<div class="tbl"><table><thead><tr><th>file</th><th>kind</th><th>made</th><th class="n">waiting</th></tr></thead><tbody>'+rows.join('')+'</tbody></table></div>';
  };
})(window.BW);
