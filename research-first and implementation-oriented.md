# Research-First and Implementation-Oriented

Companion to `WORLD-STATE ENGINE.md`. That document argues for the architecture —
a continuous world-state layer feeding event-specific intelligence, with Jev
near the end rather than at the front. This one answers a narrower question:

> Given that architecture, what does the code actually do, and what does it
> refuse to do?

Every claim below names a module and is checked by a test. Where the build stops,
this document says so rather than describing the intention.

---

## 1. The research-first principle

Research-first means the system earns the right to make a claim by measuring
first, and can always show the measurement that justified it.

Concretely, in this codebase, that is four commitments:

1. **The evidence is immutable and precedes the belief.** Observations are
   append-only. State versions are append-only. Every belief is a pure function
   of a set of observations, so any state can be re-derived and audited.
2. **Uncertainty travels with the number.** A magnitude without an interval, a
   link without a confidence, a claim without provenance is not a result — it is
   a number someone liked.
3. **Absence is a reportable value.** "Not measured" and "measured as zero" are
   different claims. The system distinguishes them everywhere.
4. **A model without weights reports that it has none.** No synthetic heuristics,
   no default confidences, no empty result dressed as a finding.

Commitments 3 and 4 are where most of the engineering effort went, because they
are the ones that are easy to violate by accident.

---

## 2. Layer map

The flow from `WORLD-STATE ENGINE.md`, with the module that implements each stage.

| Stage | Module | Notes |
|---|---|---|
| Ingestion / raw preservation | `sources/`, `adapter.py`, `collector.py` | 40 registered adapters in `sources/registry.py` |
| Normalisation | `sources/*` `normalize()` → `Observation` | Typed payload, provenance, quality scores |
| Deduplication | `content_hash` in `domain/ids.py` | Idempotent on `(source_id, source_record_id)` |
| Event resolution | `events/resolver.py` + `events/identity.py` | Collision-safe, idempotent, burst-aware |
| State reconstruction | `events/world_state.py` | Deterministic per event |
| Scale estimation | `events/scale.py` | Recency-weighted robust aggregation |
| Lifecycle | `events/lifecycle.py` | Evidence-based termination |
| Persistence | `storage/sqlite_driver.py`, `storage/repository.py` | Atomic state + delta + lifecycle |
| State delta | `delta/engine.py` | 20 change kinds across state, scale, lifecycle, analysis, exposure |
| Capability selection | `analysis/relevance.py` + `orchestrator.py` | Ranking with cost, not a hardcoded switch |
| Deterministic analysis | `analysis/{prediction,transit,geospatial,graph_engine,statistics,similarity,uncertainty}.py` | No LLM in this path |
| Predictive models | `models/portfolio.py` (A–G), `models/registry.py` | All seven are untrained; F and G have no inference at all |
| LLM interpretation | `llm/interpreter.py` | Extraction, advice, synthesis only |
| User exposure | `users/exposure.py` | Exposure deltas are durable |
| Jev | `jev/client.py` | Bounded decisions, each with a recorded reason |
| Evaluation / learning | `evaluation/{metrics,training,learning}.py` | Brier, ECE, leak-free point-in-time datasets |
| Priority / notify | `users/priority.py`, `users/notifications.py` | |

Postgres parity for the new tables is in `storage/postgres.py` (23 tables). The
Postgres **driver** is deliberately unimplemented — see §7.

---

## 3. What the LLM is allowed to touch

The architectural claim is that the LLM is not in the reconstruction path. That
is enforced structurally, not by convention.

The LLM has exactly five operations (`llm/interpreter.py`):

- `narrate_evidence` — advisory synthesis of existing claims
- `recommend_capabilities` — **reordering only**, see below
- `hypotheses` — non-authoritative suggestions
- `opinion_on_resolution` — advisory input to lifecycle discussion
- `explain_to_user` — presentation prose

It cannot: allocate event identity, reconstruct state, decide event status,
compute exposure, compute a delta, or write a claim.

**On capability advice.** `orchestrator._promote` lets the model reorder what the
relevance engine already selected. It cannot add. The asymmetry is deliberate:
a model asked to *introduce* capabilities would have to know what this build's
registry contains, which it cannot know, and the failure mode is a
plausible-sounding analysis name that does not exist. Reordering is where the
real value is anyway — the engine is good at scoring, and a reader of the same
state can tell that a cheap capability matters more here than its numeric
relevance suggests.

Every LLM call is wrapped. A failure logs and changes nothing.

---

## 4. Capability selection is a ranking, not a filter

`analysis/relevance.py` scores every registered capability on expected relevance
× user impact × information gain × uncertainty reduction ÷ cost. Nothing is
hardcoded to "if protest then run X."

Two gates on top of the score:

- **Materiality gate** (`orchestrator._select`): if the delta is not material,
  the continuous cheap tier still runs and every triggered or expensive
  capability is dropped, with the reason recorded. This is what makes "500
  events, 10 materially changed, 2 affect users" the actual behaviour rather
  than an aspiration.
- **Cost tier**: expensive capabilities below 0.35 expected relevance are not
  executed.

Skipped capabilities keep their reasons in the selection, so a run can explain
what it chose *not* to do.

---

## 5. Honesty contracts

These are the load-bearing behaviours, each pinned by a test.

### 5.1 No measurement, no measurement-shaped output

`TrafficAnomalyCapability` reports deviation from a documented expectation table,
never a deviation from an observation that does not exist.

The build previously fabricated three things, all of which looked measured:

- with a road closure present, an observed ratio of `0.45 × baseline` at
  confidence `0.70`
- mph values from an invented 35 mph free-flow speed, and travel time from a
  hardcoded 600 s nominal
- the literal infrastructure identifier `road:4th-ave` when no segment had been
  identified

Now absolute speeds and travel times are emitted only when the feed reports them
*and* a free-flow reference to compare against. With nothing measured the result
is `NO_MEASUREMENT`, with `observed_ratio: null` and `probability: null`. The
baseline expectation is still reported, because an expectation is a fact about
expectations.

Pinned by `test_traffic_with_no_measurement_invents_nothing`.

### 5.2 A model with no weights cannot look successful

Six of the seven portfolio models have specifications but no trained weights. Two
of them (F: cascade propagation, G: LTR presentation ranking) have no inference
at all, and previously returned `COMPLETED` with empty results — a cascade
across zero edges at confidence 0.80, a ranking of nothing at 0.85.

They now take no `is_trained` flag at all. With no implementation, no value of
that flag could be honest, and honouring it only invited a caller to declare
success it had not earned. They return `status: MODEL_REQUIRED`,
`reason: not_implemented`, `confidence: 0.0`, and state which of the two
different situations applies — "no weights yet" and "no implementation exists"
call for different work, and the reader needs to tell them apart.

Pinned by `test_models_f_and_g_cannot_be_switched_into_claiming_success` and
`test_untrained_models_require_weights_without_fabrication`.

### 5.3 Promotion requires weights

`ModelRegistry.promote` refuses a model with no trained weights and says why.
`POST /internal/models/promote` returns **409** with a reason, distinct from the
404 for an unknown model.

Promoting an untrained champion presents as a healthy deployment that happens to
be silent — the monitoring meant to catch a broken model ends up pointed at the
model that has none.

`is_trained` is still a caller-supplied boolean, so this is a guard against
mistakes, not proof. The real protection is §5.2: no portfolio model can set it
true without an implemented inference behind it.

Pinned by `test_untrained_models_cannot_be_promoted` and
`test_trained_baseline_model_promotes`.

### 5.4 A model's status survives the trip to the metrics

`to_model_output` now carries `status`, `is_placeholder`, `task_type`,
`deployment_mode`, and `notes` from the typed prediction rather than trusting the
free-form payload.

It used to drop all of them, leaving a hand-written string as the only evidence a
model had no weights — and the consumer compared that string against a
differently-cased one. Every untrained model was therefore reported as
`COMPLETED`, and `confidence` fell back to a hardcoded `0.8` when a model
reported neither probability nor uncertainty. An absent confidence is now
`None`; a made-up one reads as a measurement.

Pinned by `test_model_required_for_untrained_models`.

### 5.5 Scale reports its own uncertainty

`events/scale.py` aggregates across sources with recency weights (half-life
derived from the median gap between readings) and a cap that keeps any single
reading from exceeding half the total weight — so one recent outlier from one
source cannot turn a corroborated quantity into a confident 500 on its own.

Each `QuantityEstimate` carries `lower`, `upper`, `confidence`, `source_count`,
`disagreement`, and an explicit `unavailable` flag. Where sources conflict beyond
their intervals, the disagreement is measured and widens the interval rather than
being averaged away.

### 5.6 Identity is allocated, never guessed

`events/identity.py` derives a time/geospatial fingerprint and assigns collision
ordinals deterministically. Re-observing an already-linked observation is a
no-op before any scoring happens, so replaying a feed cannot fragment an event.
The resolver's candidate matching requires corroboration beyond temporal and
spatial proximity, and recovers a closed event only when its termination basis
allows it — an official resolution record is not undone by a late report.

---

## 6. Continuity and cost

- State, deltas, and the lifecycle transition are written in **one
  transaction**. If a delta is not durable, the state version it explains is not
  either. A delta must exist the moment its state version does, or it does not
  exist at all for any event no analysis happened to run on.
- `reconstructed_at` is stored separately from write time. Backfilling a week-old
  incident writes every version today, and "what did we believe at time T" is
  unanswerable from write time alone.
- One world snapshot per cycle answers "what is happening right now" across all
  events in a region, loading the latest state per event in a single query.
  Ordering is deterministic: material change first, then severity, impact,
  confidence, source count, age.
- **Coverage gaps are reported.** A source whose health is degraded, or one that
  is not a realtime source, appears as a coverage gap rather than as silence.
  Absence of data and absence of events must not look the same.

---

## 7. What is not built

Stated plainly, because a gap that is not written down gets discovered in
production.

1. **No Postgres driver.** `storage/postgres_driver.py` raises
   `NotImplementedError` with instructions. The DDL is complete and mirrors the
   SQLite schema; the repository implementation is not written. `psycopg` being
   installed does not make this path usable.
2. **No trained models.** All seven specifications exist in `docs/models/`.
   Models A–E have interpretable deterministic baselines that a learned model
   would be *measured against*; none are learned. F and G have nothing.
3. **No cascade propagation, no learned ranking.** Models F and G.
4. **No Kafka replay.** `InMemoryEventBus.replay_history` serves buffered
   envelopes; the Kafka bus raises `NotImplementedError` on it, deferring to a
   ledger that does not expose that interface yet. Only the in-memory bus is
   replay-capable in this build.
5. **The LLM is configured but its outputs are unexercised in the default
   build.** `build_llm_client()` returns a `BigPickleClient` and
   `INFRAIMPACT_ENABLE_LLM` defaults to on, so `is_configured` is true. But
   `InterpretationLayer.operations` is empty until an operation actually
   succeeds, so in CI — which runs no LLM — every one of the five LLM passes in
   §3 is a no-op, and the deterministic path is what the 428 tests exercise.
   Worth stating plainly: the LLM code paths are structurally wired and
   fail-safe, but they are not covered by the suite.

---

## 8. Verifying

```bash
.venv/bin/python -m pytest tests/ -q          # 428 passed, 4 skipped
.venv/bin/python scripts/verify_end_to_end_lifecycle.py
```

The lifecycle script runs all ten phases against synthetic sources and prints the
provenance audit at the end: supporting sources, capabilities actually executed,
which engine performed each deterministic calculation, and the bounded Jev
decisions with their reasons.

To see the honesty contracts fail loudly rather than silently, read §5 and run
the tests named in it. Each one exists because the corresponding fabrication was
in the code and was found by a user of the output, not by the author of it.
