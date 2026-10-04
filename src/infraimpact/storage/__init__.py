from .raw_store import FileRawStore, RawStore, S3RawStore, build_raw_store
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
from .sqlite_driver import SqlitePlatformRepository


def build_repository(database_url: str, driver: str = "sqlite") -> PlatformRepository:
    """Local default is SQLite; production targets PostgreSQL/PostGIS."""
    if driver == "postgres" or database_url.startswith("postgres"):
        from .postgres_driver import PostgresPlatformRepository

        return PostgresPlatformRepository(database_url)
    return SqlitePlatformRepository(database_url)


__all__ = [
    "AnalysisRunRepository",
    "ClaimRepository",
    "EventRepository",
    "FileRawStore",
    "NotificationRepository",
    "ObservationRepository",
    "PlatformRepository",
    "RawStore",
    "S3RawStore",
    "SourceHealthRepository",
    "SqlitePlatformRepository",
    "StateRepository",
    "UserImpactRepository",
    "UserRepository",
    "build_raw_store",
    "build_repository",
]