# InfraImpact

Dynamic Infrastructure Impact Intelligence Platform — a real-time, user-centred
system that turns chaotic event signals into evidence-backed, personalised
infrastructure impacts.

It does not answer "what is happening?". It answers:

> what is happening → what infrastructure is affected → how conditions are
> changing → how this affects *this* user → what information or action matters
> to them now.

Nothing about the workflow is hardcoded. A peaceful demonstration may stay
informational for its entire life; another event may suddenly intersect a
road, a transit line, emergency access, or someone's commute. The event decides
the analysis.

## Architecture

```
signals → observation ledger → event resolution → evolving event state
        → LLM + JEV + predictive ML + graph/geospatial analysis
        → state-change detection → user exposure → dynamic prioritisation
        → personalised interface and alerts
```

| Layer | Package | Responsibility |
|---|---|---|
| Domain | `domain/` | Taxonomy, schemas, geometry, ids |
| Ingestion | `sources/`, `ingestion/` | Source adapters, normalisation, dedupe |
| Ledger & state | `storage/`, `events/` | Immutable observations, event resolution, world state |
| Reasoning | `llm/`, `jev/`, `analysis/`, `graph/`, `delta/` | Interpretation, bounded decisions, prediction, propagation, change detection |
| Delivery | `users/`, `api/`, `bus/` | Exposure, priority, presentation, notifications, HTTP + SSE |
| Runtime | `runtime.py` | The loop that wires it together |

The **observation ledger is append-only**. New information never overwrites
prior information; it produces a new observation and a new analysis snapshot.
That is what makes "your route became affected 4 minutes ago" expressible
instead of re-announcing an unchanged event.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e .
$env:PYTHONPATH="src"

# Run the loop against the captured Puget Sound signals
.\.venv\Scripts\python -m infraimpact.runtime

# Serve the API (17 routes, OpenAPI at /docs)
.\.venv\Scripts\python -m uvicorn infraimpact.api.app:app --reload
```

```powershell
.\.venv\Scripts\python -m pytest tests/ -q
```

## Configuration

All configuration is environment-driven with safe local defaults; nothing is
hardcoded to a region in logic. See `src/infraimpact/config.py`.

| Variable | Default | Purpose |
|---|---|---|
| `INFRAIMPACT_DATA_ROOT` | autodetected | Workspace root holding `live_feeds/` and `data/` |
| `INFRAIMPACT_DATABASE_URL` | `sqlite:///./data/infraimpact.db` | Storage |
| `INFRAIMPACT_DRIVER` | `sqlite` | `sqlite` locally, `postgis` in production |
| `INFRAIMPACT_REGION` | `puget-sound` | Region profile |
| `INFRAIMPACT_BUS` | `memory` | Event bus backend |
| `INFRAIMPACT_ENABLE_NETWORK` | `false` | Poll live endpoints instead of snapshots |
| `INFRAIMPACT_API_RUN_LOOP` | `true` | Run the runtime loop inside the API |
| `OPENROUTER_API_KEY` / `LLM_API_KEY` | — | LLM credentials (see `llm/.env.example`) |
| `WSDOT_API_KEY`, `SDOT_API_KEY`, `ONEBUSAWAY_API_KEY` | — | Optional live transport credentials |

## Signals

The catalogue in `src/infraimpact/sources/catalog.py` describes every ingestible
signal: 36 entries across the 15 signal families the platform covers, mapped to
46 captured snapshot files. Notable properties:

- **Duplicate suppression.** Several snapshot filenames are byte-identical.
  Adapters read only the first file that exists, so duplicates never
  double-count into the ledger.
- **Region filtering.** The NWS alert snapshot is nationwide and USGS is
  global. Records whose geometry falls outside the region bounding box are
  dropped at the adapter boundary, so a flood advisory for another state can
  never be resolved into a local event.
- **Honest gaps.** Signals whose payload does not contain what the filename
  claims are registered `usable=False` with a note. Traffic speeds, ferry
  bulletins, Sound Transit alerts and truck restrictions are **not** observable
  from this snapshot set, and the system says so rather than implying coverage
  it lacks.

## Data layout

| Path | Contents |
|---|---|
| `live_feeds/` | 46 captured regional signal snapshots (~10 MB) |
| `collectors/` | Scripts that re-fetch `live_feeds/` and the datasets |
| `data/regional/` | Washington & Seattle records — can change an alert |
| `data/reference/` | Static GIS + King County Metro GTFS |
| `data/research/` | Research corpora — baselines and comparison only |
| `unused/` | Superseded prior work, parked and untracked |

See [`data/README.md`](data/README.md) for what each dataset contains, its
schema, and its regional relevance. Bulk payloads are gitignored because they
are large and reproducible; structure and documentation are tracked.

## Design commitments

These are enforced in code, not just documented:

- **The LLM never decides.** It structures unstructured text, proposes event
  merges, and recommends capabilities. Safety-critical functions, official
  emergency instructions, and authoritative closures are deterministic.
- **No invented facts.** Predictions cannot manufacture exposure — forecast
  risk is capped at 10% of exposure score. Prohibitions (§20) are checked
  before any LLM output is written.
- **Every notification has a reason.** A screen-instruction check runs over
  every emitted item; a notification that cannot be justified from evidence is
  not emitted.
- **No raw model internals reach clients.** The `/v1` response models have no
  field for model outputs, features, or decisions. `/internal/models/evaluate`
  is the deliberate operator escape hatch.
- **Absolute state and delta are both tracked.** Alerts say what *changed*
  since the previous analysis, not just what is currently true.
- **Failures lower confidence, they do not fake certainty.** A source that
  cannot be reached marks itself unavailable so analysis confidence drops
  rather than silently treating silence as "nothing happening".

## Known gaps

Not yet implemented, and stated rather than hidden:

- Live (non-snapshot) polling for WSDOT/SDOT/Metro needs API keys.
- Seattle Fire, utilities and AlertSeattle adapters are declared but not built.
- ACLED and historical retrieval are declared in config with no adapter.
- Model ensembles, spatiotemporal ML, and continuous evaluation are not built.
