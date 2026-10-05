# Event System Plan (Stage 1)

**Scope:** turn raw feed data into a small number of trustworthy, evolving **events**. No user-impact analysis here. Stage 2 consumes what this stage emits.

**Principle:** the engine models the world, not the feeds. A feed record, a news article, or a dispatch entry is *evidence about* an event. It is never the event.

---

## 1. Vocabulary (fixes the "what is an observation?" confusion)

| Term | Meaning | Count it as evidence? |
| --- | --- | --- |
| **Poll** | One fetch of a source. Happens constantly. | No. Logged only as a heartbeat. |
| **Source record** | One item in the response (a closure, an article, a CAD entry), identified by the source's own ID. | Once per distinct ID. |
| **Observation** | A source record that is **new or changed** since last poll. | Yes, once per version. |
| **Claim** | A fact extracted from an observation ("fatality: true", "road X closed"). | Yes, per claim. |
| **Event** | A real-world occurrence or condition that observations are attached to. | n/a |

Your 3,000 "observations" on one event were mostly polls and re-crawls. After this plan, that event shows roughly: *14 observations, 9 distinct documents, 3 independent sources, 4 source families.*

## 2. Ingestion: stop re-recording the same thing

Every source gets a **source policy**: poll interval, semantics (`event-feed`, `state-feed`, `article-feed`, `schedule-feed`), ID field, and expected update rate.

Per poll:

1. Fetch, then store the raw response (immutable, retention-limited).
2. For each source record, compute `fingerprint = hash(normalized meaningful fields)`. Exclude timestamps of fetch, view counters, and ads.
3. Look up `(source_id, source_record_id)`.
   - Unseen → new observation.
   - Seen, fingerprint changed → new observation version (e.g. closure extended, article updated).
   - Seen, same fingerprint → update `last_confirmed_at` only. **No observation, no event work.**
4. **Disappearance detection:** if a record present in earlier polls is missing for N consecutive successful polls (N per source), emit a `source_record_ended` observation. This is how road closures end.
5. A failed poll never counts as "record disappeared". Only successful polls with a non-empty, sane response can end things.

Articles: dedupe by canonical URL, then content hash, then title/shingle similarity (catches syndication). A re-crawled article is the same document.

## 3. Event kinds

Two kinds of event, plus a parent grouping. Everything else is not an event.

1. **Incident**: a discrete occurrence with a start, usually short-lived. Collision, fire, shooting, arrest, earthquake, medical response.
2. **Condition**: something with an interval: starts, persists, ends. Road closure, bridge restriction, ferry out of service, power outage, planned construction. Tracked from first observation to disappearance, then closed with a duration. This answers "when did the closure end?"
3. **Situation** (parent): a compound, evolving event made of linked incidents and conditions plus reports. Protest that escalates to police response and road closures. May Day. Major storm. Large outage. A situation *contains* child events and has its own timeline and trajectory.

**Not events (stay as state or ledger data):** steady "road clear", "bridge clearance 17'3"", "ferry at dock". These are state. A state only becomes an event when it transitions into something abnormal (clear → closed).

**Phase** is separate from kind: `scheduled`, `active`, `ended`, `historical`. A 2027 farmers market is a Condition in phase `scheduled`, never `active`.

## 4. Significance gate (which observations matter)

Every observation gets a cheap, deterministic **significance class** before correlation:

- `event_candidate`: dispatch with incident type, closure, article classified as incident, severe alert.
- `state_only`: routine state, updates the state store, no event.
- `context`: schedule/permit data, kept and indexed, creates events only in phase `scheduled`.
- `noise`: kept in the ledger, never correlated.

Nothing is deleted. Unassigned observations stay queryable, so you can change the gate later and replay.

## 5. Time model (so old events never look current again)

Four timestamps on every observation, all required where knowable:

`event_time` (when it happened), `published_at`, `observed_at` (when we saw it), `ingested_at`.

Rules:

- **Never use `observed_at` or `ingested_at` as `event_time`.** If `event_time` can't be extracted, store `NULL` with `event_time_confidence = unknown`. Don't invent one.
- Event phase is computed from `event_time`/interval and **last meaningful evidence**, not from row existence.
- **Freshness TTL per kind:** an Incident with no new evidence for X hours moves to `historical`. A Condition stays `active` only while its source still lists it.
- **Ingest guard:** if an article's published date is older than a threshold (say 6h) at first sight, it can attach to an existing event but cannot *create* an active event. It creates a `historical` event instead.
- **Future-dated guard:** `event_time` in the future routes to `scheduled`.
- Add a test: replay yesterday's e-bike article today and assert the event is `historical`.

## 6. Classification pipeline (observation → event)

```
observation
  → normalize (type, location geometry, time, entities, claims)
  → significance gate
  → deterministic match (same source ID, permit ID, closure ID → same event)
  → candidate generation (spatial-temporal index lookup, not scan)
  → scoring
  → decision: MATCH | POSSIBLE | NEW | CONFLICT
```

**Candidate generation:** only compare against events within a spatial radius (or same road segment) and a time window per kind. Keeps cost flat as events grow.

**Score components:** spatial overlap (point/line/area aware, same road segment beats 300 m apart), temporal compatibility, compatible event types (collision ↔ road closure ↔ police response compatible; construction ↔ collision not), entity overlap (street names, agencies, vehicle types), semantic similarity (embedding, a supporting signal only), source relationship.

**Decision:**

- High score → `MATCH`, attach.
- Middle → `POSSIBLE`: linked but unresolved, shown as "possibly related", not merged.
- Low → `NEW` event (if the significance gate allows).
- Related but contradictory (road open vs closed) → `CONFLICT`, recorded as a contradiction, not blindly overwritten.

**Rule:** a false merge is worse than a duplicate. Bias toward `POSSIBLE`.

**LLM use in Stage 1:** only to extract type, entities, and claims from unstructured text (articles, social). Output must pass a schema check. It never decides matching alone.

**Event creation minimums:** one high-authority source (dispatch, agency alert) can create an event as `detected`. Weak sources (a single article, social) create `unconfirmed` events.

## 7. Situations: compound events and escalation

A Situation forms when child events link by shared place, time, and theme:

- Seed: a single `planned protest` article makes a Situation in phase `scheduled`.
- Later observations (crowd reports, road closure, police activity) match the Situation and spawn child Incidents/Conditions inside it.
- **Escalation signals** are stored on the Situation timeline: new agency involvement, geographic spread, new impact domains, rising evidence rate, severity keywords. Each timeline entry carries the evidence that justified it.
- Trajectory field: `building | steady | escalating | de-escalating | ended`, with the reasons listed.
- May Day case: 1 Situation, many child events, every source attached, instead of 50 unrelated events.

Promotion rule: when 3+ events (tunable) correlate on place + time + theme, propose a parent Situation. Mark it `possible` until a second independent family agrees.

## 8. Lifecycle

```
Incident:   detected → confirmed → active → resolved → historical
Condition:  scheduled → active → ended → historical
Situation:  scheduled/emerging → active → escalating/de-escalating → ended → historical
Any:        unconfirmed → (confirmed | disproven)
```

Every transition writes a timeline entry with who/what caused it. Events are never deleted. They can be **merged** (`merged_into`) or **split** (`split_from`), and old IDs keep resolving.

## 9. Evidence and confidence

Per event, store these separately (never a single count):

`raw_poll_count` (hidden), `observation_count`, `distinct_document_count`, `independent_source_count`, `source_family_count`, `duplicate_count`, `meaningful_update_count`.

**Source independence:** keep a republication graph (wire stories, syndication). AP → KING5 → KOMO counts as one family.

**Confidence is per claim, then rolled up:**

- Claim confidence = f(source authority, independent corroboration, extraction confidence, contradictions, recency).
- Event confidence = *how sure we are the event exists* (separate from detail confidence).
- Raw observation volume must never enter the formula. Volume was what drove the 0.01 on a three-source event.
- Show it as: "Collision: high. Fatality: high. Location: medium."

## 10. Data model

`source`, `source_policy`, `poll_run`, `raw_record` (immutable), `source_record` (id, fingerprint, first_seen, last_confirmed, ended_at), `observation` (version, four timestamps, claims, geometry), `claim`, `entity` + `entity_alias`, `event` (kind, phase, status, current version), `event_version` (snapshots), `event_link` (observation→event with match score and decision), `event_relation` (parent/child, caused, merged_into, split_from), `timeline_entry`, `contradiction`, `correlation_candidate` (the POSSIBLE queue), `source_health`.

Not one big `persistent_events` table.

## 11. Source health

Statuses: `HEALTHY`, `HEALTHY_NO_MATCHES`, `STALE`, `ERROR`, `NEVER_CONNECTED`, `DISABLED`, `EMPTY_UNEXPECTED`. The dashboard shows a coverage panel by domain ("transit: 0 of 3 sources producing data") so absence of events is never misread as a quiet world.

## 12. Stage 1 dashboard requirements

- Event list filtered by kind and phase, defaulting to `active`, with historical and scheduled behind tabs.
- Event page: timeline with evidence per entry, claims with per-claim confidence, evidence counts (the real ones), linked children/parent, contradictions, and "possibly related" candidates.
- Raw drill-down: all observations for an event, plus the unassigned/`state_only` stream filterable by source and type.
- Coverage panel (section 11).
- Review queue for `POSSIBLE` matches and `CONFLICT`s (also becomes your labeled data for tuning).

## 13. Interface to Stage 2

Stage 1 emits a **material change** record whenever an event version differs meaningfully:

`{event_id, version, changed_fields, change_flags, material: bool, reason}`

Flags: `created`, `phase_changed`, `location_moved`, `new_impact_domain`, `escalated`, `de_escalated`, `resolved`, `confidence_jump`, `contradiction`. Thresholds and hysteresis per field, plus a minimum interval per event, so Stage 2 isn't retriggered by flapping. Stage 2 only subscribes to this stream.

## 14. Robustness checklist

- Idempotent ingestion: reprocessing a raw record never creates duplicates.
- Replay: since raw and observations are immutable, rerun correlation from history when logic improves, and diff old vs new events.
- Every decision stores its inputs and scores (explainable).
- Config-driven thresholds per source and event kind.
- Metrics: observation→event assignment rate, POSSIBLE rate, merge/split corrections, events per day by kind, stale-active count, time-to-detect vs first source.
- Test fixtures from real cases: e-bike collision (multi-domain), road closure start/end, protest escalation, May Day, scheduled 2027 event, day-old article, feed outage.

## 15. Build order

1. **Ingestion cleanup:** source policies, fingerprints, `last_confirmed`, disappearance detection, source health statuses. *Done when:* repeated polling adds zero observations.
2. **Time model and phases:** four timestamps, scheduled/historical guards. *Done when:* the day-old article and 2027 market cases pass.
3. **Normalization, claims, significance gate.** *Done when:* every observation has a class, and `state_only` stops creating events.
4. **Event store and deterministic matching, Incident + Condition kinds.** *Done when:* road closures open and close with durations.
5. **Probabilistic correlation** with MATCH/POSSIBLE/CONFLICT and the review queue.
6. **Evidence counts and claim-level confidence.** *Done when:* the e-bike event shows honest counts and sane confidence.
7. **Situations and escalation timeline.** *Done when:* the protest and May Day fixtures produce one parent with children.
8. **Dashboard, coverage panel, and material-change stream** to hand off to Stage 2.

## 16. Decisions for you

- Disappearance threshold N per source (e.g. 3 missed polls for closures, longer for slow feeds).
- Incident freshness TTLs (suggest 6h for minor, 24h for fatal or major).
- Whether `POSSIBLE` matches show in the main list or only in the review queue.
- Minimum child events to propose a Situation.