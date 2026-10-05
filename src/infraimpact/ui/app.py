from __future__ import annotations


def get_template() -> str:
    return r"""
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>World-State Intelligence Engine</title>
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
    <style>
      :root {
        --bg-base: #0f1117;
        --bg-surface: #171a23;
        --bg-card: #1e2230;
        --bg-hover: #262c3e;
        --border: #2a3144;
        --border-active: #3b82f6;
        --text-main: #f1f5f9;
        --text-muted: #94a3b8;
        --accent-blue: #3b82f6;
        --accent-green: #10b981;
        --accent-amber: #f59e0b;
        --accent-red: #ef4444;
        --accent-purple: #8b5cf6;
      }
      * { box-sizing: border-box; }
      body {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        margin: 0;
        padding: 1.5rem;
        background: var(--bg-base);
        color: var(--text-main);
        line-height: 1.5;
      }
      header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 2rem;
        padding-bottom: 1rem;
        border-bottom: 1px solid var(--border);
      }
      h1 { font-size: 1.5rem; font-weight: 700; margin: 0; color: #fff; letter-spacing: -0.02em; }
      h2 { font-size: 1.15rem; font-weight: 600; margin: 0 0 1rem; color: #f8fafc; }
      h3 { font-size: 0.95rem; font-weight: 600; margin: 0 0 0.5rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }
      section {
        margin-bottom: 2rem;
        padding: 1.25rem;
        border: 1px solid var(--border);
        border-radius: 8px;
        background: var(--bg-surface);
      }
      .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; }
      .grid-3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; }
      .card {
        border: 1px solid var(--border);
        padding: 1rem;
        border-radius: 6px;
        background: var(--bg-card);
        transition: border-color 0.15s ease, transform 0.15s ease;
      }
      .card.clickable { cursor: pointer; }
      .card.clickable:hover { border-color: var(--accent-blue); background: var(--bg-hover); }
      .card.selected { border-color: var(--border-active); box-shadow: 0 0 0 1px var(--border-active); }
      .card-title { font-size: 1.05rem; font-weight: 600; color: #fff; margin-bottom: 0.5rem; line-height: 1.3; }
      .card-meta-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.4rem 1rem; font-size: 0.85rem; margin-top: 0.75rem; border-top: 1px solid rgba(255,255,255,0.06); padding-top: 0.75rem; }
      .meta-item { display: flex; flex-direction: column; }
      .meta-label { color: var(--text-muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; }
      .meta-val { font-weight: 500; color: #e2e8f0; }
      pre { margin: 0; white-space: pre-wrap; word-wrap: break-word; font-size: 0.85rem; color: #cbd5e1; }
      .mono { font-family: 'JetBrains Mono', monospace; font-size: 0.85rem; }
      .badge {
        display: inline-flex;
        align-items: center;
        padding: 0.2rem 0.55rem;
        border-radius: 9999px;
        font-size: 0.75rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
      }
      .badge.active { background: rgba(16, 185, 129, 0.15); color: var(--accent-green); border: 1px solid rgba(16, 185, 129, 0.3); }
      .badge.planned { background: rgba(139, 92, 246, 0.15); color: var(--accent-purple); border: 1px solid rgba(139, 92, 246, 0.3); }
      .badge.closed { background: rgba(148, 163, 184, 0.15); color: var(--text-muted); border: 1px solid rgba(148, 163, 184, 0.3); }
      .badge.quiescent { background: rgba(245, 158, 11, 0.15); color: var(--accent-amber); border: 1px solid rgba(245, 158, 11, 0.3); }
      .good { color: var(--accent-green); }
      .muted { color: var(--text-muted); }
      .warn { color: var(--accent-amber); }
      .err { color: var(--accent-red); }
      table { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
      th, td { border-bottom: 1px solid var(--border); padding: 0.65rem 0.75rem; text-align: left; }
      th { color: var(--text-muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; background: rgba(255,255,255,0.02); }
      tr:hover td { background: rgba(255,255,255,0.02); }
      a { color: #60a5fa; text-decoration: none; }
      a:hover { text-decoration: underline; }
      .stat-val { font-size: 1.75rem; font-weight: 700; line-height: 1.2; margin-top: 0.25rem; }
      .tag { background: rgba(255,255,255,0.06); padding: 0.15rem 0.4rem; border-radius: 4px; font-size: 0.75rem; }
    </style>
  </head>
  <body>
    <header>
      <div>
        <h1>World-State Intelligence Engine</h1>
        <div class="muted mono" style="font-size:0.8rem;margin-top:0.25rem">Continuous Real-Time Event Correlation, Persistent State & Provenance</div>
      </div>
      <div id="live-indicator" class="mono" style="display:flex;align-items:center;gap:0.5rem;font-size:0.85rem">
        <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent-green)"></span>
        LIVE ENGINE CONNECTED
      </div>
    </header>

    <section id="overview"></section>
    <section id="events"></section>
    <section id="detail"></section>
    <section id="observations"></section>
    <section id="sources"></section>

    <script>
      const $ = (id) => document.getElementById(id);
      let selectedEventId = null;

      function fmt(ts){
        if(!ts) return '-';
        try {
          const d = new Date(ts);
          return d.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', second:'2-digit'});
        } catch(e) { return ts; }
      }
      function ago(ts){
        if(!ts) return '-';
        const s = (Date.now() - new Date(ts)) / 1000;
        if(s < 0) return 'in future';
        if(s < 60) return Math.floor(s) + 's ago';
        const m = s / 60;
        if(m < 60) return Math.floor(m) + 'm ago';
        const h = m / 60;
        if(h < 24) return Math.floor(h) + 'h ago';
        return Math.floor(h/24) + 'd ago';
      }
      async function j(p){
        const r = await fetch(p);
        if(!r.ok) throw new Error(p + ':' + r.status);
        return r.json();
      }

      async function load(){
        let world = null, sources = [], observations = [], events = [], detail = null;
        try { world = await j('/v1/world?include_closed=false'); } catch(e){}
        try { const res = await j('/v1/events?limit=40'); events = res.events || []; } catch(e){}
        if(events.length && !selectedEventId){ selectedEventId = events[0].event_id; }
        if(selectedEventId){
          try { detail = await j('/v1/events/' + selectedEventId); } catch(e){}
        }
        try { const r = await fetch('/v1/observations?limit=40'); observations = (await r.json()).observations || []; } catch(e){}
        try { const r = await fetch('/v1/sources'); sources = (await r.json()).sources || []; } catch(e){}

        renderOverview(world, sources, events, observations);
        renderEvents(events, selectedEventId);
        if(detail) renderDetail(detail);
        renderObservations(observations);
        renderSources(sources);
        setTimeout(load, 3000);
      }

      function renderOverview(w, s, ev, obs){
        const activeEvents = ev.filter(e => e.status === 'active').length;
        const totalObs = w ? w.observation_total : obs.length;
        const healthySources = s.filter(src => src.state === 'healthy' || src.state === 'active').length;
        $('overview').innerHTML = `
          <h2>World-State Overview</h2>
          <div class="grid">
            <div class="card">
              <span class="meta-label">Active Persistent Events</span>
              <div class="stat-val good">${activeEvents}</div>
              <div class="muted" style="font-size:0.8rem;margin-top:0.25rem">${ev.length} total events tracked</div>
            </div>
            <div class="card">
              <span class="meta-label">Total Observations Ledger</span>
              <div class="stat-val mono">${totalObs.toLocaleString()}</div>
              <div class="muted" style="font-size:0.8rem;margin-top:0.25rem">From ${s.length} independent signals</div>
            </div>
            <div class="card">
              <span class="meta-label">Active Data Sources</span>
              <div class="stat-val">${healthySources} <span class="muted" style="font-size:1rem;font-weight:400">/ ${s.length}</span></div>
              <div class="muted" style="font-size:0.8rem;margin-top:0.25rem">Real-time public safety & transport</div>
            </div>
            <div class="card">
              <span class="meta-label">State Reconstruction</span>
              <div class="stat-val" style="font-size:1.25rem;font-weight:600;margin-top:0.5rem">${fmt(w ? w.generated_at : new Date())}</div>
              <div class="muted" style="font-size:0.8rem;margin-top:0.25rem">${ago(w ? w.generated_at : new Date())}</div>
            </div>
          </div>`;
      }

      function renderEvents(ev, selId){
        let html = '<h2>Persistent World Events</h2><div class="grid">';
        if(!ev.length){ html += '<div class="card muted">No calculated events found.</div>'; }
        ev.forEach(e => {
          const isSel = e.event_id === selId;
          const statusCls = e.status === 'active' ? 'active' : (e.status === 'planned' ? 'planned' : 'closed');
          const title = e.title || ('Incident ' + e.event_id);
          const loc = e.location || (e.geometry ? 'Geocoded Corridor' : 'Regional');
          const conf = typeof e.confidence === 'number' ? e.confidence.toFixed(2) : '0.85';

          html += `
            <div class="card clickable ${isSel ? 'selected' : ''}" onclick="selectEvent('${e.event_id}')">
              <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:0.5rem;margin-bottom:0.4rem">
                <span class="badge ${statusCls}">${e.status}</span>
                <span class="mono muted" style="font-size:0.75rem">${e.event_id}</span>
              </div>
              <div class="card-title">${title}</div>
              <div class="card-meta-grid">
                <div class="meta-item">
                  <span class="meta-label">Location</span>
                  <span class="meta-val">${loc}</span>
                </div>
                <div class="meta-item">
                  <span class="meta-label">Dominant Type</span>
                  <span class="meta-val tag">${e.dominant_type || 'incident'}</span>
                </div>
                <div class="meta-item">
                  <span class="meta-label">First Observed</span>
                  <span class="meta-val mono">${fmt(e.first_observed)}</span>
                </div>
                <div class="meta-item">
                  <span class="meta-label">Last Updated</span>
                  <span class="meta-val mono">${fmt(e.updated_at || e.last_reconstructed_at)}</span>
                </div>
                <div class="meta-item">
                  <span class="meta-label">Observations</span>
                  <span class="meta-val good">${e.observation_count || 1}</span>
                </div>
                <div class="meta-item">
                  <span class="meta-label">Sources (Indep)</span>
                  <span class="meta-val">${e.source_count || 1} <span class="muted">(${e.independent_source_count || 1})</span></span>
                </div>
                <div class="meta-item">
                  <span class="meta-label">Confidence</span>
                  <span class="meta-val">${conf}</span>
                </div>
                <div class="meta-item">
                  <span class="meta-label">State Version</span>
                  <span class="meta-val mono">v${e.state_version || 1}</span>
                </div>
              </div>
            </div>`;
        });
        html += '</div>';
        $('events').innerHTML = html;
      }

      async function selectEvent(eid){
        selectedEventId = eid;
        try {
          const [d, evd] = await Promise.all([
            j('/v1/events/' + eid),
            j('/v1/events/' + eid + '/evidence').catch(() => ({}))
          ]);
          if(d && evd && evd.observations){
            d.observations = evd.observations;
          }
          renderDetail(d);
          const evCards = document.querySelectorAll('#events .card');
          evCards.forEach(c => c.classList.remove('selected'));
        } catch(e){}
      }

      function renderDetail(d){
        if(!d) return;
        const ev = d.event || {};
        const cs = d.current_state || {};
        const title = ev.title || ('Event ' + ev.event_id);
        const loc = ev.location || (ev.geometry ? 'Corridor Geocoded' : '-');
        const statusCls = ev.status === 'active' ? 'active' : (ev.status === 'planned' ? 'planned' : 'closed');
        const supportingObs = d.observations || [];

        let obsRows = '';
        if(supportingObs.length){
          supportingObs.forEach(o => {
            const geomStr = o.centroid ? o.centroid.join(', ') : '-';
            obsRows += `
              <tr>
                <td class="mono">${fmt(o.observed_at || o.event_time)}</td>
                <td><span class="tag">${o.source_id}</span></td>
                <td>${o.observation_type || '-'}</td>
                <td class="mono">${geomStr}</td>
                <td><strong>${o.headline || (o.structured_payload ? JSON.stringify(o.structured_payload).slice(0, 80) : '-')}</strong></td>
                <td>${o.source_url ? '<a href="'+o.source_url+'" target="_blank">link</a>' : '-'}</td>
              </tr>`;
          });
        } else {
          obsRows = '<tr><td colspan="6" class="muted">No supporting observation records retrieved for this event yet.</td></tr>';
        }

        let changesHtml = '';
        if(d.recent_changes && d.recent_changes.length){
          d.recent_changes.forEach(rc => {
            changesHtml += `
              <div style="border-left:2px solid var(--accent-blue);padding-left:0.5rem;margin-bottom:0.5rem">
                <div class="mono" style="font-weight:600">State Transition v${rc.previous_state_version} → v${rc.state_version} <span class="muted font-normal">(${fmt(rc.reconstructed_at)})</span></div>
                <div class="muted">Magnitude: ${rc.magnitude !== null ? rc.magnitude : '-'}</div>
              </div>`;
          });
        } else {
          changesHtml = '<div class="muted">Initial reconstruction (v1) - no prior delta.</div>';
        }

        $('detail').innerHTML = `
          <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;margin-bottom:1rem">
            <div>
              <div style="display:flex;align-items:center;gap:0.75rem;margin-bottom:0.25rem">
                <h2 style="margin:0">${title}</h2>
                <span class="badge ${statusCls}">${ev.status}</span>
              </div>
              <div class="muted mono" style="font-size:0.85rem">Event ID: ${ev.event_id} | Location: ${loc}</div>
            </div>
            <div style="text-align:right" class="mono muted">
              <div>First Observed: ${fmt(ev.first_observed)}</div>
              <div>Last Updated: ${fmt(ev.updated_at || ev.last_reconstructed_at)}</div>
            </div>
          </div>

          <div class="grid" style="margin-bottom:1.5rem">
            <div class="card">
              <h3>Current State Reconstructed</h3>
              <div style="margin-bottom:0.5rem"><strong>Status:</strong> ${cs.status || ev.status} | <strong>Version:</strong> v${cs.state_version || 1}</div>
              <div style="margin-bottom:0.5rem"><strong>Dominant Type Distribution:</strong> ${Object.entries(cs.event_type_distribution || {}).map(([k,v]) => `<span class="tag">${k}: ${(v*100).toFixed(0)}%</span>`).join(' ') || '-'}</div>
              <pre class="mono" style="max-height:220px;overflow-y:auto;background:rgba(0,0,0,0.25);padding:0.5rem;border-radius:4px">${JSON.stringify(cs, null, 2)}</pre>
            </div>
            <div class="card">
              <h3>What Changed (Semantic Deltas)</h3>
              <div style="margin-top:0.5rem">${changesHtml}</div>
              <div style="margin-top:1rem">
                <h3>Corroboration & Provenance</h3>
                <div>Sources: <strong>${ev.source_count || 1}</strong> | Independent: <strong>${ev.independent_source_count || 1}</strong></div>
                <div>Confidence Score: <strong>${typeof ev.confidence === 'number' ? ev.confidence.toFixed(2) : '0.85'}</strong></div>
              </div>
            </div>
          </div>

          <div>
            <h3>Supporting Observations & Multi-Source Evidence (${supportingObs.length})</h3>
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Source</th>
                  <th>Observation Type</th>
                  <th>Coordinates</th>
                  <th>Evidence Content / Headline</th>
                  <th>Provenance</th>
                </tr>
              </thead>
              <tbody>
                ${obsRows}
              </tbody>
            </table>
          </div>`;
      }

      function renderObservations(obs){
        let html = `<h2>Recent Observations Ledger</h2><table><thead><tr><th>Time</th><th>Source</th><th>Type</th><th>Location</th><th>Content</th><th>Resolved To</th></tr></thead><tbody>`;
        if(!obs.length){ html += '<tr><td colspan=6 class="muted">No observations returned.</td></tr>'; }
        obs.slice(0, 30).forEach(o => {
          const loc = o.centroid ? o.centroid.join(', ') : '-';
          const content = o.headline || (o.structured_payload ? JSON.stringify(o.structured_payload).slice(0, 80) : '-');
          const resolvedLink = o.event_id ? `<a href="#detail" onclick="selectEvent('${o.event_id}')" class="mono good">${o.event_id}</a>` : '<span class="muted">-</span>';
          html += `<tr><td class="mono">${fmt(o.observed_at || o.event_time)}</td><td><span class="tag">${o.source_id}</span></td><td>${o.observation_type || '-'}</td><td class="mono">${loc}</td><td>${content}</td><td>${resolvedLink}</td></tr>`;
        });
        html += '</tbody></table>';
        $('observations').innerHTML = html;
      }

      function renderSources(s){
        let html = '<h2>Data Sources & Adapter Health</h2><div class="grid-3">';
        if(!s.length){ html += '<div class="card muted">No source health exposed yet.</div>'; }
        s.forEach(src => {
          const st = src.state || src.status || 'unknown';
          const cls = (st === 'healthy' || st === 'active') ? 'good' : ((st === 'degraded' || st === 'warn') ? 'warn' : ((st === 'error' || st === 'offline') ? 'err' : 'muted'));
          html += `
            <div class="card">
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:0.25rem">
                <strong>${src.source_id || src.name}</strong>
                <span class="${cls} mono" style="font-size:0.8rem;font-weight:600;text-transform:uppercase">${st}</span>
              </div>
              <div class="muted" style="font-size:0.75rem;margin-bottom:0.5rem">${src.source_type || 'realtime feed'}</div>
              <div style="font-size:0.8rem">Last success: <strong>${fmt(src.last_success)}</strong> <span class="muted">(${ago(src.last_success)})</span></div>
              <div style="font-size:0.8rem;margin-top:0.2rem">Records: <strong>${src.records_received || 0}</strong> | Latency: <strong>${src.latency_ms || 0}ms</strong> | Err: <strong>${(src.error_rate || 0).toFixed(2)}</strong></div>
            </div>`;
        });
        html += '</div>';
        $('sources').innerHTML = html;
      }

      load();
    </script>
  </body>
</html>
"""
