from __future__ import annotations


def get_template() -> str:
    return r"""
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>World-State Debug UI</title>
    <style>
      body { font-family: system-ui,-apple-system,BlinkMacSystemFont,sans-serif; margin:1rem; background:#111; color:#ddd; }
      h1,h2,h3 { margin:0 0 0.5rem; color:#f0f0f0; }
      section { margin-bottom:1.5rem; padding:1rem; border:1px solid #333; border-radius:4px; background:#181818; }
      .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:1rem; }
      .card { border:1px solid #333; padding:0.75rem; border-radius:3px; background:#1f1f1f; }
      .card strong { color:#f0f0f0; }
      pre { white-space:pre-wrap; word-wrap:break-word; font-size:0.9rem; }
      .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
      .good { color:#4a4; }
      .muted { color:#999; }
      .warn { color:#aa4; }
      .err { color:#a44; }
      table { width:100%; border-collapse:collapse; }
      th,td { border-bottom:1px solid #333; padding:0.5rem; text-align:left; font-size:0.95rem; }
      th { color:#bbb; }
      .pill { background:#333; padding:0.15rem 0.4rem; border-radius:2px; margin-right:0.25rem; }
      .right { float:right; }
      a { color:#7bb0ff; text-decoration:none; }
      a:hover { text-decoration:underline; }
    </style>
  </head>
  <body>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem;">
      <h1>World-State Debug UI</h1>
      <span class="mono muted">Developer/operator window into the real world-state engine</span>
    </div>
    <section id="overview"></section>
    <section id="sources"></section>
    <section id="observations"></section>
    <section id="events"></section>
    <section id="detail"></section>
    <script>
      const $ = (id) => document.getElementById(id);
      function fmt(ts){ if(!ts) return '-'; try{ const d=new Date(ts); return d.toLocaleTimeString(); }catch(e){ return ts; } }
      function ago(ts){ if(!ts) return '-'; const s=(Date.now()-new Date(ts))/1000; if(s<60) return Math.floor(s)+'s ago'; const m=s/60; if(m<60) return Math.floor(m)+'m ago'; const h=m/60; return Math.floor(h)+'h ago'; }
      async function j(p){ const r=await fetch(p); if(!r.ok) throw new Error(p+':'+r.status); return r.json(); }
      async function load(){
        let world=null, sources=[], observations=[], events=[], detail=null;
        try{ world=await j('/v1/world?include_closed=false'); }catch(e){}
        try{ const res=await j('/v1/events'); events=res.events||[]; }catch(e){}
        if(events.length){ try{ detail=await j('/v1/events/'+events[0].event_id); }catch(e){} }
        try{ const r=await fetch('/v1/observations?limit=30'); const d=await r.json(); observations=d.observations||[]; }catch(e){}
        try{ const r=await fetch('/v1/sources'); sources=(await r.json()).sources||[]; }catch(e){}
        renderOverview(world, sources, events, observations);
        renderSources(sources, world);
        renderObservations(observations);
        renderEvents(events, detail);
        if(detail) renderDetail(detail);
        setTimeout(load, 2000);
      }
      function renderOverview(w,s,ev,obs){
        $('overview').innerHTML = `
          <h2>World State Overview</h2>
          <div class="grid">
            <div class="card"><strong>Active Events</strong><div class="mono" style="font-size:1.5rem">${w?w.events_active:ev.filter(e=>e.status=='active').length}</div></div>
            <div class="card"><strong>Events Changed (material)</strong><div class="mono" style="font-size:1.5rem">${w?w.events_changed_materially:0}</div></div>
            <div class="card"><strong>Observations (total in snapshot)</strong><div class="mono" style="font-size:1.5rem">${w?w.observation_total:(obs.length)}</div></div>
            <div class="card"><strong>Sources Reported</strong><div class="mono" style="font-size:1.5rem">${s.length}</div></div>
            <div class="card"><strong>Last World-State Update</strong><div>${fmt(w&&w.generated_at)} <span class="muted">(${ago(w&&w.generated_at)})</span></div></div>
          </div>`;
      }
      function renderSources(s){
        let html='<h2>Data Sources</h2><div class="grid">';
        if(!s.length){ html+='<div class="card muted">No source health exposed yet.</div>'; }
        s.forEach(src=>{
          const st = src.state||src.status||'unknown';
          const cls = (st==='healthy'||st==='active')?'good':(st==='degraded'||st==='warn'?'warn':(st==='error'||st==='offline'?'err':'muted'));
          html+=`<div class="card"><strong>${src.source_id||src.name}</strong> <span class="${cls} right">${st}</span><div class="muted">${src.source_type||''}</div><div>Last success: ${fmt(src.last_success)} <span class="muted">(${ago(src.last_success)})</span></div><div>Records: ${src.records_received||0} Latency:${src.latency_ms||'-'}ms Err:${(src.error_rate||0).toFixed(2)}</div></div>`;
        });
        html+='</div>';
        $('sources').innerHTML=html;
      }
      function renderObservations(obs){
        let html=`<h2>Recent Observations</h2><table><thead><tr><th>Time</th><th>Source</th><th>Type</th><th>Location</th><th>Content</th><th>Resolved To</th></tr></thead><tbody>`;
        if(!obs.length){ html+='<tr><td colspan=6 class="muted">No observations returned.</td></tr>'; }
        obs.slice(0,30).forEach(o=>{
          const loc = o.centroid ? o.centroid.join(',') : '-';
          const content = o.headline || (o.structured_payload?JSON.stringify(o.structured_payload).slice(0,80):'-');
          html+=`<tr><td class="mono">${fmt(o.observed_at||o.event_time)}</td><td>${o.source_id}</td><td>${o.observation_type||'-'}</td><td class="mono">${loc}</td><td>${content}</td><td class="mono">${o.event_id?'<a href="#evt-'+o.event_id+'">'+o.event_id+'</a>':'-'}</td></tr>`;
        });
        html+='</tbody></table>';
        $('observations').innerHTML=html;
      }
      function renderEvents(ev,detail){
        let html='<h2>Calculated Events</h2><div class="grid">';
        if(!ev.length){ html+='<div class="card muted">No events found.</div>'; }
        ev.forEach(e=>{
          html+=`<div class="card" id="evt-${e.event_id}"><strong><a href="#" onclick="selectEvent('${e.event_id}')">${e.event_id}</a></strong> <span class="right good">${e.status}</span><div>${e.dominant_type||e.event_type||'-'}</div><div class="muted">First:${fmt(e.first_observed)} Last:${fmt(e.updated_at||e.last_reconstructed_at)}</div><div>Obs:${e.observation_count||'-'} v${e.state_version||'-'}</div></div>`;
        });
        html+='</div>';
        $('events').innerHTML=html;
      }
      async function selectEvent(eid){
        try{ const d=await j('/v1/events/'+eid); renderDetail(d); }catch(e){}
      }
      function renderDetail(d){
        if(!d) return;
        const ev=d.event||{}; const cs=d.current_state||{};
        let html=`<h2>Event Detail <span class="mono muted">${ev.event_id}</span></h2><div class="card">Status: ${cs.status||ev.status} Type dist: ${Object.entries(cs.event_type_distribution||{}).slice(0,2).map(([k,v])=>k+':'+v.toFixed(2)).join(' ')} Obs:${ev.observation_count} State v${cs.state_version}</div><br/><div class="grid"><div class="card"><h3>Current State</h3><pre class="mono">${JSON.stringify(cs,null,2)}</pre></div><div class="card"><h3>Recent Changes</h3>`;
        if(d.recent_changes&&d.recent_changes.length){ d.recent_changes.forEach(rc=>{ html+=`<div class="mono">v${rc.previous_state_version}→v${rc.state_version} mag=${rc.magnitude}</div>`; }); } else html+='<div class="muted">None</div>';
        html+='</div></div><br/><div class="grid"><div class="card"><h3>Evidence</h3><pre class="mono">${JSON.stringify(d.evidence_summary,null,2)}</pre></div><div class="card"><h3>Top Impacts</h3>';
        if(d.top_impacts&&d.top_impacts.length){ d.top_impacts.forEach(i=>{ html+=`<pre class="mono">${JSON.stringify(i,null,2)}</pre>`; }); } else html+='<div class="muted">None</div>';
        html+='</div></div>';
        $('detail').innerHTML=html;
      }
      load();
    </script>
  </body>
</html>
"""
