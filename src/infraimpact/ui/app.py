from __future__ import annotations


def get_template() -> str:
    return r"""
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>World-State Intelligence Engine | Puget Sound</title>
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet" />
    <style>
      :root {
        --bg-base: #0a0d14;
        --bg-surface: #111622;
        --bg-card: #182030;
        --bg-card-hover: #1f293d;
        --border: #232d42;
        --border-light: rgba(255, 255, 255, 0.08);
        --border-active: #38bdf8;
        --text-main: #f8fafc;
        --text-muted: #94a3b8;
        --text-subtle: #64748b;
        --accent-blue: #38bdf8;
        --accent-indigo: #6366f1;
        --accent-purple: #a855f7;
        --accent-green: #10b981;
        --accent-amber: #f59e0b;
        --accent-red: #ef4444;
        --accent-cyan: #06b6d4;
        --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        --font-mono: 'JetBrains Mono', monospace;
      }
      * { box-sizing: border-box; }
      body {
        font-family: var(--font-sans);
        margin: 0;
        padding: 1.5rem 2rem;
        background: var(--bg-base);
        color: var(--text-main);
        line-height: 1.5;
        -webkit-font-smoothing: antialiased;
      }
      header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 1.5rem;
        padding-bottom: 1.25rem;
        border-bottom: 1px solid var(--border);
      }
      .brand { display: flex; align-items: center; gap: 0.85rem; }
      .brand-logo {
        width: 36px;
        height: 36px;
        border-radius: 8px;
        background: linear-gradient(135deg, #0284c7, #6366f1);
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 800;
        color: #fff;
        font-size: 1.1rem;
        box-shadow: 0 0 15px rgba(56, 189, 248, 0.35);
      }
      h1 { font-size: 1.4rem; font-weight: 700; margin: 0; color: #fff; letter-spacing: -0.02em; }
      h2 { font-size: 1.15rem; font-weight: 600; margin: 0 0 1rem; color: #f8fafc; }
      h3 { font-size: 0.85rem; font-weight: 600; margin: 0 0 0.5rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }
      
      .nav-tabs {
        display: flex;
        gap: 0.5rem;
        background: rgba(255, 255, 255, 0.03);
        padding: 0.3rem;
        border-radius: 8px;
        border: 1px solid var(--border);
      }
      .nav-tab {
        padding: 0.4rem 0.9rem;
        border-radius: 6px;
        font-size: 0.85rem;
        font-weight: 500;
        cursor: pointer;
        color: var(--text-muted);
        transition: all 0.15s ease;
        display: flex;
        align-items: center;
        gap: 0.4rem;
        border: none;
        background: transparent;
      }
      .nav-tab:hover { color: var(--text-main); background: rgba(255, 255, 255, 0.05); }
      .nav-tab.active {
        color: #fff;
        background: var(--bg-card);
        border: 1px solid var(--border-light);
        box-shadow: 0 2px 4px rgba(0,0,0,0.2);
      }
      
      .tab-badge {
        background: var(--accent-red);
        color: #fff;
        font-size: 0.7rem;
        padding: 0.1rem 0.4rem;
        border-radius: 9999px;
        font-weight: 700;
      }
      
      section {
        margin-bottom: 2rem;
        padding: 1.25rem;
        border: 1px solid var(--border);
        border-radius: 10px;
        background: var(--bg-surface);
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
      }
      
      .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1rem; }
      .grid-4 { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; }
      
      .card {
        border: 1px solid var(--border);
        padding: 1rem;
        border-radius: 8px;
        background: var(--bg-card);
        transition: border-color 0.15s ease, transform 0.15s ease, box-shadow 0.15s ease;
      }
      .card.clickable { cursor: pointer; }
      .card.clickable:hover {
        border-color: var(--border-active);
        background: var(--bg-card-hover);
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(0,0,0,0.25);
      }
      .card.selected {
        border-color: var(--border-active);
        box-shadow: 0 0 0 1px var(--border-active), 0 4px 16px rgba(56, 189, 248, 0.15);
        background: #1c2538;
      }
      
      .card-title { font-size: 1rem; font-weight: 600; color: #fff; margin-bottom: 0.5rem; line-height: 1.35; }
      .card-meta-grid {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
        gap: 0.5rem 1rem;
        font-size: 0.85rem;
        margin-top: 0.75rem;
        border-top: 1px solid rgba(255,255,255,0.06);
        padding-top: 0.75rem;
      }
      .meta-item { display: flex; flex-direction: column; }
      .meta-label { color: var(--text-muted); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; }
      .meta-val { font-weight: 500; color: #e2e8f0; }
      
      .pill-bar {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 0.5rem;
        margin-bottom: 1rem;
      }
      .pill-group {
        display: inline-flex;
        background: rgba(0,0,0,0.3);
        border: 1px solid var(--border);
        border-radius: 6px;
        padding: 0.2rem;
      }
      .filter-pill {
        border: none;
        background: transparent;
        color: var(--text-muted);
        padding: 0.25rem 0.65rem;
        font-size: 0.78rem;
        font-weight: 600;
        border-radius: 4px;
        cursor: pointer;
        transition: all 0.15s ease;
      }
      .filter-pill:hover { color: #fff; }
      .filter-pill.active {
        background: var(--accent-blue);
        color: #0b1120;
      }
      
      .badge {
        display: inline-flex;
        align-items: center;
        gap: 0.25rem;
        padding: 0.18rem 0.5rem;
        border-radius: 9999px;
        font-size: 0.72rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .badge.incident { background: rgba(56, 189, 248, 0.15); color: var(--accent-blue); border: 1px solid rgba(56, 189, 248, 0.3); }
      .badge.condition { background: rgba(245, 158, 11, 0.15); color: var(--accent-amber); border: 1px solid rgba(245, 158, 11, 0.3); }
      .badge.situation { background: rgba(168, 85, 247, 0.15); color: var(--accent-purple); border: 1px solid rgba(168, 85, 247, 0.3); }
      
      .badge.phase-active { background: rgba(16, 185, 129, 0.15); color: var(--accent-green); border: 1px solid rgba(16, 185, 129, 0.3); }
      .badge.phase-scheduled { background: rgba(6, 182, 212, 0.15); color: var(--accent-cyan); border: 1px solid rgba(6, 182, 212, 0.3); }
      .badge.phase-historical { background: rgba(148, 163, 184, 0.15); color: var(--text-muted); border: 1px solid rgba(148, 163, 184, 0.3); }
      .badge.phase-ended { background: rgba(100, 116, 139, 0.15); color: #cbd5e1; border: 1px solid rgba(100, 116, 139, 0.3); }
      
      .badge.traj-escalating { background: rgba(239, 68, 68, 0.2); color: var(--accent-red); border: 1px solid rgba(239, 68, 68, 0.4); animation: pulse 2s infinite; }
      .badge.traj-stable { background: rgba(56, 189, 248, 0.15); color: var(--accent-blue); border: 1px solid rgba(56, 189, 248, 0.3); }
      .badge.traj-de_escalating { background: rgba(16, 185, 129, 0.15); color: var(--accent-green); border: 1px solid rgba(16, 185, 129, 0.3); }
      
      @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.6; }
      }
      
      .stat-val { font-size: 1.8rem; font-weight: 700; line-height: 1.1; margin-top: 0.25rem; }
      .mono { font-family: var(--font-mono); }
      .good { color: var(--accent-green); }
      .muted { color: var(--text-muted); }
      .warn { color: var(--accent-amber); }
      .err { color: var(--accent-red); }
      .tag { background: rgba(255,255,255,0.06); padding: 0.15rem 0.45rem; border-radius: 4px; font-size: 0.75rem; }
      
      table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
      th, td { border-bottom: 1px solid var(--border); padding: 0.65rem 0.75rem; text-align: left; }
      th { color: var(--text-muted); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em; background: rgba(255,255,255,0.02); }
      tr:hover td { background: rgba(255,255,255,0.02); }
      a { color: var(--accent-blue); text-decoration: none; }
      a:hover { text-decoration: underline; }
      
      .btn {
        padding: 0.35rem 0.75rem;
        border-radius: 6px;
        font-size: 0.78rem;
        font-weight: 600;
        cursor: pointer;
        border: 1px solid transparent;
        transition: all 0.15s ease;
      }
      .btn-approve { background: rgba(16, 185, 129, 0.2); color: #34d399; border-color: rgba(16, 185, 129, 0.4); }
      .btn-approve:hover { background: rgba(16, 185, 129, 0.35); color: #fff; }
      .btn-reject { background: rgba(239, 68, 68, 0.2); color: #f87171; border-color: rgba(239, 68, 68, 0.4); }
      .btn-reject:hover { background: rgba(239, 68, 68, 0.35); color: #fff; }
      
      .timeline-box {
        position: relative;
        padding-left: 1.5rem;
        margin-top: 1rem;
      }
      .timeline-box::before {
        content: '';
        position: absolute;
        left: 6px;
        top: 4px;
        bottom: 4px;
        width: 2px;
        background: var(--border);
      }
      .timeline-step {
        position: relative;
        margin-bottom: 1rem;
      }
      .timeline-step::before {
        content: '';
        position: absolute;
        left: -1.5rem;
        top: 6px;
        width: 10px;
        height: 10px;
        border-radius: 50%;
        background: var(--accent-blue);
        border: 2px solid var(--bg-surface);
      }
      
      .meter-bar {
        height: 6px;
        border-radius: 3px;
        background: rgba(255,255,255,0.1);
        overflow: hidden;
        margin-top: 0.3rem;
      }
      .meter-fill {
        height: 100%;
        border-radius: 3px;
        background: linear-gradient(90deg, var(--accent-blue), var(--accent-indigo));
      }
    </style>
  </head>
  <body>
    <header>
      <div class="brand">
        <div class="brand-logo">Ω</div>
        <div>
          <h1>World-State Intelligence Engine</h1>
          <div class="muted mono" style="font-size:0.78rem;margin-top:0.15rem">
            Puget Sound Persistent Event Architecture (Stage 1)
          </div>
        </div>
      </div>
      
      <div style="display:flex;align-items:center;gap:1.5rem">
        <nav class="nav-tabs" id="view-tabs">
          <button class="nav-tab active" onclick="switchView('events')">Events</button>
          <button class="nav-tab" onclick="switchView('review')">Review Queue <span id="review-badge" class="tab-badge" style="display:none">0</span></button>
          <button class="nav-tab" onclick="switchView('coverage')">Coverage Matrix</button>
          <button class="nav-tab" onclick="switchView('ledger')">Observations Ledger</button>
        </nav>
        <div id="live-indicator" class="mono" style="display:flex;align-items:center;gap:0.4rem;font-size:0.8rem">
          <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent-green);box-shadow:0 0 8px var(--accent-green)"></span>
          <span>LIVE</span>
        </div>
      </div>
    </header>

    <!-- Overview Statistics -->
    <section id="overview-section">
      <div class="grid-4" id="overview-grid">
        <div class="card"><div class="muted">Loading world state...</div></div>
      </div>
    </section>

    <!-- Main View: Events -->
    <main id="view-events">
      <div class="pill-bar">
        <div style="display:flex;align-items:center;gap:0.5rem">
          <span class="meta-label">Phase:</span>
          <div class="pill-group" id="filter-phase-group">
            <button class="filter-pill" onclick="setPhaseFilter('')">All</button>
            <button class="filter-pill active" onclick="setPhaseFilter('active')">Active</button>
            <button class="filter-pill" onclick="setPhaseFilter('scheduled')">Scheduled</button>
            <button class="filter-pill" onclick="setPhaseFilter('historical')">Historical</button>
            <button class="filter-pill" onclick="setPhaseFilter('ended')">Ended</button>
          </div>
        </div>

        <div style="display:flex;align-items:center;gap:0.5rem;margin-left:1rem">
          <span class="meta-label">Kind:</span>
          <div class="pill-group" id="filter-kind-group">
            <button class="filter-pill active" onclick="setKindFilter('')">All</button>
            <button class="filter-pill" onclick="setKindFilter('incident')">Incident</button>
            <button class="filter-pill" onclick="setKindFilter('condition')">Condition</button>
            <button class="filter-pill" onclick="setKindFilter('situation')">Situation</button>
          </div>
        </div>
      </div>

      <section>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem">
          <h2 style="margin:0" id="events-heading">Persistent Events</h2>
          <span class="muted mono" id="events-count" style="font-size:0.8rem"></span>
        </div>
        <div class="grid" id="events-container">
          <div class="card muted">Fetching events...</div>
        </div>
      </section>

      <!-- Event Detail Pane -->
      <section id="detail-section" style="display:none">
        <div id="detail-container"></div>
      </section>
    </main>

    <!-- Main View: Review Queue -->
    <main id="view-review" style="display:none">
      <section>
        <h2>Human & Operator Review Queue</h2>
        <div class="muted" style="margin-bottom:1rem;font-size:0.85rem">
          Stage 1 Section 12: Ambiguous correlation candidates in the <code>[0.45, 0.65)</code> score band and conflicting contradictory claims are safely quarantined here without polluting world-state.
        </div>
        <div id="review-container">
          <div class="card muted">Checking review queue...</div>
        </div>
      </section>
    </main>

    <!-- Main View: Coverage Matrix -->
    <main id="view-coverage" style="display:none">
      <section>
        <h2>Domain Coverage & Feed Status Matrix</h2>
        <div class="muted" style="margin-bottom:1.25rem;font-size:0.85rem">
          Stage 1 Section 3: Puget Sound regional domain breakdown distinguishing genuinely quiet feeds (e.g. USGS earthquake telemetry) from disconnected or failing feeds.
        </div>
        <div class="grid" id="coverage-container">
          <div class="card muted">Loading coverage matrix...</div>
        </div>
      </section>
    </main>

    <!-- Main View: Observations Ledger -->
    <main id="view-ledger" style="display:none">
      <section>
        <h2>Observations Ledger (Raw Provenance)</h2>
        <div class="muted" style="margin-bottom:1rem;font-size:0.85rem">
          Every raw ingest record is assigned a canonical fingerprint and significance class. Routine telemetry (<code>STATE_ONLY</code>) remains strictly in this ledger without spawning artificial events.
        </div>
        <div id="ledger-container">
          <div class="card muted">Loading observations...</div>
        </div>
      </section>
    </main>

    <script>
      const $ = (id) => document.getElementById(id);
      let currentView = 'events';
      let currentPhase = 'active';
      let currentKind = '';
      let selectedEventId = null;
      let cachedEvents = [];
      let pendingReviewCount = 0;

      function switchView(viewName){
        currentView = viewName;
        ['events', 'review', 'coverage', 'ledger'].forEach(v => {
          const el = $('view-' + v);
          if(el) el.style.display = (v === viewName) ? 'block' : 'none';
        });
        const tabs = document.querySelectorAll('#view-tabs .nav-tab');
        tabs.forEach((tab, idx) => {
          const names = ['events', 'review', 'coverage', 'ledger'];
          if(names[idx] === viewName) tab.classList.add('active');
          else tab.classList.remove('active');
        });
        refresh();
      }

      function setPhaseFilter(p){
        currentPhase = p;
        document.querySelectorAll('#filter-phase-group .filter-pill').forEach(btn => {
          const isMatch = (btn.textContent.trim().toLowerCase() === (p || 'all'));
          if(isMatch) btn.classList.add('active'); else btn.classList.remove('active');
        });
        refreshEvents();
      }

      function setKindFilter(k){
        currentKind = k;
        document.querySelectorAll('#filter-kind-group .filter-pill').forEach(btn => {
          const isMatch = (btn.textContent.trim().toLowerCase() === (k || 'all'));
          if(isMatch) btn.classList.add('active'); else btn.classList.remove('active');
        });
        refreshEvents();
      }

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

      async function j(url, opts = {}){
        const r = await fetch(url, opts);
        if(!r.ok) throw new Error(url + ' ' + r.status);
        return r.json();
      }

      async function refresh(){
        try {
          await Promise.all([
            refreshOverview(),
            refreshEvents(),
            refreshReviewQueue(),
            currentView === 'coverage' ? refreshCoverage() : Promise.resolve(),
            currentView === 'ledger' ? refreshLedger() : Promise.resolve()
          ]);
        } catch(e){
          console.error('Refresh error:', e);
        }
      }

      async function refreshOverview(){
        let world = null, cov = null, queue = null;
        try { world = await j('/v1/world?include_closed=false'); } catch(e){}
        try { cov = await j('/v1/sources/coverage'); } catch(e){}
        try { queue = await j('/v1/events/review/queue'); } catch(e){}

        if(queue){
          pendingReviewCount = queue.total_pending || 0;
          const badge = $('review-badge');
          if(pendingReviewCount > 0){
            badge.textContent = pendingReviewCount;
            badge.style.display = 'inline-block';
          } else {
            badge.style.display = 'none';
          }
        }

        const activeEvs = cachedEvents.filter(e => (e.phase || e.status) === 'active').length;
        const totalObs = world ? world.observation_total : 0;
        const healthySrcs = cov ? cov.healthy_sources : 0;
        const totalSrcs = cov ? cov.total_sources : 0;

        $('overview-grid').innerHTML = `
          <div class="card">
            <span class="meta-label">Active Persistent Events</span>
            <div class="stat-val good">${activeEvs}</div>
            <div class="muted" style="font-size:0.78rem;margin-top:0.25rem">Stage 1 Filtered & Correlated</div>
          </div>
          <div class="card">
            <span class="meta-label">Observations Ledger</span>
            <div class="stat-val mono">${totalObs.toLocaleString()}</div>
            <div class="muted" style="font-size:0.78rem;margin-top:0.25rem">Routine Telemetry Filtered</div>
          </div>
          <div class="card">
            <span class="meta-label">Data Sources Coverage</span>
            <div class="stat-val">${healthySrcs} <span class="muted" style="font-size:0.95rem;font-weight:400">/ ${totalSrcs}</span></div>
            <div class="muted" style="font-size:0.78rem;margin-top:0.25rem">Puget Sound Multi-Source Feeds</div>
          </div>
          <div class="card">
            <span class="meta-label">Review Queue Items</span>
            <div class="stat-val ${pendingReviewCount > 0 ? 'warn' : 'good'}">${pendingReviewCount}</div>
            <div class="muted" style="font-size:0.78rem;margin-top:0.25rem">Ambiguous Candidates & Conflicts</div>
          </div>
        `;
      }

      async function refreshEvents(){
        let q = '/v1/events?limit=60';
        if(currentPhase) q += '&phase=' + encodeURIComponent(currentPhase);
        if(currentKind) q += '&kind=' + encodeURIComponent(currentKind);

        const res = await j(q);
        cachedEvents = res.events || [];
        $('events-count').textContent = `Showing ${cachedEvents.length} events`;

        if(!cachedEvents.length){
          $('events-container').innerHTML = `
            <div class="card muted" style="grid-column: 1 / -1; padding: 2rem; text-align: center;">
              No persistent events matching phase: <strong>${currentPhase || 'all'}</strong>, kind: <strong>${currentKind || 'all'}</strong>.
            </div>`;
          return;
        }

        if(!selectedEventId && cachedEvents.length){
          selectedEventId = cachedEvents[0].event_id;
        }

        let html = '';
        cachedEvents.forEach(e => {
          const isSel = e.event_id === selectedEventId;
          const kind = (e.kind || 'incident').toLowerCase();
          const phase = (e.phase || e.status || 'active').toLowerCase();
          const title = e.title || ('Incident ' + e.event_id);
          const loc = e.location || (e.geometry ? 'Corridor Geocoded' : 'Regional');
          const conf = typeof e.confidence === 'number' ? (e.confidence * 100).toFixed(0) + '%' : '85%';
          
          let trajBadge = '';
          if(e.trajectory){
            const tcls = 'traj-' + e.trajectory.toLowerCase();
            trajBadge = `<span class="badge ${tcls}">${e.trajectory}</span>`;
          }

          html += `
            <div class="card clickable ${isSel ? 'selected' : ''}" id="card-${e.event_id}" onclick="selectEvent('${e.event_id}')">
              <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:0.4rem;margin-bottom:0.4rem">
                <div style="display:flex;gap:0.35rem;flex-wrap:wrap">
                  <span class="badge ${kind}">${kind}</span>
                  <span class="badge phase-${phase}">${phase}</span>
                  ${trajBadge}
                </div>
                <span class="mono muted" style="font-size:0.75rem">${e.event_id}</span>
              </div>
              <div class="card-title">${title}</div>
              <div class="card-meta-grid">
                <div class="meta-item">
                  <span class="meta-label">Location</span>
                  <span class="meta-val" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${loc}</span>
                </div>
                <div class="meta-item">
                  <span class="meta-label">Type</span>
                  <span class="meta-val tag">${e.dominant_type || 'incident'}</span>
                </div>
                <div class="meta-item">
                  <span class="meta-label">Evidence Provenance</span>
                  <span class="meta-val">${e.source_count || 1} src (${e.independent_source_count || 1} indep)</span>
                </div>
                <div class="meta-item">
                  <span class="meta-label">Documents</span>
                  <span class="meta-val">${e.distinct_document_count || 1} docs (${e.observation_count || 1} obs)</span>
                </div>
                <div class="meta-item">
                  <span class="meta-label">Confidence</span>
                  <span class="meta-val good">${conf}</span>
                </div>
                <div class="meta-item">
                  <span class="meta-label">Last Updated</span>
                  <span class="meta-val mono">${fmt(e.updated_at || e.last_reconstructed_at)}</span>
                </div>
              </div>
            </div>
          `;
        });
        $('events-container').innerHTML = html;

        if(selectedEventId){
          loadDetail(selectedEventId);
        }
      }

      async function selectEvent(eid){
        selectedEventId = eid;
        document.querySelectorAll('#events-container .card').forEach(c => c.classList.remove('selected'));
        const activeCard = $('card-' + eid);
        if(activeCard) activeCard.classList.add('selected');
        await loadDetail(eid);
        $('detail-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
      }

      async function loadDetail(eid){
        try {
          const [d, evd] = await Promise.all([
            j('/v1/events/' + eid),
            j('/v1/events/' + eid + '/evidence').catch(() => ({}))
          ]);
          if(d && evd && evd.observations){
            d.observations = evd.observations;
          }
          renderDetail(d);
          $('detail-section').style.display = 'block';
        } catch(e){
          console.error('Error loading detail:', e);
        }
      }

      function renderDetail(d){
        if(!d) return;
        const ev = d.event || {};
        const cs = d.current_state || {};
        const title = ev.title || ('Event ' + ev.event_id);
        const loc = ev.location || (ev.geometry ? 'Corridor Geocoded' : 'Regional');
        const kind = (ev.kind || 'incident').toLowerCase();
        const phase = (ev.phase || ev.status || 'active').toLowerCase();
        const supportingObs = d.observations || [];
        const timeline = ev.timeline_entries || [];
        const contradictions = ev.contradictions || [];
        const candidates = ev.candidates || [];
        const relations = ev.relations || [];

        let trajHtml = '';
        if(ev.trajectory){
          trajHtml = `<span class="badge traj-${ev.trajectory.toLowerCase()}">${ev.trajectory}</span>`;
        }

        let relationsHtml = '';
        if(relations.length || ev.parent_event_id || (ev.child_event_ids && ev.child_event_ids.length)){
          relationsHtml = '<div style="margin-top:0.5rem;padding:0.6rem;background:rgba(0,0,0,0.25);border-radius:6px;font-size:0.8rem">';
          if(ev.parent_event_id){
            relationsHtml += `<div>Parent Event: <a href="#detail-section" onclick="selectEvent('${ev.parent_event_id}')" class="mono good">${ev.parent_event_id}</a></div>`;
          }
          if(ev.child_event_ids && ev.child_event_ids.length){
            relationsHtml += `<div>Child Events (${ev.child_event_ids.length}): ${ev.child_event_ids.map(c => `<a href="#detail-section" onclick="selectEvent('${c}')" class="mono tag">${c}</a>`).join(' ')}</div>`;
          }
          relationsHtml += '</div>';
        }

        let contradictionHtml = '';
        if(contradictions.length){
          contradictionHtml = `
            <div style="margin-top:1rem;border:1px solid rgba(239, 68, 68, 0.4);background:rgba(239, 68, 68, 0.08);padding:0.75rem;border-radius:6px">
              <h3 style="color:var(--accent-red);margin-bottom:0.4rem">Conflicting Claims Detected (${contradictions.length})</h3>
              ${contradictions.map(c => `
                <div style="font-size:0.8rem;margin-bottom:0.35rem">
                  <strong>${c.field_name}:</strong> Claim A: <code>${c.claim_a_value}</code> vs Claim B: <code>${c.claim_b_value}</code>
                  <span class="muted mono">(${c.status})</span>
                </div>
              `).join('')}
            </div>`;
        }

        let timelineHtml = '';
        if(timeline.length){
          timelineHtml = '<div class="timeline-box">';
          timeline.forEach(t => {
            const factors = (t.causal_factors || []).map(f => `<span class="tag" style="font-size:0.7rem">${f}</span>`).join(' ');
            timelineHtml += `
              <div class="timeline-step">
                <div class="mono" style="font-size:0.8rem;font-weight:600">
                  v${t.state_version} • ${fmt(t.reconstructed_at)} <span class="tag">${t.event_phase || '-'}</span>
                </div>
                <div style="font-size:0.82rem;margin-top:0.15rem">${t.explanation || 'State update reconstructed.'}</div>
                ${factors ? `<div style="margin-top:0.25rem">${factors}</div>` : ''}
              </div>`;
          });
          timelineHtml += '</div>';
        } else {
          timelineHtml = '<div class="muted" style="font-size:0.85rem">Initial state version v1.</div>';
        }

        let obsRows = '';
        if(supportingObs.length){
          supportingObs.forEach(o => {
            const geomStr = o.centroid ? o.centroid.join(', ') : '-';
            const headline = o.headline || (o.structured_payload ? JSON.stringify(o.structured_payload).slice(0, 90) : '-');
            obsRows += `
              <tr>
                <td class="mono">${fmt(o.observed_at || o.event_time)}</td>
                <td><span class="tag">${o.source_id}</span></td>
                <td>${o.observation_type || '-'}</td>
                <td class="mono">${geomStr}</td>
                <td><strong>${headline}</strong></td>
                <td>${o.source_url ? '<a href="'+o.source_url+'" target="_blank">source</a>' : '-'}</td>
              </tr>`;
          });
        } else {
          obsRows = '<tr><td colspan="6" class="muted">No supporting observation records retrieved.</td></tr>';
        }

        const confPct = typeof ev.confidence === 'number' ? Math.round(ev.confidence * 100) : 85;

        $('detail-container').innerHTML = `
          <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;margin-bottom:1.25rem">
            <div>
              <div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.35rem">
                <h2 style="margin:0">${title}</h2>
                <span class="badge ${kind}">${kind}</span>
                <span class="badge phase-${phase}">${phase}</span>
                ${trajHtml}
              </div>
              <div class="muted mono" style="font-size:0.82rem">
                Event ID: ${ev.event_id} | Location: ${loc}
              </div>
              ${relationsHtml}
            </div>
            <div style="text-align:right" class="mono muted">
              <div style="font-size:0.8rem">First Observed: ${fmt(ev.first_observed)}</div>
              <div style="font-size:0.8rem">Last Updated: ${fmt(ev.updated_at || ev.last_reconstructed_at)}</div>
              ${ev.closed_at ? `<div style="font-size:0.8rem;color:var(--text-muted)">Closed At: ${fmt(ev.closed_at)}</div>` : ''}
            </div>
          </div>

          <div class="grid" style="margin-bottom:1.5rem">
            <!-- Stage 1 Honest Evidence Card -->
            <div class="card">
              <h3>Stage 1 Honest Evidence & Provenance</h3>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.75rem;margin-top:0.75rem">
                <div>
                  <span class="meta-label">Independent Sources</span>
                  <div class="stat-val good" style="font-size:1.4rem">${ev.independent_source_count || 1} <span class="muted" style="font-size:0.85rem">/ ${ev.source_count || 1} total</span></div>
                </div>
                <div>
                  <span class="meta-label">Distinct Documents</span>
                  <div class="stat-val" style="font-size:1.4rem">${ev.distinct_document_count || 1} <span class="muted" style="font-size:0.85rem">(${ev.observation_count || 1} obs)</span></div>
                </div>
              </div>
              <div style="margin-top:0.75rem">
                <div style="display:flex;justify-content:space-between;font-size:0.8rem">
                  <span class="meta-label">Event Confidence Score</span>
                  <strong class="good">${confPct}%</strong>
                </div>
                <div class="meter-bar">
                  <div class="meter-fill" style="width:${confPct}%"></div>
                </div>
                <div class="muted" style="font-size:0.72rem;margin-top:0.35rem">
                  Anchored on authority + independent corroboration; raw poll volume creates 0 artificial confidence.
                </div>
              </div>
              ${contradictionHtml}
            </div>

            <!-- Stage 1 Timeline & Evolution -->
            <div class="card">
              <h3>Evolution & Causal Timeline</h3>
              ${timelineHtml}
            </div>
          </div>

          <!-- Multi-Source Observations Table -->
          <div>
            <h3>Multi-Source Evidence Ledger (${supportingObs.length})</h3>
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Source</th>
                  <th>Observation Type</th>
                  <th>Coordinates</th>
                  <th>Headline / Signal</th>
                  <th>Link</th>
                </tr>
              </thead>
              <tbody>
                ${obsRows}
              </tbody>
            </table>
          </div>
        `;
      }

      async function refreshReviewQueue(){
        const data = await j('/v1/events/review/queue');
        const candidates = data.candidates || [];
        const contradictions = data.contradictions || [];

        if(!candidates.length && !contradictions.length){
          $('review-container').innerHTML = `
            <div class="card" style="padding:2rem;text-align:center">
              <span class="good" style="font-size:1.5rem">✓</span>
              <div style="margin-top:0.5rem;font-weight:600">Review Queue is Clear</div>
              <div class="muted" style="font-size:0.82rem;margin-top:0.25rem">No ambiguous correlation candidates or open claim contradictions require review.</div>
            </div>`;
          return;
        }

        let html = '';
        if(candidates.length){
          html += `<h3>Correlation Candidates Awaiting Decision (${candidates.length})</h3><div class="grid" style="margin-bottom:1.5rem">`;
          candidates.forEach(c => {
            const scorePct = (c.score * 100).toFixed(1);
            const comps = c.score_components || {};
            html += `
              <div class="card" id="cand-${c.candidate_id}">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.5rem">
                  <span class="badge phase-scheduled">Score: ${scorePct}%</span>
                  <span class="mono muted" style="font-size:0.75rem">${c.candidate_id}</span>
                </div>
                <div style="font-size:0.85rem;margin-bottom:0.5rem">
                  Candidate for Event: <a href="#detail-section" onclick="selectEvent('${c.event_id}')" class="mono good">${c.event_id}</a>
                </div>
                <div class="mono muted" style="font-size:0.75rem;margin-bottom:0.75rem">
                  Record: ${c.source_record_id}<br/>
                  Spatial: ${(comps.spatial || 0).toFixed(2)} | Temporal: ${(comps.temporal || 0).toFixed(2)} | Semantic: ${(comps.semantic || 0).toFixed(2)}
                </div>
                <div style="display:flex;gap:0.5rem">
                  <button class="btn btn-approve" onclick="reviewCandidate('${c.candidate_id}', 'approve')">Approve Match</button>
                  <button class="btn btn-reject" onclick="reviewCandidate('${c.candidate_id}', 'reject')">Reject</button>
                </div>
              </div>
            `;
          });
          html += `</div>`;
        }

        if(contradictions.length){
          html += `<h3>Unresolved Contradictions (${contradictions.length})</h3><table><thead><tr><th>Event ID</th><th>Field</th><th>Claim A</th><th>Claim B</th><th>Created</th></tr></thead><tbody>`;
          contradictions.forEach(c => {
            html += `
              <tr>
                <td><a href="#detail-section" onclick="selectEvent('${c.event_id}')" class="mono good">${c.event_id}</a></td>
                <td><strong>${c.field_name}</strong></td>
                <td><code>${c.claim_a_value}</code></td>
                <td><code>${c.claim_b_value}</code></td>
                <td class="mono">${fmt(c.created_at)}</td>
              </tr>
            `;
          });
          html += `</tbody></table>`;
        }

        $('review-container').innerHTML = html;
      }

      async function reviewCandidate(candId, action){
        try {
          await j(`/v1/events/review/candidate/${candId}?action=${action}`, { method: 'POST' });
          const card = $('cand-' + candId);
          if(card){
            card.style.opacity = '0.5';
            card.innerHTML = `<div class="good" style="padding:1rem;text-align:center">Marked ${action}ed</div>`;
          }
          setTimeout(refresh, 800);
        } catch(e){
          alert('Action failed: ' + e.message);
        }
      }

      async function refreshCoverage(){
        const data = await j('/v1/sources/coverage');
        const domains = data.domains || [];

        let html = '';
        domains.forEach(d => {
          const healthyPct = d.total_sources > 0 ? Math.round((d.healthy_sources / d.total_sources) * 100) : 0;
          html += `
            <div class="card">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.4rem">
                <strong style="text-transform:capitalize">${d.domain.replace('_', ' ')}</strong>
                <span class="mono ${healthyPct === 100 ? 'good' : 'warn'}" style="font-size:0.8rem">${d.healthy_sources}/${d.total_sources} Healthy</span>
              </div>
              <div class="meter-bar" style="margin-bottom:0.75rem">
                <div class="meter-fill" style="width:${healthyPct}%;background:${healthyPct === 100 ? 'var(--accent-green)' : 'var(--accent-amber)'}"></div>
              </div>
              <div class="muted" style="font-size:0.78rem;margin-bottom:0.5rem">Producing Data: <strong>${d.producing_data_sources}</strong> / ${d.total_sources}</div>
              <div style="display:flex;flex-direction:column;gap:0.35rem">
                ${(d.sources || []).map(s => {
                  const isHealthy = (s.state === 'healthy' || s.state === 'active');
                  const isProducing = (s.records_received > 0);
                  const statusTag = isHealthy ? (isProducing ? '<span class="tag good">Active Data</span>' : '<span class="tag muted">Quiet Feed</span>') : '<span class="tag err">Feed Degraded</span>';
                  return `
                    <div style="display:flex;justify-content:space-between;align-items:center;font-size:0.78rem;padding:0.25rem 0;border-top:1px solid rgba(255,255,255,0.04)">
                      <span class="mono">${s.source_id}</span>
                      <div>${statusTag}</div>
                    </div>`;
                }).join('')}
              </div>
            </div>
          `;
        });
        $('coverage-container').innerHTML = html;
      }

      async function refreshLedger(){
        const data = await j('/v1/observations?limit=40');
        const obs = data.observations || [];
        let html = `<table><thead><tr><th>Time</th><th>Source</th><th>Type</th><th>Location</th><th>Headline / Payload</th><th>Resolved Event</th></tr></thead><tbody>`;
        if(!obs.length){
          html += '<tr><td colspan="6" class="muted">No observations in ledger.</td></tr>';
        } else {
          obs.forEach(o => {
            const loc = o.centroid ? o.centroid.join(', ') : '-';
            const headline = o.headline || (o.structured_payload ? JSON.stringify(o.structured_payload).slice(0, 90) : '-');
            const resolvedLink = o.event_id ? `<a href="#detail-section" onclick="selectEvent('${o.event_id}')" class="mono good">${o.event_id}</a>` : '<span class="muted">Filtered / Telemetry</span>';
            html += `
              <tr>
                <td class="mono">${fmt(o.observed_at || o.event_time)}</td>
                <td><span class="tag">${o.source_id}</span></td>
                <td>${o.observation_type || '-'}</td>
                <td class="mono">${loc}</td>
                <td>${headline}</td>
                <td>${resolvedLink}</td>
              </tr>
            `;
          });
        }
        html += '</tbody></table>';
        $('ledger-container').innerHTML = html;
      }

      // Initial boot
      refresh();
      setInterval(refresh, 10000);
    </script>
  </body>
</html>
"""
