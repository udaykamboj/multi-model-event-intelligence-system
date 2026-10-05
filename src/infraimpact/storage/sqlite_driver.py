"""SQLite driver (local development).

Geometry is stored as GeoJSON text in a ``geometry`` column with min/max
longitude and latitude columns for indexed spatial screening. PostGIS uses a
real ``geometry(Geometry, 4326)`` column plus GiST - see
``infraimpact/storage/postgres/schema.sql``. Both satisfy the same repository
contract, and the geo column set here is what promotes cleanly to
``ST_DWithin``/GiST in production.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..domain.enums import EventKind, EventPhase, MatchDecision
from ..domain.geo import bbox_of, centroid_of
from ..domain.ids import ensure_utc, new_id
from ..domain.schemas import (
    AnalysisRun,
    Claim,
    Contradiction,
    CorrelationCandidate,
    EventLifecycleTransition,
    EventRelation,
    EventState,
    MaterialChangeRecord,
    NotificationCandidate,
    Observation,
    RouteProfile,
    SavedPlace,
    SourceHealth,
    SourceRecord,
    StateDeltaRecord,
    TimelineEntry,
    UserContext,
    WorldSnapshot,
)
from .repository import (
    AnalysisRunRepository,
    ClaimRepository,
    ContradictionRepository,
    CorrelationCandidateRepository,
    EventLifecycleRepository,
    EventRepository,
    MaterialChangeRepository,
    NotificationRepository,
    ObservationRepository,
    PlatformRepository,
    SourceHealthRepository,
    SourceRecordRepository,
    StateDeltaRepository,
    StateRepository,
    UserImpactRepository,
    UserRepository,
    WorldSnapshotRepository,
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS observations (
    observation_id       TEXT PRIMARY KEY,
    schema_version       TEXT NOT NULL,
    source_id            TEXT NOT NULL,
    source_record_id     TEXT,
    event_time           TEXT,
    event_time_confidence TEXT NOT NULL DEFAULT 'known',
    published_at         TEXT,
    observed_at          TEXT NOT NULL,
    ingested_at          TEXT NOT NULL,
    source_type          TEXT NOT NULL,
    observation_type     TEXT NOT NULL,
    significance_class   TEXT NOT NULL DEFAULT 'event_candidate',
    version              INTEGER NOT NULL DEFAULT 1,
    authority            TEXT NOT NULL,
    headline             TEXT NOT NULL DEFAULT '',
    geometry             TEXT,
    min_lon              REAL,
    min_lat              REAL,
    max_lon              REAL,
    max_lat              REAL,
    centroid_lon         REAL,
    centroid_lat         REAL,
    location_precision_m REAL,
    structured_payload   TEXT NOT NULL,
    provenance           TEXT NOT NULL,
    quality              TEXT NOT NULL,
    source_url           TEXT,
    raw_payload_uri      TEXT,
    dedupe_key           TEXT NOT NULL UNIQUE,
    payload_hash         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_time ON observations(observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_event_time ON observations(event_time);
CREATE INDEX IF NOT EXISTS idx_obs_source ON observations(source_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_type ON observations(observation_type, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_geo ON observations(min_lon, max_lon, min_lat, max_lat);

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    region_id       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'candidate',
    kind            TEXT NOT NULL DEFAULT 'incident',
    phase           TEXT NOT NULL DEFAULT 'active',
    parent_event_id TEXT,
    first_observed  TEXT,
    last_observed   TEXT,
    closed_at       TEXT,
    close_reason    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS source_records (
    source_id        TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    fingerprint      TEXT NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1,
    first_seen       TEXT NOT NULL,
    last_confirmed   TEXT NOT NULL,
    missed_polls     INTEGER NOT NULL DEFAULT 0,
    is_active        INTEGER NOT NULL DEFAULT 1,
    ended_at         TEXT,
    PRIMARY KEY (source_id, source_record_id)
);
CREATE INDEX IF NOT EXISTS idx_sr_source ON source_records(source_id, is_active);

CREATE TABLE IF NOT EXISTS correlation_candidates (
    candidate_id     TEXT PRIMARY KEY,
    observation_id   TEXT NOT NULL,
    event_id         TEXT NOT NULL,
    score            REAL NOT NULL,
    reasons          TEXT NOT NULL DEFAULT '[]',
    decision         TEXT NOT NULL DEFAULT 'possible',
    created_at       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON correlation_candidates(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_candidates_event ON correlation_candidates(event_id);

CREATE TABLE IF NOT EXISTS contradictions (
    contradiction_id TEXT PRIMARY KEY,
    event_id         TEXT NOT NULL,
    predicate        TEXT NOT NULL,
    claim_id_a       TEXT NOT NULL,
    claim_id_b       TEXT NOT NULL,
    value_a          TEXT,
    value_b          TEXT,
    detected_at      TEXT NOT NULL,
    resolved         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_contradictions_event ON contradictions(event_id, resolved);

CREATE TABLE IF NOT EXISTS event_relations (
    parent_event_id  TEXT NOT NULL,
    child_event_id   TEXT NOT NULL,
    relation_type    TEXT NOT NULL DEFAULT 'parent_child',
    linked_at        TEXT NOT NULL,
    PRIMARY KEY (parent_event_id, child_event_id, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_rel_parent ON event_relations(parent_event_id);
CREATE INDEX IF NOT EXISTS idx_rel_child ON event_relations(child_event_id);

CREATE TABLE IF NOT EXISTS timeline_entries (
    entry_id                  TEXT PRIMARY KEY,
    event_id                  TEXT NOT NULL,
    timestamp                 TEXT NOT NULL,
    event_kind                TEXT NOT NULL DEFAULT 'incident',
    phase                     TEXT NOT NULL DEFAULT 'active',
    headline                  TEXT NOT NULL,
    detail                    TEXT NOT NULL DEFAULT '',
    evidence_observation_ids  TEXT NOT NULL DEFAULT '[]',
    causal_factor             TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tle_event ON timeline_entries(event_id, timestamp ASC);

CREATE TABLE IF NOT EXISTS material_changes (
    change_id        TEXT PRIMARY KEY,
    event_id         TEXT NOT NULL,
    version          INTEGER NOT NULL,
    changed_fields   TEXT NOT NULL DEFAULT '[]',
    change_flags     TEXT NOT NULL DEFAULT '[]',
    is_material      INTEGER NOT NULL DEFAULT 0,
    reason           TEXT NOT NULL DEFAULT '',
    recorded_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mat_changes ON material_changes(event_id, version);
CREATE INDEX IF NOT EXISTS idx_mat_feed ON material_changes(is_material, recorded_at DESC);


CREATE TABLE IF NOT EXISTS event_observations (
    event_id       TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    linked_at      TEXT NOT NULL,
    PRIMARY KEY (event_id, observation_id)
);
CREATE INDEX IF NOT EXISTS idx_eo_obs ON event_observations(observation_id);

CREATE TABLE IF NOT EXISTS event_aliases (
    alias      TEXT NOT NULL,
    event_id   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (alias, event_id)
);

CREATE TABLE IF NOT EXISTS event_merges (
    merge_id    TEXT PRIMARY KEY,
    source_event TEXT NOT NULL,
    target_event TEXT NOT NULL,
    reason      TEXT NOT NULL,
    merged_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_states (
    event_id         TEXT NOT NULL,
    state_version    INTEGER NOT NULL,
    state            TEXT NOT NULL,
    reconstructed_at TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (event_id, state_version)
);

-- Section 32/54: "what changed?" is first-class, so it gets its own table rather
-- than living only inside an opaque analysis_runs JSON blob. The world-state
-- engine writes these in the same transaction as the state version they describe,
-- which is what makes the delta survive independently of whether any analysis
-- subsequently ran. before/after hold arbitrary JSON because a delta is a
-- comparison of whatever the previous state believed against whatever the new
-- one does, and that differs per change kind.
CREATE TABLE IF NOT EXISTS state_deltas (
    delta_id                 TEXT PRIMARY KEY,
    schema_version           TEXT NOT NULL DEFAULT '1.0.0',
    event_id                 TEXT NOT NULL,
    region_id                TEXT NOT NULL DEFAULT '',
    state_version            INTEGER NOT NULL,
    previous_state_version   INTEGER,
    change                   TEXT NOT NULL,
    domain                   TEXT NOT NULL DEFAULT 'event',
    before                   TEXT,
    after                    TEXT,
    magnitude                REAL NOT NULL DEFAULT 0,
    confidence               REAL NOT NULL DEFAULT 0,
    novelty                  REAL NOT NULL DEFAULT 0,
    is_material              INTEGER NOT NULL DEFAULT 0,
    causes                   TEXT NOT NULL DEFAULT '[]',
    urgency                  TEXT NOT NULL DEFAULT 'none',
    affected_user_count      INTEGER NOT NULL DEFAULT 0,
    official_guidance        INTEGER NOT NULL DEFAULT 0,
    recorded_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deltas_event ON state_deltas(event_id, state_version);
-- The region-wide change feed: material changes newest-first, filterable by event.
CREATE INDEX IF NOT EXISTS idx_deltas_material ON state_deltas(is_material, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_deltas_region ON state_deltas(region_id, recorded_at DESC);

-- Lifecycle history. The events.status column holds the current conclusion and is
-- overwritten in place; this holds how the platform reached it and on what
-- evidence, which is the part that has to survive for an audit of a closure.
CREATE TABLE IF NOT EXISTS event_lifecycle (
    transition_id             TEXT PRIMARY KEY,
    event_id                  TEXT NOT NULL,
    region_id                 TEXT NOT NULL DEFAULT '',
    from_status               TEXT,
    to_status                 TEXT NOT NULL,
    reason                    TEXT NOT NULL DEFAULT '',
    termination_basis         TEXT NOT NULL DEFAULT 'unknown',
    confidence                REAL NOT NULL DEFAULT 0,
    evidence_observation_ids  TEXT NOT NULL DEFAULT '[]',
    silence_threshold_seconds REAL,
    observation_count         INTEGER NOT NULL DEFAULT 0,
    state_version             INTEGER,
    at                        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_event ON event_lifecycle(event_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_lifecycle_region ON event_lifecycle(region_id, at DESC);

-- Cross-event world projection: the platform's current answer to "what is
-- happening right now". A projection, not a source of truth - it can always be
-- rebuilt from event_states, and observation_total records what it was built
-- from so a stale snapshot is recognisable as stale rather than served as current.
CREATE TABLE IF NOT EXISTS world_snapshots (
    snapshot_id               TEXT PRIMARY KEY,
    schema_version            TEXT NOT NULL DEFAULT '1.0.0',
    region_id                 TEXT NOT NULL,
    generated_at              TEXT NOT NULL,
    observation_total         INTEGER NOT NULL DEFAULT 0,
    events_total              INTEGER NOT NULL DEFAULT 0,
    events_active             INTEGER NOT NULL DEFAULT 0,
    events_quiescent          INTEGER NOT NULL DEFAULT 0,
    events_closed             INTEGER NOT NULL DEFAULT 0,
    events_changed_materially INTEGER NOT NULL DEFAULT 0,
    snapshot                  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_world_region ON world_snapshots(region_id, generated_at DESC);

CREATE TABLE IF NOT EXISTS claims (
    claim_id              TEXT PRIMARY KEY,
    observation_id        TEXT NOT NULL,
    event_id              TEXT,
    predicate             TEXT NOT NULL,
    value                 TEXT,
    geometry              TEXT,
    valid_from            TEXT,
    valid_until           TEXT,
    extraction_method     TEXT NOT NULL,
    extraction_confidence REAL NOT NULL,
    source_id             TEXT,
    truth_status          TEXT NOT NULL,
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_event ON claims(event_id, predicate);
CREATE INDEX IF NOT EXISTS idx_claims_pred ON claims(predicate, valid_from DESC);

CREATE TABLE IF NOT EXISTS analysis_runs (
    analysis_run_id  TEXT PRIMARY KEY,
    event_id         TEXT NOT NULL,
    region_id        TEXT NOT NULL,
    trigger          TEXT NOT NULL,
    previous_state_version INTEGER,
    new_state_version INTEGER NOT NULL,
    started_at       TEXT NOT NULL,
    completed_at     TEXT NOT NULL,
    run              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_event ON analysis_runs(event_id, completed_at DESC);

CREATE TABLE IF NOT EXISTS user_impacts (
    analysis_run_id  TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    event_id         TEXT NOT NULL,
    exposure_level   TEXT NOT NULL,
    priority         REAL NOT NULL,
    computed_at      TEXT NOT NULL,
    exposure         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uimp_user ON user_impacts(user_id, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_uimp_event ON user_impacts(event_id, computed_at DESC);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    reason          TEXT NOT NULL,
    urgency         TEXT NOT NULL,
    dedupe_key      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    sent_at         TEXT,
    payload         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notif_dedupe ON notifications(dedupe_key);

CREATE TABLE IF NOT EXISTS source_health (
    source_id     TEXT PRIMARY KEY,
    state         TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    health        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consumers (
    consumer TEXT PRIMARY KEY,
    offset   TEXT
);

CREATE TABLE IF NOT EXISTS infrastructure_nodes (
    node_id     TEXT PRIMARY KEY,
    node_class  TEXT NOT NULL,
    name        TEXT,
    geometry    TEXT,
    attributes  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_class ON infrastructure_nodes(node_class);

CREATE TABLE IF NOT EXISTS infrastructure_edges (
    edge_id     TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    source_node_id TEXT NOT NULL,
    target_node_id TEXT NOT NULL,
    weight      REAL NOT NULL DEFAULT 1.0,
    attributes  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON infrastructure_edges(source_node_id);
CREATE INDEX IF NOT EXISTS idx_edges_tgt ON infrastructure_edges(target_node_id);

CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    saved_places TEXT NOT NULL,
    route_profiles TEXT NOT NULL,
    transport_modes TEXT NOT NULL,
    preferences TEXT NOT NULL,
    current_location TEXT,
    current_location_expires_at TEXT,
    active_destination_id TEXT,
    active_route_id TEXT,
    updated_at  TEXT NOT NULL
);
"""


def _dump(model: Any) -> str:
    """Serialize a model, a sequence of models, or plain JSON-ish data.

    Callers hand this either a single Pydantic model or an already-extracted
    list of dumps (``[p.model_dump(...) for p in ...]``), so both shapes have
    to work.
    """
    if hasattr(model, "model_dump"):
        model = model.model_dump(mode="json")
    return json.dumps(model, default=str)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Add a column to a pre-existing dev database.

    ``CREATE TABLE IF NOT EXISTS`` silently skips tables that already exist, so
    a schema that gains a column would otherwise break every developer who ran
    the app before the change. SQLite has no ``ADD COLUMN IF NOT EXISTS``, so
    check first. Dev-only concern: production migrates with real DDL.
    """
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _delta_from_row(row: dict[str, Any]) -> StateDeltaRecord:
    """Rebuild a delta record from its row.

    ``before``/``after`` are stored as JSON text because a delta compares
    whatever two state versions happened to believe, and that shape differs per
    change kind - a geometry change carries a centroid and an area, a severity
    change carries two enum values, an impact-appeared change carries a list of
    identifiers. One JSON column each beats a fixed schema that fits none of
    them, and the reconstruction is lossless because the values went in as JSON.
    """

    row = dict(row)
    return StateDeltaRecord(
        delta_id=row["delta_id"],
        schema_version=row.get("schema_version") or "1.0.0",
        event_id=row["event_id"],
        region_id=row.get("region_id") or "",
        state_version=int(row["state_version"]),
        previous_state_version=(
            int(row["previous_state_version"])
            if row.get("previous_state_version") is not None
            else None
        ),
        change=row["change"],
        domain=row.get("domain") or "event",
        before=_json_or_none(row.get("before")),
        after=_json_or_none(row.get("after")),
        magnitude=float(row.get("magnitude") or 0.0),
        confidence=float(row.get("confidence") or 0.0),
        novelty=float(row.get("novelty") or 0.0),
        is_material=bool(row.get("is_material")),
        causes=tuple(json.loads(row.get("causes") or "[]")),
        urgency=row.get("urgency") or "none",
        affected_user_count=int(row.get("affected_user_count") or 0),
        official_guidance=bool(row.get("official_guidance")),
        recorded_at=ensure_utc(
            datetime.fromisoformat(row["recorded_at"])
            if not isinstance(row["recorded_at"], datetime)
            else row["recorded_at"]
        ),
    )


def _json_or_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        text = value.decode() if isinstance(value, bytes) else value
        if not text or text == "null":
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return value


def _transition_from_row(row: dict[str, Any]) -> EventLifecycleTransition:
    row = dict(row)
    at = row["at"]
    return EventLifecycleTransition(
        transition_id=row["transition_id"],
        event_id=row["event_id"],
        region_id=row.get("region_id") or "",
        from_status=row.get("from_status"),
        to_status=row["to_status"],
        reason=row.get("reason") or "",
        termination_basis=row.get("termination_basis") or "unknown",
        confidence=float(row.get("confidence") or 0.0),
        evidence_observation_ids=tuple(json.loads(row.get("evidence_observation_ids") or "[]")),
        silence_threshold_seconds=row.get("silence_threshold_seconds"),
        observation_count=int(row.get("observation_count") or 0),
        state_version=(
            int(row["state_version"]) if row.get("state_version") is not None else None
        ),
        at=at if isinstance(at, datetime) else datetime.fromisoformat(at),
    )



class _Transaction:
    """Nesting depth of explicit batches on one connection.

    Deliberately an explicit counter rather than ``sqlite3.Connection
    .in_transaction``. That attribute reports whether *any* transaction is open,
    which in Python's sqlite3 includes the implicit one a bare DML statement
    opens - so keying commit behaviour off it would silently leave the very
    statements it was meant to auto-commit uncommitted forever.
    """

    __slots__ = ("depth",)

    def __init__(self) -> None:
        self.depth = 0


class _SqliteRepo:
    """Shared connection with a re-entrant lock."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: threading.RLock,
        tx: _Transaction | None = None,
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._tx = tx if tx is not None else _Transaction()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            # Commit per statement is the default so that a repository used on
            # its own is durable. Inside an explicit batch the caller has
            # promised atomicity and owns the commit.
            if self._tx.depth == 0:
                self._conn.commit()
            return cur

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def _many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)
            if self._tx.depth == 0:
                self._conn.commit()


class SqliteObservationRepository(_SqliteRepo, ObservationRepository):
    def append(self, observation: Observation) -> bool:
        bounds = bbox_of(observation.geometry)
        centre = centroid_of(observation.geometry)
        event_time_str = observation.event_time.isoformat() if observation.event_time else None
        published_at_str = observation.published_at.isoformat() if observation.published_at else None
        try:
            self._execute(
                """INSERT INTO observations (
                    observation_id, schema_version, source_id, source_record_id,
                    event_time, event_time_confidence, published_at, observed_at, ingested_at,
                    source_type, observation_type, significance_class, version,
                    authority, headline, geometry, min_lon, min_lat, max_lon, max_lat,
                    centroid_lon, centroid_lat, location_precision_m, structured_payload,
                    provenance, quality, source_url, raw_payload_uri, dedupe_key, payload_hash
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    observation.observation_id,
                    observation.schema_version,
                    observation.source_id,
                    observation.source_record_id,
                    event_time_str,
                    observation.event_time_confidence,
                    published_at_str,
                    observation.observed_at.isoformat(),
                    observation.ingested_at.isoformat(),
                    str(observation.source_type),
                    str(observation.observation_type),
                    str(observation.significance_class),
                    observation.version,
                    str(observation.provenance.authority),
                    observation.headline,
                    json.dumps(observation.geometry) if observation.geometry else None,
                    bounds[0] if bounds else None,
                    bounds[1] if bounds else None,
                    bounds[2] if bounds else None,
                    bounds[3] if bounds else None,
                    centre[0] if centre else None,
                    centre[1] if centre else None,
                    observation.location_precision_m,
                    json.dumps(observation.structured_payload, default=str),
                    _dump(observation.provenance),
                    _dump(observation.quality),
                    observation.source_url,
                    observation.raw_payload_uri,
                    observation.dedupe_key(),
                    observation.provenance.content_hash,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def get(self, observation_id: str) -> Observation | None:
        rows = self._query("SELECT * FROM observations WHERE observation_id=?", (observation_id,))
        return _row_to_observation(rows[0]) if rows else None

    def has_dedupe_key(self, dedupe_key: str) -> bool:
        rows = self._query("SELECT 1 FROM observations WHERE dedupe_key=?", (dedupe_key,))
        return bool(rows)

    def list_for_event(self, event_id: str) -> list[Observation]:
        rows = self._query(
            """SELECT o.* FROM observations o
               JOIN event_observations eo ON eo.observation_id = o.observation_id
               WHERE eo.event_id = ? ORDER BY o.observed_at ASC""",
            (event_id,),
        )
        return [_row_to_observation(r) for r in rows]

    def event_ids_between(self, start: datetime, end: datetime) -> list[str]:
        rows = self._query(
            """SELECT DISTINCT eo.event_id FROM event_observations eo
               JOIN observations o ON o.observation_id = eo.observation_id
               WHERE o.event_time >= ? AND o.event_time <= ?
               ORDER BY eo.event_id""",
            (ensure_utc(start).isoformat(), ensure_utc(end).isoformat()),
        )
        return [r["event_id"] for r in rows]

    def find_event_for_source_record(self, source_id: str, source_record_id: str) -> str | None:
        if not source_record_id:
            return None
        rows = self._query(
            """SELECT eo.event_id FROM observations o
               JOIN event_observations eo ON eo.observation_id = o.observation_id
               WHERE o.source_id = ? AND o.source_record_id = ?
               ORDER BY o.observed_at DESC LIMIT 1""",
            (source_id, source_record_id),
        )
        return rows[0]["event_id"] if rows else None

    def list_between(
        self, start: datetime, end: datetime, region_id: str | None = None
    ) -> list[Observation]:
        rows = self._query(
            """SELECT * FROM observations
               WHERE observed_at >= ? AND observed_at <= ?
               ORDER BY observed_at ASC""",
            (ensure_utc(start).isoformat(), ensure_utc(end).isoformat()),
        )
        return [_row_to_observation(r) for r in rows]

    def recent(self, limit: int = 100) -> list[Observation]:
        rows = self._query(
            """SELECT o.*, eo.event_id FROM observations o
               LEFT JOIN event_observations eo ON eo.observation_id = o.observation_id
               ORDER BY o.observed_at DESC LIMIT ?""",
            (limit,),
        )
        return [_row_to_observation(r) for r in rows]


    def count(self) -> int:
        rows = self._query("SELECT COUNT(*) AS c FROM observations")
        return int(rows[0]["c"]) if rows else 0


class SqliteEventRepository(_SqliteRepo, EventRepository):
    def ensure(
        self,
        event_id: str,
        first_observed: datetime,
        region_id: str,
        kind: EventKind = EventKind.INCIDENT,
        phase: EventPhase = EventPhase.ACTIVE,
        parent_event_id: str | None = None,
    ) -> None:
        self._execute(
            """INSERT INTO events (event_id, region_id, status, kind, phase, parent_event_id, first_observed, created_at, updated_at)
               VALUES (?,?,'candidate',?,?,?,?,?,?)
               ON CONFLICT(event_id) DO UPDATE SET last_observed=excluded.last_observed,
                                                   updated_at=excluded.updated_at""",
            (
                event_id,
                region_id,
                str(kind),
                str(phase),
                parent_event_id,
                first_observed.isoformat(),
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )

    def create(
        self,
        event_id: str,
        first_observed: datetime,
        region_id: str,
        kind: EventKind = EventKind.INCIDENT,
        phase: EventPhase = EventPhase.ACTIVE,
        parent_event_id: str | None = None,
    ) -> None:
        self.ensure(event_id, first_observed, region_id, kind=kind, phase=phase, parent_event_id=parent_event_id)

    def set_kind(self, event_id: str, kind: EventKind) -> None:
        self._execute(
            "UPDATE events SET kind=?, updated_at=? WHERE event_id=?",
            (str(kind), datetime.now(UTC).isoformat(), event_id),
        )

    def kind_of(self, event_id: str) -> EventKind:
        rows = self._query("SELECT kind FROM events WHERE event_id=?", (event_id,))
        if rows and rows[0]["kind"]:
            try:
                return EventKind(rows[0]["kind"])
            except ValueError:
                pass
        return EventKind.INCIDENT

    def set_phase(self, event_id: str, phase: EventPhase) -> None:
        self._execute(
            "UPDATE events SET phase=?, updated_at=? WHERE event_id=?",
            (str(phase), datetime.now(UTC).isoformat(), event_id),
        )

    def phase_of(self, event_id: str) -> EventPhase:
        rows = self._query("SELECT phase FROM events WHERE event_id=?", (event_id,))
        if rows and rows[0]["phase"]:
            try:
                return EventPhase(rows[0]["phase"])
            except ValueError:
                pass
        return EventPhase.ACTIVE

    def add_relation(self, relation: EventRelation) -> None:
        self._execute(
            """INSERT INTO event_relations (parent_event_id, child_event_id, relation_type, linked_at)
               VALUES (?,?,?,?) ON CONFLICT DO NOTHING""",
            (
                relation.parent_event_id,
                relation.child_event_id,
                relation.relation_type,
                ensure_utc(relation.linked_at).isoformat(),
            ),
        )

    def relations_for(self, event_id: str) -> list[EventRelation]:
        rows = self._query(
            "SELECT * FROM event_relations WHERE parent_event_id=? OR child_event_id=?",
            (event_id, event_id),
        )
        return [
            EventRelation(
                parent_event_id=r["parent_event_id"],
                child_event_id=r["child_event_id"],
                relation_type=r["relation_type"],
                linked_at=datetime.fromisoformat(r["linked_at"]),
            )
            for r in rows
        ]

    def add_timeline_entry(self, entry: TimelineEntry) -> None:
        self._execute(
            """INSERT INTO timeline_entries (
                entry_id, event_id, timestamp, event_kind, phase, headline, detail,
                evidence_observation_ids, causal_factor
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(entry_id) DO NOTHING""",
            (
                entry.entry_id,
                entry.event_id,
                ensure_utc(entry.timestamp).isoformat(),
                str(entry.event_kind),
                str(entry.phase),
                entry.headline,
                entry.detail,
                json.dumps(list(entry.evidence_observation_ids)),
                entry.causal_factor,
            ),
        )

    def timeline_for(self, event_id: str) -> list[TimelineEntry]:
        rows = self._query(
            "SELECT * FROM timeline_entries WHERE event_id=? ORDER BY timestamp ASC",
            (event_id,),
        )
        return [
            TimelineEntry(
                entry_id=r["entry_id"],
                event_id=r["event_id"],
                timestamp=datetime.fromisoformat(r["timestamp"]),
                event_kind=r["event_kind"],
                phase=r["phase"],
                headline=r["headline"],
                detail=r["detail"] or "",
                evidence_observation_ids=tuple(json.loads(r["evidence_observation_ids"] or "[]")),
                causal_factor=r["causal_factor"] or "",
            )
            for r in rows
        ]

    def link_observation(self, event_id: str, observation_id: str) -> None:
        self._execute(
            """INSERT INTO event_observations (event_id, observation_id, linked_at)
               VALUES (?,?,?) ON CONFLICT DO NOTHING""",
            (event_id, observation_id, datetime.now(UTC).isoformat()),
        )

    def observations_of(self, event_id: str) -> list[str]:
        rows = self._query(
            "SELECT observation_id FROM event_observations WHERE event_id=?", (event_id,)
        )
        return [r["observation_id"] for r in rows]

    def event_for_observation(self, observation_id: str) -> str | None:
        rows = self._query(
            "SELECT event_id FROM event_observations WHERE observation_id=? ORDER BY linked_at ASC LIMIT 1",
            (observation_id,),
        )
        return rows[0]["event_id"] if rows else None

    def active_events(self) -> list[str]:
        rows = self._query("SELECT event_id FROM events WHERE status != 'closed'")
        return [r["event_id"] for r in rows]

    def all_events(self) -> list[dict[str, Any]]:
        rows = self._query(
            """SELECT e.*, (SELECT COUNT(*) FROM event_observations eo WHERE eo.event_id=e.event_id) AS observation_count
               FROM events e ORDER BY e.updated_at DESC"""
        )
        return [dict(r) for r in rows]

    def status_of(self, event_id: str) -> str | None:
        rows = self._query("SELECT status FROM events WHERE event_id=?", (event_id,))
        return rows[0]["status"] if rows else None

    def set_status(self, event_id: str, status: str, at: datetime, reason: str) -> bool:
        cursor = self._execute(
            """UPDATE events
               SET status=?,
                   updated_at=?,
                   closed_at=CASE WHEN ?='closed' THEN ? ELSE NULL END,
                   close_reason=CASE WHEN ?='closed' THEN ? ELSE NULL END
               WHERE event_id=?""",
            (
                status,
                ensure_utc(at).isoformat(),
                status,
                ensure_utc(at).isoformat(),
                status,
                reason[:500],
                event_id,
            ),
        )
        return bool(cursor.rowcount)

    def close(self, event_id: str, at: datetime, reason: str) -> None:
        self._execute(
            "UPDATE events SET status='closed', closed_at=?, close_reason=?, updated_at=? WHERE event_id=?",
            (at.isoformat(), reason, datetime.now(UTC).isoformat(), event_id),
        )

    def record_alias(self, alias: str, event_id: str) -> None:
        self._execute(
            "INSERT INTO event_aliases (alias, event_id, created_at) VALUES (?,?,?) ON CONFLICT DO NOTHING",
            (alias, event_id, datetime.now(UTC).isoformat()),
        )

    def merge(self, source_event_id: str, target_event_id: str, reason: str) -> None:
        from ..domain.ids import new_id

        self._execute(
            """UPDATE OR REPLACE event_observations SET event_id=?
               WHERE event_id=? AND observation_id NOT IN (SELECT observation_id FROM event_observations WHERE event_id=?)""",
            (target_event_id, source_event_id, target_event_id),
        )
        self._execute(
            "UPDATE claims SET event_id=? WHERE event_id=?", (target_event_id, source_event_id)
        )
        self._execute(
            "UPDATE events SET status='closed', close_reason=?, updated_at=? WHERE event_id=?",
            (f"merged_into:{target_event_id}", datetime.now(UTC).isoformat(), source_event_id),
        )
        self._execute(
            """INSERT INTO event_merges (merge_id, source_event, target_event, reason, merged_at)
               VALUES (?,?,?,?,?)""",
            (new_id("merge"), source_event_id, target_event_id, reason, datetime.now(UTC).isoformat()),
        )


class SqliteSourceRecordRepository(_SqliteRepo, SourceRecordRepository):
    def upsert(self, record: SourceRecord) -> None:
        self._execute(
            """INSERT INTO source_records (
                source_id, source_record_id, fingerprint, version, first_seen,
                last_confirmed, missed_polls, is_active, ended_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id, source_record_id) DO UPDATE SET
                fingerprint=excluded.fingerprint,
                version=excluded.version,
                last_confirmed=excluded.last_confirmed,
                missed_polls=excluded.missed_polls,
                is_active=excluded.is_active,
                ended_at=excluded.ended_at""",
            (
                record.source_id,
                record.source_record_id,
                record.fingerprint,
                record.version,
                ensure_utc(record.first_seen).isoformat(),
                ensure_utc(record.last_confirmed).isoformat(),
                record.missed_polls,
                1 if record.is_active else 0,
                ensure_utc(record.ended_at).isoformat() if record.ended_at else None,
            ),
        )

    def get(self, source_id: str, source_record_id: str) -> SourceRecord | None:
        rows = self._query(
            "SELECT * FROM source_records WHERE source_id=? AND source_record_id=?",
            (source_id, source_record_id),
        )
        if not rows:
            return None
        r = rows[0]
        return SourceRecord(
            source_id=r["source_id"],
            source_record_id=r["source_record_id"],
            fingerprint=r["fingerprint"],
            version=r["version"],
            first_seen=datetime.fromisoformat(r["first_seen"]),
            last_confirmed=datetime.fromisoformat(r["last_confirmed"]),
            missed_polls=r["missed_polls"],
            is_active=bool(r["is_active"]),
            ended_at=datetime.fromisoformat(r["ended_at"]) if r["ended_at"] else None,
        )

    def list_active(self, source_id: str) -> list[SourceRecord]:
        rows = self._query(
            "SELECT * FROM source_records WHERE source_id=? AND is_active=1",
            (source_id,),
        )
        return [
            SourceRecord(
                source_id=r["source_id"],
                source_record_id=r["source_record_id"],
                fingerprint=r["fingerprint"],
                version=r["version"],
                first_seen=datetime.fromisoformat(r["first_seen"]),
                last_confirmed=datetime.fromisoformat(r["last_confirmed"]),
                missed_polls=r["missed_polls"],
                is_active=bool(r["is_active"]),
                ended_at=datetime.fromisoformat(r["ended_at"]) if r["ended_at"] else None,
            )
            for r in rows
        ]

    def confirm_present(self, source_id: str, source_record_id: str, fingerprint: str) -> tuple[bool, int]:
        existing = self.get(source_id, source_record_id)
        now_dt = datetime.now(UTC)
        if existing is None:
            new_record = SourceRecord(
                source_id=source_id,
                source_record_id=source_record_id,
                fingerprint=fingerprint,
                version=1,
                first_seen=now_dt,
                last_confirmed=now_dt,
                missed_polls=0,
                is_active=True,
            )
            self.upsert(new_record)
            return (True, 1)

        changed = (existing.fingerprint != fingerprint)
        new_version = existing.version + 1 if changed else existing.version
        updated = existing.model_copy(
            update={
                "fingerprint": fingerprint,
                "version": new_version,
                "last_confirmed": now_dt,
                "missed_polls": 0,
                "is_active": True,
            }
        )
        self.upsert(updated)
        return (changed, new_version)

    def mark_missed(
        self, source_id: str, missing_ids: Sequence[str], threshold: int
    ) -> list[SourceRecord]:
        ended: list[SourceRecord] = []
        now_dt = datetime.now(UTC)
        for rid in missing_ids:
            rec = self.get(source_id, rid)
            if not rec or not rec.is_active:
                continue
            missed = rec.missed_polls + 1
            if missed >= threshold:
                rec_ended = rec.model_copy(
                    update={
                        "missed_polls": missed,
                        "is_active": False,
                        "ended_at": now_dt,
                    }
                )
                self.upsert(rec_ended)
                ended.append(rec_ended)
            else:
                rec_updated = rec.model_copy(update={"missed_polls": missed})
                self.upsert(rec_updated)
        return ended


class SqliteCorrelationCandidateRepository(_SqliteRepo, CorrelationCandidateRepository):
    def append(self, candidate: CorrelationCandidate) -> None:
        self._execute(
            """INSERT INTO correlation_candidates (
                candidate_id, observation_id, event_id, score, reasons, decision, created_at, status
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(candidate_id) DO NOTHING""",
            (
                candidate.candidate_id,
                candidate.observation_id,
                candidate.event_id,
                candidate.score,
                json.dumps(list(candidate.reasons)),
                str(candidate.decision),
                ensure_utc(candidate.created_at).isoformat(),
                candidate.status,
            ),
        )

    def list_pending(self, limit: int = 100) -> list[CorrelationCandidate]:
        rows = self._query(
            "SELECT * FROM correlation_candidates WHERE status='pending' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [
            CorrelationCandidate(
                candidate_id=r["candidate_id"],
                observation_id=r["observation_id"],
                event_id=r["event_id"],
                score=r["score"],
                reasons=tuple(json.loads(r["reasons"] or "[]")),
                decision=r["decision"],
                created_at=datetime.fromisoformat(r["created_at"]),
                status=r["status"],
            )
            for r in rows
        ]

    def for_event(self, event_id: str) -> list[CorrelationCandidate]:
        rows = self._query(
            "SELECT * FROM correlation_candidates WHERE event_id=? ORDER BY created_at DESC",
            (event_id,),
        )
        return [
            CorrelationCandidate(
                candidate_id=r["candidate_id"],
                observation_id=r["observation_id"],
                event_id=r["event_id"],
                score=r["score"],
                reasons=tuple(json.loads(r["reasons"] or "[]")),
                decision=r["decision"],
                created_at=datetime.fromisoformat(r["created_at"]),
                status=r["status"],
            )
            for r in rows
        ]

    def update_status(self, candidate_id: str, status: str) -> bool:
        cur = self._execute(
            "UPDATE correlation_candidates SET status=? WHERE candidate_id=?",
            (status, candidate_id),
        )
        return bool(cur.rowcount)


class SqliteContradictionRepository(_SqliteRepo, ContradictionRepository):
    def append(self, contradiction: Contradiction) -> None:
        self._execute(
            """INSERT INTO contradictions (
                contradiction_id, event_id, predicate, claim_id_a, claim_id_b,
                value_a, value_b, detected_at, resolved
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(contradiction_id) DO NOTHING""",
            (
                contradiction.contradiction_id,
                contradiction.event_id,
                contradiction.predicate,
                contradiction.claim_id_a,
                contradiction.claim_id_b,
                json.dumps(contradiction.value_a, default=str),
                json.dumps(contradiction.value_b, default=str),
                ensure_utc(contradiction.detected_at).isoformat(),
                1 if contradiction.resolved else 0,
            ),
        )

    def for_event(self, event_id: str) -> list[Contradiction]:
        rows = self._query(
            "SELECT * FROM contradictions WHERE event_id=? ORDER BY detected_at DESC",
            (event_id,),
        )
        return [
            Contradiction(
                contradiction_id=r["contradiction_id"],
                event_id=r["event_id"],
                predicate=r["predicate"],
                claim_id_a=r["claim_id_a"],
                claim_id_b=r["claim_id_b"],
                value_a=_json_or_none(r["value_a"]),
                value_b=_json_or_none(r["value_b"]),
                detected_at=datetime.fromisoformat(r["detected_at"]),
                resolved=bool(r["resolved"]),
            )
            for r in rows
        ]

    def unresolved(self, limit: int = 100) -> list[Contradiction]:
        rows = self._query(
            "SELECT * FROM contradictions WHERE resolved=0 ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        )
        return [
            Contradiction(
                contradiction_id=r["contradiction_id"],
                event_id=r["event_id"],
                predicate=r["predicate"],
                claim_id_a=r["claim_id_a"],
                claim_id_b=r["claim_id_b"],
                value_a=_json_or_none(r["value_a"]),
                value_b=_json_or_none(r["value_b"]),
                detected_at=datetime.fromisoformat(r["detected_at"]),
                resolved=bool(r["resolved"]),
            )
            for r in rows
        ]


class SqliteMaterialChangeRepository(_SqliteRepo, MaterialChangeRepository):
    def append(self, record: MaterialChangeRecord) -> None:
        self._execute(
            """INSERT INTO material_changes (
                change_id, event_id, version, changed_fields, change_flags, is_material, reason, recorded_at
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                new_id("mat"),
                record.event_id,
                record.version,
                json.dumps(list(record.changed_fields)),
                json.dumps(list(record.change_flags)),
                1 if record.is_material else 0,
                record.reason,
                ensure_utc(record.recorded_at).isoformat(),
            ),
        )

    def list_for_event(self, event_id: str) -> list[MaterialChangeRecord]:
        rows = self._query(
            "SELECT * FROM material_changes WHERE event_id=? ORDER BY version ASC",
            (event_id,),
        )
        return [
            MaterialChangeRecord(
                event_id=r["event_id"],
                version=r["version"],
                changed_fields=tuple(json.loads(r["changed_fields"] or "[]")),
                change_flags=tuple(json.loads(r["change_flags"] or "[]")),
                is_material=bool(r["is_material"]),
                reason=r["reason"] or "",
                recorded_at=datetime.fromisoformat(r["recorded_at"]),
            )
            for r in rows
        ]

    def list_recent(self, limit: int = 50) -> list[MaterialChangeRecord]:
        rows = self._query(
            "SELECT * FROM material_changes WHERE is_material=1 ORDER BY recorded_at DESC LIMIT ?",
            (limit,),
        )
        return [
            MaterialChangeRecord(
                event_id=r["event_id"],
                version=r["version"],
                changed_fields=tuple(json.loads(r["changed_fields"] or "[]")),
                change_flags=tuple(json.loads(r["change_flags"] or "[]")),
                is_material=bool(r["is_material"]),
                reason=r["reason"] or "",
                recorded_at=datetime.fromisoformat(r["recorded_at"]),
            )
            for r in rows
        ]


class SqliteStateRepository(_SqliteRepo, StateRepository):
    def append_state(self, state: EventState) -> None:
        self._execute(
            """INSERT INTO event_states (
                   event_id, state_version, state, reconstructed_at, created_at
               ) VALUES (?,?,?,?,?)
               ON CONFLICT(event_id, state_version) DO NOTHING""",
            (
                state.event_id,
                state.state_version,
                _dump(state),
                ensure_utc(state.reconstructed_at).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )

    def latest(self, event_id: str) -> EventState | None:
        rows = self._query(
            "SELECT state FROM event_states WHERE event_id=? ORDER BY state_version DESC LIMIT 1",
            (event_id,),
        )
        return EventState.model_validate_json(rows[0]["state"]) if rows else None

    def version(self, event_id: str, state_version: int) -> EventState | None:
        rows = self._query(
            "SELECT state FROM event_states WHERE event_id=? AND state_version=?",
            (event_id, state_version),
        )
        return EventState.model_validate_json(rows[0]["state"]) if rows else None

    def history(self, event_id: str) -> list[EventState]:
        rows = self._query(
            "SELECT state FROM event_states WHERE event_id=? ORDER BY state_version ASC", (event_id,)
        )
        return [EventState.model_validate_json(r["state"]) for r in rows]

    def state_as_of(self, event_id: str, as_of: datetime) -> EventState | None:
        # Section 49 replay asks what we *believed* at a past moment. That is
        # the reconstruction time, not the wall-clock time the row happened to be
        # written: a backfill of last week's incident inserts every row today, so
        # filtering on created_at would make replay impossible.
        rows = self._query(
            """SELECT state FROM event_states
               WHERE event_id=? AND reconstructed_at <= ?
               ORDER BY reconstructed_at DESC, state_version DESC LIMIT 1""",
            (event_id, ensure_utc(as_of).isoformat()),
        )
        return EventState.model_validate_json(rows[0]["state"]) if rows else None

    def latest_all(self, limit: int = 1000) -> list[EventState]:
        """Latest state per event in one read.

        A correlated subquery per event would work and would be a query per
        event anyway, so this uses a window function and reads the table once.
        The world projection runs on every cycle over every event, which makes
        the difference between O(1) and O(events) queries per cycle.
        """

        rows = self._query(
            """SELECT state FROM (
                   SELECT state, state_version, reconstructed_at,
                          ROW_NUMBER() OVER (
                              PARTITION BY event_id ORDER BY state_version DESC
                          ) AS rn
                   FROM event_states
               ) WHERE rn = 1
               ORDER BY reconstructed_at DESC
               LIMIT ?""",
            (limit,),
        )
        return [EventState.model_validate_json(r["state"]) for r in rows]


class SqliteStateDeltaRepository(_SqliteRepo, StateDeltaRepository):
    """Durable state deltas.

    This is the table that makes "what changed?" a first-class, queryable fact
    rather than a field buried in an analysis run's JSON. Two consequences matter:

    - A delta exists the moment its state version exists, in the same
      transaction, so it cannot be lost because no analysis ran.
    - It can be asked about without parsing run blobs: "what has materially
      changed about this event in the last hour" and "what is changing anywhere
      in this region right now" are both single indexed reads.
    """

    def append_many(self, records: Sequence[StateDeltaRecord]) -> int:
        if not records:
            return 0
        written = 0
        for record in records:
            cursor = self._execute(
                """INSERT INTO state_deltas (
                       delta_id, schema_version, event_id, region_id,
                       state_version, previous_state_version, change, domain,
                       before, after, magnitude, confidence, novelty,
                       is_material, causes, urgency, affected_user_count,
                       official_guidance, recorded_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(delta_id) DO NOTHING""",
                (
                    record.delta_id,
                    record.schema_version,
                    record.event_id,
                    record.region_id,
                    record.state_version,
                    record.previous_state_version,
                    record.change,
                    record.domain,
                    json.dumps(record.before, default=str),
                    json.dumps(record.after, default=str),
                    record.magnitude,
                    record.confidence,
                    record.novelty,
                    1 if record.is_material else 0,
                    json.dumps(list(record.causes)),
                    str(record.urgency),
                    record.affected_user_count,
                    1 if record.official_guidance else 0,
                    ensure_utc(record.recorded_at).isoformat(),
                ),
            )
            # rowcount is 0 on conflict, so re-rebuilding an identical state
            # version reports 0 new deltas rather than double-counting them.
            written += max(0, cursor.rowcount)
        return written

    def for_event(self, event_id: str, limit: int = 200) -> list[StateDeltaRecord]:
        rows = self._query(
            """SELECT * FROM state_deltas WHERE event_id=?
               ORDER BY state_version ASC, magnitude DESC LIMIT ?""",
            (event_id, limit),
        )
        return [_delta_from_row(r) for r in rows]

    def for_state_version(self, event_id: str, state_version: int) -> list[StateDeltaRecord]:
        rows = self._query(
            """SELECT * FROM state_deltas WHERE event_id=? AND state_version=?
               ORDER BY magnitude DESC""",
            (event_id, state_version),
        )
        return [_delta_from_row(r) for r in rows]

    def material_since(self, event_id: str, since: datetime) -> list[StateDeltaRecord]:
        rows = self._query(
            """SELECT * FROM state_deltas
               WHERE event_id=? AND is_material=1 AND recorded_at >= ?
               ORDER BY recorded_at ASC, magnitude DESC""",
            (event_id, ensure_utc(since).isoformat()),
        )
        return [_delta_from_row(r) for r in rows]

    def recent(self, region_id: str | None = None, limit: int = 200) -> list[StateDeltaRecord]:
        if region_id:
            rows = self._query(
                """SELECT * FROM state_deltas
                   WHERE is_material=1 AND region_id=?
                   ORDER BY recorded_at DESC, magnitude DESC LIMIT ?""",
                (region_id, limit),
            )
        else:
            rows = self._query(
                """SELECT * FROM state_deltas WHERE is_material=1
                   ORDER BY recorded_at DESC, magnitude DESC LIMIT ?""",
                (limit,),
            )
        return [_delta_from_row(r) for r in rows]

    def count(self) -> int:
        rows = self._query("SELECT COUNT(*) AS n FROM state_deltas")
        return int(rows[0]["n"]) if rows else 0


class SqliteEventLifecycleRepository(_SqliteRepo, EventLifecycleRepository):
    def record(self, transition: EventLifecycleTransition) -> None:
        self._execute(
            """INSERT INTO event_lifecycle (
                   transition_id, event_id, region_id, from_status, to_status,
                   reason, termination_basis, confidence,
                   evidence_observation_ids, silence_threshold_seconds,
                   observation_count, state_version, at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(transition_id) DO NOTHING""",
            (
                transition.transition_id,
                transition.event_id,
                transition.region_id,
                transition.from_status,
                transition.to_status,
                transition.reason,
                transition.termination_basis,
                transition.confidence,
                json.dumps(list(transition.evidence_observation_ids)),
                transition.silence_threshold_seconds,
                transition.observation_count,
                transition.state_version,
                ensure_utc(transition.at).isoformat(),
            ),
        )

    def for_event(self, event_id: str) -> list[EventLifecycleTransition]:
        rows = self._query(
            "SELECT * FROM event_lifecycle WHERE event_id=? ORDER BY at ASC", (event_id,)
        )
        return [_transition_from_row(r) for r in rows]

    def latest(self, event_id: str) -> EventLifecycleTransition | None:
        rows = self._query(
            "SELECT * FROM event_lifecycle WHERE event_id=? ORDER BY at DESC LIMIT 1",
            (event_id,),
        )
        return _transition_from_row(rows[0]) if rows else None


class SqliteWorldSnapshotRepository(_SqliteRepo, WorldSnapshotRepository):
    def put(self, snapshot: WorldSnapshot) -> None:
        self._execute(
            """INSERT INTO world_snapshots (
                   snapshot_id, schema_version, region_id, generated_at,
                   observation_total, events_total, events_active,
                   events_quiescent, events_closed, events_changed_materially,
                   snapshot
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(snapshot_id) DO UPDATE SET
                   snapshot=excluded.snapshot,
                   generated_at=excluded.generated_at""",
            (
                snapshot.snapshot_id,
                snapshot.schema_version,
                snapshot.region_id,
                ensure_utc(snapshot.generated_at).isoformat(),
                snapshot.observation_total,
                snapshot.events_total,
                snapshot.events_active,
                snapshot.events_quiescent,
                snapshot.events_closed,
                snapshot.events_changed_materially,
                _dump(snapshot),
            ),
        )

    def latest(self, region_id: str) -> WorldSnapshot | None:
        rows = self._query(
            """SELECT snapshot FROM world_snapshots WHERE region_id=?
               ORDER BY generated_at DESC LIMIT 1""",
            (region_id,),
        )
        return WorldSnapshot.model_validate_json(rows[0]["snapshot"]) if rows else None

    def history(self, region_id: str, limit: int = 50) -> list[WorldSnapshot]:
        rows = self._query(
            """SELECT snapshot FROM world_snapshots WHERE region_id=?
               ORDER BY generated_at DESC LIMIT ?""",
            (region_id, limit),
        )
        return [WorldSnapshot.model_validate_json(r["snapshot"]) for r in rows]


class SqliteClaimRepository(_SqliteRepo, ClaimRepository):
    def append(self, claim: Claim) -> None:
        self._execute(
            """INSERT INTO claims (
                claim_id, observation_id, event_id, predicate, value, geometry,
                valid_from, valid_until, extraction_method, extraction_confidence,
                source_id, truth_status, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(claim_id) DO NOTHING""",
            (
                claim.claim_id,
                claim.observation_id,
                claim.event_id,
                claim.predicate,
                json.dumps(claim.value, default=str),
                json.dumps(claim.geometry) if claim.geometry else None,
                claim.valid_from.isoformat() if claim.valid_from else None,
                claim.valid_until.isoformat() if claim.valid_until else None,
                claim.extraction_method,
                claim.extraction_confidence,
                claim.source_id,
                str(claim.truth_status),
                datetime.now(UTC).isoformat(),
            ),
        )

    def claims_for_event(self, event_id: str) -> list[Claim]:
        rows = self._query("SELECT * FROM claims WHERE event_id=?", (event_id,))
        return [_row_to_claim(r) for r in rows]

    def claims_for_observation(self, observation_id: str) -> list[Claim]:
        rows = self._query("SELECT * FROM claims WHERE observation_id=?", (observation_id,))
        return [_row_to_claim(r) for r in rows]


class SqliteAnalysisRunRepository(_SqliteRepo, AnalysisRunRepository):
    def append(self, run: AnalysisRun) -> None:
        self._execute(
            """INSERT INTO analysis_runs (
                analysis_run_id, event_id, region_id, trigger, previous_state_version,
                new_state_version, started_at, completed_at, run
            ) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(analysis_run_id) DO NOTHING""",
            (
                run.analysis_run_id,
                run.event_id,
                run.region_id,
                run.trigger,
                run.previous_state_version,
                run.new_state_version,
                run.started_at.isoformat(),
                run.completed_at.isoformat(),
                _dump(run),
            ),
        )

    def latest_for_event(self, event_id: str) -> AnalysisRun | None:
        rows = self._query(
            "SELECT run FROM analysis_runs WHERE event_id=? ORDER BY completed_at DESC LIMIT 1",
            (event_id,),
        )
        return AnalysisRun.model_validate_json(rows[0]["run"]) if rows else None

    def for_event(self, event_id: str, limit: int = 50) -> list[AnalysisRun]:
        rows = self._query(
            "SELECT run FROM analysis_runs WHERE event_id=? ORDER BY completed_at DESC LIMIT ?",
            (event_id, limit),
        )
        return [AnalysisRun.model_validate_json(r["run"]) for r in rows]

    def by_id(self, analysis_run_id: str) -> AnalysisRun | None:
        rows = self._query(
            "SELECT run FROM analysis_runs WHERE analysis_run_id=?", (analysis_run_id,)
        )
        return AnalysisRun.model_validate_json(rows[0]["run"]) if rows else None


class SqliteUserRepository(_SqliteRepo, UserRepository):
    def upsert(self, user: UserContext) -> None:
        self._execute(
            """INSERT INTO users (
                user_id, saved_places, route_profiles, transport_modes, preferences,
                current_location, current_location_expires_at, active_destination_id,
                active_route_id, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                saved_places=excluded.saved_places,
                route_profiles=excluded.route_profiles,
                transport_modes=excluded.transport_modes,
                preferences=excluded.preferences,
                current_location=excluded.current_location,
                current_location_expires_at=excluded.current_location_expires_at,
                active_destination_id=excluded.active_destination_id,
                active_route_id=excluded.active_route_id,
                updated_at=excluded.updated_at""",
            (
                user.user_id,
                _dump([p.model_dump(mode="json") for p in user.saved_places]),
                _dump([r.model_dump(mode="json") for r in user.route_profiles]),
                _dump(list(user.transport_modes)),
                _dump(user.preferences.model_dump(mode="json")),
                json.dumps(user.current_location) if user.current_location else None,
                user.current_location_expires_at.isoformat() if user.current_location_expires_at else None,
                user.active_destination_id,
                user.active_route_id,
                datetime.now(UTC).isoformat(),
            ),
        )

    def get(self, user_id: str) -> UserContext | None:
        rows = self._query("SELECT * FROM users WHERE user_id=?", (user_id,))
        return _row_to_user(rows[0]) if rows else None

    def all(self) -> list[UserContext]:
        return [_row_to_user(r) for r in self._query("SELECT * FROM users")]

    def set_ephemeral_location(
        self, user_id: str, geometry: dict[str, Any] | None, expires_at: datetime | None
    ) -> None:
        self._execute(
            "UPDATE users SET current_location=?, current_location_expires_at=?, updated_at=? WHERE user_id=?",
            (
                json.dumps(geometry) if geometry else None,
                expires_at.isoformat() if expires_at else None,
                datetime.now(UTC).isoformat(),
                user_id,
            ),
        )

    def add_place(self, user_id: str, place: SavedPlace) -> None:
        user = self.get(user_id)
        if user is None:
            return
        places = [p for p in user.saved_places if p.place_id != place.place_id]
        places.append(place)
        self.upsert(user.model_copy(update={"saved_places": tuple(places)}))

    def add_route(self, user_id: str, route: RouteProfile) -> None:
        user = self.get(user_id)
        if user is None:
            return
        routes = [r for r in user.route_profiles if r.route_id != route.route_id]
        routes.append(route)
        self.upsert(user.model_copy(update={"route_profiles": tuple(routes)}))


class SqliteUserImpactRepository(_SqliteRepo, UserImpactRepository):
    def put(self, exposure_json: str, analysis_run_id: str) -> None:
        import json as _json

        data = _json.loads(exposure_json)
        self._execute(
            """INSERT INTO user_impacts (
                analysis_run_id, user_id, event_id, exposure_level, priority, computed_at, exposure
            ) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(analysis_run_id) DO UPDATE SET
                exposure_level=excluded.exposure_level,
                priority=excluded.priority,
                computed_at=excluded.computed_at,
                exposure=excluded.exposure""",
            (
                analysis_run_id,
                data["user_id"],
                data["event_id"],
                data["current"]["exposure_level"],
                float(data.get("priority", 0.0)),
                data["current"].get("as_of") or datetime.now(UTC).isoformat(),
                exposure_json,
            ),
        )

    def latest(self, user_id: str, event_id: str) -> dict[str, Any] | None:
        import json as _json

        rows = self._query(
            "SELECT exposure FROM user_impacts WHERE user_id=? AND event_id=? ORDER BY computed_at DESC LIMIT 1",
            (user_id, event_id),
        )
        return _json.loads(rows[0]["exposure"]) if rows else None

    def for_user(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        import json as _json

        rows = self._query(
            "SELECT exposure FROM user_impacts WHERE user_id=? ORDER BY computed_at DESC LIMIT ?",
            (user_id, limit),
        )
        return [_json.loads(r["exposure"]) for r in rows]


class SqliteNotificationRepository(_SqliteRepo, NotificationRepository):
    def enqueue(self, notification: NotificationCandidate) -> bool:
        try:
            self._execute(
                """INSERT INTO notifications (
                    notification_id, user_id, event_id, reason, urgency, dedupe_key,
                    status, created_at, payload
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    notification.notification_id,
                    notification.user_id,
                    notification.event_id,
                    str(notification.reason),
                    str(notification.urgency),
                    notification.dedupe_key,
                    "pending",
                    notification.created_at.isoformat(),
                    _dump(notification),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def _load(self, row: sqlite3.Row) -> NotificationCandidate:
        return NotificationCandidate.model_validate_json(row["payload"])

    def pending(self) -> list[NotificationCandidate]:
        rows = self._query("SELECT payload FROM notifications WHERE status='pending' ORDER BY created_at ASC")
        return [self._load(r) for r in rows]

    def mark_sent(self, notification_id: str) -> None:
        self._execute(
            "UPDATE notifications SET status='sent', sent_at=? WHERE notification_id=?",
            (datetime.now(UTC).isoformat(), notification_id),
        )

    def seen_dedupe_key(self, dedupe_key: str) -> bool:
        rows = self._query("SELECT 1 FROM notifications WHERE dedupe_key=?", (dedupe_key,))
        return bool(rows)

    def recent(self, limit: int = 50) -> list[NotificationCandidate]:
        rows = self._query("SELECT payload FROM notifications ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._load(r) for r in rows]


class SqliteSourceHealthRepository(_SqliteRepo, SourceHealthRepository):
    def put(self, health: SourceHealth) -> None:
        self._execute(
            """INSERT INTO source_health (source_id, state, updated_at, health)
               VALUES (?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
               state=excluded.state, updated_at=excluded.updated_at, health=excluded.health""",
            (health.source_id, str(health.state), datetime.now(UTC).isoformat(), _dump(health)),
        )

    def all(self) -> list[SourceHealth]:
        return [SourceHealth.model_validate_json(r["health"]) for r in self._query("SELECT health FROM source_health")]

    def get(self, source_id: str) -> SourceHealth | None:
        rows = self._query("SELECT health FROM source_health WHERE source_id=?", (source_id,))
        return SourceHealth.model_validate_json(rows[0]["health"]) if rows else None


class SqlitePlatformRepository(PlatformRepository):
    def __init__(self, database_url: str = "sqlite:///./data/infraimpact.db") -> None:
        path = database_url.split("sqlite:///", 1)[-1] if database_url.startswith("sqlite") else database_url
        if path not in {":memory:", ""}:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # ``synchronous=NORMAL`` under WAL, set explicitly on the connection
            # rather than left to the default of FULL.
            #
            # This is the single largest performance fact about the local
            # driver. Under FULL, every commit is an fsync, and this repository
            # committed once per statement: five commits per ingested record,
            # roughly 65,000 fsyncs for a cold ingest of the supplied feeds,
            # and 1,062 of the 1,177 seconds a full ``seed --cycle`` took.
            #
            # What NORMAL under WAL actually gives up: a transaction is durable
            # across an application crash, and may be lost only if the operating
            # system or the power fails. Given that the ledger is rebuilt from
            # ``live_feeds`` on demand and that correctness claims here are
            # about *provenance and ordering* rather than about surviving power
            # loss, that is the right trade. FULL is a one-line change for a
            # deployment that needs it.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # Readers should not block on the writer. The loop fans out over
            # events while the API may be serving, and the default rollback
            # journal would serialise them.
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            # Backfill anything an older dev database is missing.
            _ensure_column(self._conn, "event_states", "reconstructed_at", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(self._conn, "events", "kind", "TEXT NOT NULL DEFAULT 'incident'")
            _ensure_column(self._conn, "events", "phase", "TEXT NOT NULL DEFAULT 'active'")
            _ensure_column(self._conn, "events", "parent_event_id", "TEXT")
            _ensure_column(self._conn, "observations", "published_at", "TEXT")
            _ensure_column(self._conn, "observations", "event_time_confidence", "TEXT NOT NULL DEFAULT 'known'")
            _ensure_column(self._conn, "observations", "significance_class", "TEXT NOT NULL DEFAULT 'event_candidate'")
            _ensure_column(self._conn, "observations", "version", "INTEGER NOT NULL DEFAULT 1")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_significance ON observations(significance_class, observed_at DESC)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind_phase ON events(kind, phase, updated_at DESC)")
            self._conn.commit()

        #: Shared across every sub-repository on this connection. See
        #: :meth:`PlatformRepository.transaction`.
        self._tx = _Transaction()

        base = _SqliteRepo(self._conn, self._lock, self._tx)
        self.observations = SqliteObservationRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.events = SqliteEventRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.states = SqliteStateRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.deltas = SqliteStateDeltaRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.lifecycle = SqliteEventLifecycleRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.world = SqliteWorldSnapshotRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.claims = SqliteClaimRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.runs = SqliteAnalysisRunRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.users = SqliteUserRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.user_impacts = SqliteUserImpactRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.notifications = SqliteNotificationRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.source_health = SqliteSourceHealthRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.source_records = SqliteSourceRecordRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.candidates = SqliteCorrelationCandidateRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.contradictions = SqliteContradictionRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.material_changes = SqliteMaterialChangeRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        _ = base

    @contextmanager
    def transaction(self) -> Iterator[PlatformRepository]:
        """Group many writes into one commit. Re-entrant.

        The repository contract already says an observation is immutable and
        that consumers are at-least-once and idempotent, so a crash mid-batch is
        safe either way. What it does *not* say is that a half-linked
        observation - stored but not yet resolved to an event - is acceptable,
        and it is not: the resolver would rebuild the link on the next pass but
        the ledger would briefly disagree with itself.

        Nesting is by depth, so a pipeline that opens a transaction per record
        inside one that spans the batch commits exactly once, at the outermost
        exit. Exceptions roll back to the outermost boundary.
        """

        with self._lock:
            self._tx.depth += 1
            try:
                yield self
            except BaseException:
                self._tx.depth -= 1
                if self._tx.depth == 0:
                    self._conn.rollback()
                raise
            else:
                self._tx.depth -= 1
                if self._tx.depth == 0:
                    self._conn.commit()

    def checkpoint(self, consumer: str, offset: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO consumers (consumer, offset) VALUES (?,?)
                   ON CONFLICT(consumer) DO UPDATE SET offset=excluded.offset""",
                (consumer, offset),
            )
            if self._tx.depth == 0:
                self._conn.commit()

    def get_checkpoint(self, consumer: str) -> str | None:
        rows = self._conn.execute("SELECT offset FROM consumers WHERE consumer=?", (consumer,)).fetchall()
        return rows[0]["offset"] if rows else None

    def execute_raw(self, sql: str, params: Sequence[Any] | None = None) -> Iterable[dict[str, Any]]:
        cur = self._conn.execute(sql, tuple(params or ()))
        return [dict(r) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_observation(row: sqlite3.Row) -> Observation:
    import json as _json

    from ..domain.geo import centroid_of
    from ..domain.schemas import ObservationQuality, Provenance

    geom = _json.loads(row["geometry"]) if row["geometry"] else None
    c = centroid_of(geom) if geom else None
    centroid = (round(c[0], 6), round(c[1], 6)) if c else None
    row_keys = row.keys()
    event_id = row["event_id"] if "event_id" in row_keys and row["event_id"] else None

    return Observation(
        observation_id=row["observation_id"],
        schema_version=row["schema_version"],
        source_id=row["source_id"],
        source_record_id=row["source_record_id"] or "",
        event_time=row["event_time"] if row["event_time"] else None,
        event_time_confidence=row["event_time_confidence"] if "event_time_confidence" in row_keys else "known",
        published_at=row["published_at"] if "published_at" in row_keys and row["published_at"] else None,
        observed_at=row["observed_at"],
        ingested_at=row["ingested_at"],
        source_type=row["source_type"],
        observation_type=row["observation_type"],
        significance_class=row["significance_class"] if "significance_class" in row_keys else "event_candidate",
        version=row["version"] if "version" in row_keys else 1,
        geometry=geom,
        location_precision_m=row["location_precision_m"],
        headline=row["headline"],
        structured_payload=_json.loads(row["structured_payload"]),
        provenance=Provenance.model_validate(_json.loads(row["provenance"])),
        quality=ObservationQuality.model_validate(_json.loads(row["quality"])),
        source_url=row["source_url"],
        raw_payload_uri=row["raw_payload_uri"],
        centroid=centroid,
        event_id=event_id,
    )




def _row_to_claim(row: sqlite3.Row) -> Claim:
    import json as _json

    return Claim(
        claim_id=row["claim_id"],
        observation_id=row["observation_id"],
        event_id=row["event_id"],
        predicate=row["predicate"],
        value=_json.loads(row["value"]),
        geometry=_json.loads(row["geometry"]) if row["geometry"] else None,
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        extraction_method=row["extraction_method"],
        extraction_confidence=row["extraction_confidence"],
        source_id=row["source_id"],
        truth_status=row["truth_status"],
    )


def _row_to_user(row: sqlite3.Row) -> UserContext:
    import json as _json

    from ..domain.schemas import NotificationPreferences

    return UserContext(
        user_id=row["user_id"],
        saved_places=tuple(SavedPlace.model_validate(p) for p in _json.loads(row["saved_places"])),
        route_profiles=tuple(RouteProfile.model_validate(r) for r in _json.loads(row["route_profiles"])),
        transport_modes=tuple(_json.loads(row["transport_modes"])),
        preferences=NotificationPreferences.model_validate(_json.loads(row["preferences"])),
        current_location=_json.loads(row["current_location"]) if row["current_location"] else None,
        current_location_expires_at=row["current_location_expires_at"],
        active_destination_id=row["active_destination_id"],
        active_route_id=row["active_route_id"],
    )