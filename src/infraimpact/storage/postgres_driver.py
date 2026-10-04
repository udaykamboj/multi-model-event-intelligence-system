"""PostgreSQL/PostGIS storage driver - **not implemented in V1**.

Both call sites that previously reached for a Postgres repository pointed at
module paths that did not exist, so configuring a Postgres URL produced a bare
``ModuleNotFoundError`` from deep inside the API factory. That is the worst
possible failure for this setting: it looks like a packaging mistake rather than
an unimplemented feature, and it surfaces at first request instead of at
startup.

This module replaces both of them with the truth.

What *is* ready
---------------
The data model is PostGIS-ready, not aspirational:

* :data:`infraimpact.storage.postgres.DDL` is the complete production schema -
  ``geometry(Geometry, 4326)`` columns with GiST indexes, ``h3_cell`` for
  regional partitioning, ``JSONB`` payloads with GIN indexes, a ``TSVECTOR`` for
  payload search, ``pgvector`` state embeddings with an ivfflat index, and
  ``prediction_outcomes`` for the section 45 join back to ground truth.
* :mod:`infraimpact.storage.repository` defines the repository contracts as
  ``Protocol``\\ s over plain JSON-serialisable values, so a driver is a
  translation layer with no domain coupling.
* The SQLite driver implements the same contracts against the same schema, which
  is what keeps the DDL honest: every table here is a table there.

What is not
-----------
No statement-executing driver. Writing 15 repositories against psycopg that
cannot be run - there is no PostgreSQL server in this environment, no
integration test, and no way to verify a single ``INSERT`` - would produce code
that claims to work and does not. For a platform whose entire premise is that
the ledger is trustworthy, untested persistence code is worse than none.

Setting ``INFRAIMPACT_DATABASE_URL=postgres://...`` therefore fails immediately
and says exactly why. Provision the schema with::

    psql "$INFRAIMPACT_DATABASE_URL" -f src/infraimpact/storage/schema.sql

then implement the contracts in this module and delete this class.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

from .postgres import DDL

#: Written out by :func:`write_schema` so it can be fed straight to ``psql``.
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_IMPLEMENTATION_NOTES = """\
INFRAIMPACT_DATABASE_URL points at PostgreSQL, but no psycopg driver is \
implemented in this build.

The schema is ready - run:

    psql "$INFRAIMPACT_DATABASE_URL" -f {schema}

against a database with PostGIS, pg_trgm and pgvector available, then \
implement infraimpact.storage.postgres_driver.PostgresPlatformRepository \
against the contracts in infraimpact.storage.repository.

For local use, leave INFRAIMPACT_DATABASE_URL unset or point it at a SQLite \
file: sqlite:///./data/platform.db
"""


def write_schema(path: Path | str | None = None) -> Path:
    """Write :data:`DDL` next to this module and return the path."""

    target = Path(path) if path else SCHEMA_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(DDL.strip() + "\n", encoding="utf-8")
    return target


def postgres_requirements_met() -> tuple[bool, str]:
    """``(usable, reason)`` for the Postgres path, without raising.

    Used by the CLI and ``/v1/health`` so an unimplemented driver is reported as
    an unimplemented driver instead of as a crash.
    """

    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False, (
            "psycopg is not installed. `pip install '.[postgres]'` installs the "
            "driver, but the repository implementation is still missing."
        )
    return False, "the repository implementation is missing"


class PostgresPlatformRepository:
    """Placeholder that refuses to pretend.

    Exists so that ``build_repository`` has one importable target and so that
    the failure is a sentence a human can act on rather than a traceback about
    a module that was never written.
    """

    def __init__(self, database_url: str, **_: Any) -> None:
        self.database_url = database_url
        raise NotImplementedError(
            _IMPLEMENTATION_NOTES.format(schema=SCHEMA_PATH)
        )

    def __init_subclass__(cls, **kwargs: Any) -> None:  # pragma: no cover
        super().__init_subclass__(**kwargs)


__all__ = [
    "PostgresPlatformRepository",
    "postgres_requirements_met",
    "write_schema",
]
