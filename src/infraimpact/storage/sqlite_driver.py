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

from ..domain.geo import bbox_of, centroid_of
from ..domain.ids import ensure_utc
from ..domain.schemas import (
    AnalysisRun,
    Claim,
    EventState,
    NotificationCandidate,
    Observation,
    RouteProfile,
    SavedPlace,
    SourceHealth,
    UserContext,
)
from .repository import (
    AnalysisRunRepository,
    ClaimRepository,
    EventRepository,
    NotificationRepository,
    ObservationRepository,
    PlatformRepository,
    SourceHealthRepository,
    StateRepository,
    UserImpactRepository,
    UserRepository,
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS observations (
    observation_id       TEXT PRIMARY KEY,
    schema_version       TEXT NOT NULL,
    source_id            TEXT NOT NULL,
    source_record_id     TEXT,
    event_time           TEXT NOT NULL,
    observed_at          TEXT NOT NULL,
    ingested_at          TEXT NOT NULL,
    source_type          TEXT NOT NULL,
    observation_type     TEXT NOT NULL,
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
-- event_time, not observed_at: candidate generation resolves an observation to an
-- event by *when the thing happened*, and observed_at is when the platform saw it.
-- The two diverge sharply for replayed snapshots, so an observed_at index would
-- make every cold replay scan the whole ledger. observed_at keeps its own index
-- above for the freshness and staleness queries, which genuinely want it.
CREATE INDEX IF NOT EXISTS idx_obs_event_time ON observations(event_time);
CREATE INDEX IF NOT EXISTS idx_obs_source ON observations(source_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_type ON observations(observation_type, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_geo ON observations(min_lon, max_lon, min_lat, max_lat);

CREATE TABLE IF NOT EXISTS events (
    event_id       TEXT PRIMARY KEY,
    region_id      TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'candidate',
    first_observed TEXT,
    last_observed  TEXT,
    closed_at      TEXT,
    close_reason   TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status, updated_at DESC);

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
        try:
            self._execute(
                """INSERT INTO observations (
                    observation_id, schema_version, source_id, source_record_id,
                    event_time, observed_at, ingested_at, source_type, observation_type,
                    authority, headline, geometry, min_lon, min_lat, max_lon, max_lat,
                    centroid_lon, centroid_lat, location_precision_m, structured_payload,
                    provenance, quality, source_url, raw_payload_uri, dedupe_key, payload_hash
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    observation.observation_id,
                    observation.schema_version,
                    observation.source_id,
                    observation.source_record_id,
                    observation.event_time.isoformat(),
                    observation.observed_at.isoformat(),
                    observation.ingested_at.isoformat(),
                    str(observation.source_type),
                    str(observation.observation_type),
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
        rows = self._query("SELECT * FROM observations ORDER BY observed_at DESC LIMIT ?", (limit,))
        return [_row_to_observation(r) for r in rows]

    def count(self) -> int:
        rows = self._query("SELECT COUNT(*) AS c FROM observations")
        return int(rows[0]["c"]) if rows else 0


class SqliteEventRepository(_SqliteRepo, EventRepository):
    def ensure(self, event_id: str, first_observed: datetime, region_id: str) -> None:
        self._execute(
            """INSERT INTO events (event_id, region_id, status, first_observed, created_at, updated_at)
               VALUES (?,?,'candidate',?,?,?)
               ON CONFLICT(event_id) DO UPDATE SET last_observed=excluded.last_observed,
                                                   updated_at=excluded.updated_at""",
            (event_id, region_id, first_observed.isoformat(), datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )

    def create(self, event_id: str, first_observed: datetime, region_id: str) -> None:
        self.ensure(event_id, first_observed, region_id)

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

    def active_events(self) -> list[str]:
        rows = self._query("SELECT event_id FROM events WHERE status != 'closed'")
        return [r["event_id"] for r in rows]

    def all_events(self) -> list[dict[str, Any]]:
        rows = self._query(
            """SELECT e.*, (SELECT COUNT(*) FROM event_observations eo WHERE eo.event_id=e.event_id) AS observation_count
               FROM events e ORDER BY e.updated_at DESC"""
        )
        return [dict(r) for r in rows]

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
            self._conn.commit()

        #: Shared across every sub-repository on this connection. See
        #: :meth:`PlatformRepository.transaction`.
        self._tx = _Transaction()

        base = _SqliteRepo(self._conn, self._lock, self._tx)
        self.observations = SqliteObservationRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.events = SqliteEventRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.states = SqliteStateRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.claims = SqliteClaimRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.runs = SqliteAnalysisRunRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.users = SqliteUserRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.user_impacts = SqliteUserImpactRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.notifications = SqliteNotificationRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
        self.source_health = SqliteSourceHealthRepository(self._conn, self._lock, self._tx)  # type: ignore[arg-type]
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

    from ..domain.schemas import ObservationQuality, Provenance

    return Observation(
        observation_id=row["observation_id"],
        schema_version=row["schema_version"],
        source_id=row["source_id"],
        source_record_id=row["source_record_id"] or "",
        event_time=row["event_time"],
        observed_at=row["observed_at"],
        ingested_at=row["ingested_at"],
        source_type=row["source_type"],
        observation_type=row["observation_type"],
        geometry=_json.loads(row["geometry"]) if row["geometry"] else None,
        location_precision_m=row["location_precision_m"],
        headline=row["headline"],
        structured_payload=_json.loads(row["structured_payload"]),
        provenance=Provenance.model_validate(_json.loads(row["provenance"])),
        quality=ObservationQuality.model_validate(_json.loads(row["quality"])),
        source_url=row["source_url"],
        raw_payload_uri=row["raw_payload_uri"],
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