"""PostGIS target schema (brief sections 28/42).

Not wired into the local run - it is the production reference the SQLite driver
mirrors. Applied with::

    psql -f schema.sql

Spatial indexing uses GiST; H3-style regional partitioning is represented by the
``h3_cell`` columns used for caching and regional aggregation.
"""

DDL = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;          -- pgvector for analogue retrieval

CREATE TABLE observations (
    observation_id       UUID PRIMARY KEY,
    schema_version       TEXT        NOT NULL,
    source_id            TEXT        NOT NULL,
    source_record_id     TEXT,

    event_time           TIMESTAMPTZ NOT NULL,
    observed_at          TIMESTAMPTZ NOT NULL,
    ingested_at          TIMESTAMPTZ NOT NULL,

    source_type          TEXT        NOT NULL,
    observation_type     TEXT        NOT NULL,
    authority            TEXT        NOT NULL,
    headline             TEXT        NOT NULL DEFAULT '',

    geometry             geometry(Geometry, 4326),
    h3_cell              TEXT,
    location_precision_m REAL,

    structured_payload   JSONB       NOT NULL,
    provenance           JSONB       NOT NULL,
    quality              JSONB       NOT NULL,
    source_url           TEXT,
    raw_payload_uri      TEXT,
    dedupe_key           TEXT        NOT NULL UNIQUE,
    payload_hash         TEXT        NOT NULL,
    payload_tsv          TSVECTOR
);

CREATE INDEX obs_time_idx     ON observations (observed_at DESC);
CREATE INDEX obs_source_idx   ON observations (source_id, observed_at DESC);
CREATE INDEX obs_type_idx     ON observations (observation_type, observed_at DESC);
CREATE INDEX obs_geom_idx     ON observations USING GIST (geometry);
CREATE INDEX obs_h3_idx       ON observations (h3_cell);
CREATE INDEX obs_payload_idx  ON observations USING GIN (payload_tsv);

CREATE TABLE events (
    event_id       UUID PRIMARY KEY,
    region_id      TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'candidate',
    geometry       geometry(Geometry, 4326),
    first_observed TIMESTAMPTZ,
    last_observed  TIMESTAMPTZ,
    closed_at      TIMESTAMPTZ,
    close_reason   TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX events_status_idx ON events (status, updated_at DESC);
CREATE INDEX events_geom_idx   ON events USING GIST (geometry);

CREATE TABLE event_observations (
    event_id       UUID NOT NULL REFERENCES events(event_id),
    observation_id UUID NOT NULL REFERENCES observations(observation_id),
    linked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, observation_id)
);
CREATE INDEX eo_obs_idx ON event_observations (observation_id);

-- Section 10: no merge is irreversible.
CREATE TABLE event_aliases (
    alias      TEXT NOT NULL,
    event_id   UUID NOT NULL REFERENCES events(event_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (alias, event_id)
);

CREATE TABLE event_merge_history (
    merge_id      UUID PRIMARY KEY,
    source_event  UUID NOT NULL,
    target_event  UUID NOT NULL,
    reason        TEXT NOT NULL,
    merged_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE event_split_history (
    split_id    UUID PRIMARY KEY,
    parent_event UUID NOT NULL,
    child_event  UUID NOT NULL,
    reason       TEXT NOT NULL,
    split_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE event_states (
    event_id      UUID NOT NULL REFERENCES events(event_id),
    state_version INTEGER NOT NULL,
    state         JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, state_version)
);

-- ``reconstructed_at`` is when the platform formed this belief, which is not
-- ``created_at``. They differ whenever the ledger is backfilled: a week-old
-- incident replayed today writes every version today, and section 49's
-- "what did we believe at time T" is unanswerable from write time alone.
CREATE INDEX event_states_reconstructed_idx
    ON event_states (reconstructed_at DESC);

-- Durable state deltas. Kept outside ``analysis_runs`` on purpose: a delta has
-- to exist the moment its state version does, in the same transaction, or it
-- does not exist at all for any event no analysis happened to run on. The
-- (region_id, is_material, recorded_at) index is what makes "what is changing
-- right now anywhere in this region" a bounded read rather than a table scan.
CREATE TABLE state_deltas (
    delta_id               UUID PRIMARY KEY,
    schema_version         TEXT NOT NULL DEFAULT '1.0.0',
    event_id               UUID NOT NULL REFERENCES events(event_id),
    region_id              TEXT NOT NULL DEFAULT '',
    state_version          INTEGER NOT NULL,
    previous_state_version INTEGER,
    change                 TEXT NOT NULL,
    domain                 TEXT NOT NULL DEFAULT 'event',
    before                 JSONB,
    after                  JSONB,
    magnitude              DOUBLE PRECISION NOT NULL DEFAULT 0,
    confidence             DOUBLE PRECISION NOT NULL DEFAULT 0,
    novelty                DOUBLE PRECISION NOT NULL DEFAULT 0,
    is_material            BOOLEAN NOT NULL DEFAULT FALSE,
    causes                 JSONB NOT NULL DEFAULT '[]'::jsonb,
    urgency                TEXT NOT NULL DEFAULT 'none',
    affected_user_count    INTEGER NOT NULL DEFAULT 0,
    official_guidance      BOOLEAN NOT NULL DEFAULT FALSE,
    recorded_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The delta is a fact about one version of one event. The unique constraint
    -- is what makes re-deriving a state version idempotent instead of
    -- duplicating every change it explains.
    UNIQUE (event_id, state_version, change)
);

CREATE INDEX state_deltas_event_idx
    ON state_deltas (event_id, state_version);
CREATE INDEX state_deltas_region_material_idx
    ON state_deltas (region_id, is_material, recorded_at DESC)
    WHERE is_material;

-- Lifecycle transitions: what status an event moved to, when, and on what
-- evidence. Closure inferred from silence and closure announced by an official
-- record are different facts with very different confidence, and only a log
-- records which one happened.
CREATE TABLE event_lifecycle (
    transition_id            UUID PRIMARY KEY,
    event_id                 UUID NOT NULL REFERENCES events(event_id),
    region_id                TEXT NOT NULL DEFAULT '',
    from_status              TEXT,
    to_status                TEXT NOT NULL,
    reason                   TEXT NOT NULL DEFAULT '',
    termination_basis        TEXT NOT NULL DEFAULT 'unknown',
    confidence               DOUBLE PRECISION NOT NULL DEFAULT 0,
    evidence_observation_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    silence_threshold_seconds DOUBLE PRECISION,
    observation_count        INTEGER NOT NULL DEFAULT 0,
    state_version            INTEGER,
    at                       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX lifecycle_event_idx ON event_lifecycle (event_id, at DESC);
CREATE INDEX lifecycle_basis_idx ON event_lifecycle (termination_basis, at DESC);

-- The cross-event world view. A projection, not a source of truth: every field
-- is derived from state versions that already exist, and ``observation_total``
-- is recorded so a stale snapshot is recognisable as stale rather than
-- authoritative.
CREATE TABLE world_snapshots (
    snapshot_id               UUID PRIMARY KEY,
    schema_version            TEXT NOT NULL DEFAULT '1.0.0',
    region_id                 TEXT NOT NULL,
    generated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    observation_total         INTEGER NOT NULL DEFAULT 0,
    events_total              INTEGER NOT NULL DEFAULT 0,
    events_active             INTEGER NOT NULL DEFAULT 0,
    events_quiescent          INTEGER NOT NULL DEFAULT 0,
    events_closed             INTEGER NOT NULL DEFAULT 0,
    events_changed_materially INTEGER NOT NULL DEFAULT 0,
    snapshot                  JSONB NOT NULL
);

CREATE INDEX world_snapshots_region_idx
    ON world_snapshots (region_id, generated_at DESC);

CREATE TABLE claims (
    claim_id              UUID PRIMARY KEY,
    observation_id        UUID NOT NULL REFERENCES observations(observation_id),
    event_id              UUID REFERENCES events(event_id),
    predicate             TEXT NOT NULL,
    value                 JSONB,
    geometry              geometry(Geometry, 4326),
    valid_from            TIMESTAMPTZ,
    valid_until           TIMESTAMPTZ,
    extraction_method     TEXT NOT NULL,
    extraction_confidence REAL NOT NULL,
    source_id             TEXT,
    truth_status          TEXT NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX claims_event_idx ON claims (event_id, predicate);
CREATE INDEX claims_pred_idx  ON claims (predicate, valid_from DESC);
CREATE INDEX claims_geom_idx  ON claims USING GIST (geometry);

CREATE TABLE analysis_runs (
    analysis_run_id         UUID PRIMARY KEY,
    event_id                UUID NOT NULL,
    region_id               TEXT NOT NULL,
    trigger                 TEXT NOT NULL,
    previous_state_version  INTEGER,
    new_state_version       INTEGER NOT NULL,
    started_at              TIMESTAMPTZ NOT NULL,
    completed_at            TIMESTAMPTZ NOT NULL,
    run                     JSONB NOT NULL
);
CREATE INDEX runs_event_idx ON analysis_runs (event_id, completed_at DESC);

-- Section 44/45: predictions joined to ground truth, never just the displayed value.
CREATE TABLE model_outputs (
    model_output_id    UUID PRIMARY KEY,
    event_id           UUID NOT NULL,
    analysis_run_id    UUID,
    model_id           TEXT NOT NULL,
    model_version      TEXT NOT NULL,
    prediction_time    TIMESTAMPTZ NOT NULL,
    forecast_horizon   INTERVAL,
    features_version   TEXT NOT NULL,
    input_state_version INTEGER,
    output             JSONB NOT NULL,
    probability        REAL,
    uncertainty        REAL,
    calibration_version TEXT
);
CREATE INDEX model_outputs_event_idx ON model_outputs (event_id, prediction_time DESC);

CREATE TABLE prediction_outcomes (
    model_output_id UUID PRIMARY KEY REFERENCES model_outputs(model_output_id),
    ground_truth_at TIMESTAMPTZ,
    outcome         TEXT,
    resolved        BOOLEAN NOT NULL DEFAULT FALSE,
    metrics         JSONB
);

CREATE TABLE users (
    user_id UUID PRIMARY KEY,
    saved_places JSONB NOT NULL DEFAULT '[]',
    route_profiles JSONB NOT NULL DEFAULT '[]',
    transport_modes JSONB NOT NULL DEFAULT '[]',
    preferences JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Section 59: ephemeral by default, short retention, audited access.
CREATE TABLE user_locations (
    user_id    UUID NOT NULL REFERENCES users(user_id),
    geometry   geometry(Geometry, 4326) NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX user_locations_idx ON user_locations USING GIST (geometry);

CREATE TABLE user_impacts (
    analysis_run_id UUID PRIMARY KEY,
    user_id    UUID NOT NULL,
    event_id   UUID NOT NULL,
    exposure_level TEXT NOT NULL,
    priority   REAL NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    exposure   JSONB NOT NULL
);
CREATE INDEX uimp_user_idx  ON user_impacts (user_id, computed_at DESC);
CREATE INDEX uimp_event_idx ON user_impacts (event_id, computed_at DESC);

CREATE TABLE notifications (
    notification_id UUID PRIMARY KEY,
    user_id   UUID NOT NULL,
    event_id  UUID NOT NULL,
    reason    TEXT NOT NULL,
    urgency   TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    status    TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at   TIMESTAMPTZ,
    payload   JSONB NOT NULL
);

CREATE TABLE source_health (
    source_id  TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    health     JSONB NOT NULL
);

CREATE TABLE infrastructure_nodes (
    node_id    TEXT PRIMARY KEY,
    node_class TEXT NOT NULL,
    name       TEXT,
    geometry   geometry(Geometry, 4326),
    attributes JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX infra_nodes_geom_idx  ON infrastructure_nodes USING GIST (geometry);
CREATE INDEX infra_nodes_class_idx ON infrastructure_nodes (node_class);

CREATE TABLE infrastructure_edges (
    edge_id        TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    source_node_id TEXT NOT NULL,
    target_node_id TEXT NOT NULL,
    weight         REAL NOT NULL DEFAULT 1.0,
    attributes     JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX edges_src_idx ON infrastructure_edges (source_node_id);
CREATE INDEX edges_tgt_idx ON infrastructure_edges (target_node_id);

-- Section 31: point-in-time historical state embeddings.
CREATE TABLE event_state_embeddings (
    event_id      UUID NOT NULL,
    state_version INTEGER NOT NULL,
    embedding     vector(384),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, state_version)
);
CREATE INDEX state_embeddings_idx ON event_state_embeddings USING ivfflat (embedding vector_l2_ops) WITH (lists = 100);

CREATE TABLE consumers (
    consumer TEXT PRIMARY KEY,
    offset   TEXT
);
"""